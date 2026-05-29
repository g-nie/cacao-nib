"""Parallel checking across CPU cores via subinterpreters (Python 3.14+).

Kept separate from `nib.cli` so the basic single-file path doesn't carry the
threads/subinterpreter machinery. `cli.main` imports this module lazily, only
when a run is big enough to parallelise — so light/serial runs never touch
`concurrent.interpreters`, `threading`, or psutil.

Each subinterpreter gets its own `nib` module table, registry, and rule
instances. We resolve the final select/ignore token lists in the main
interpreter (so plugin import errors and registry-validation errors surface
once) and ship them to workers; each worker re-runs `_load_plugins` +
`_select_rules` against its own registry to build its rule instances.
"""

import os
import sys
import threading
from concurrent import interpreters
from pathlib import Path

from nib import Rule
from nib.cli import _check_file, _emit_result, _load_plugins, _select_rules


def _worker_loop(work_q, result_q, plugins: tuple, select: tuple, ignore: tuple):
    """Runs *inside* a subinterpreter: build rules once, then drain `work_q`,
    pushing each `_check_file` result tuple onto `result_q` until the `None`
    sentinel arrives."""
    sys.path.insert(0, str(Path.cwd()))
    _load_plugins(list(plugins))
    rules = [cls() for cls in _select_rules(Rule._registry, list(select), list(ignore))]
    while (file_str := work_q.get()) is not None:
        result_q.put(_check_file(Path(file_str), rules))


def _worker_thread(
    work_q, result_q, plugins: tuple, select: tuple, ignore: tuple, drained
):
    """Driver thread: own one subinterpreter and run the worker loop in it.

    A result tuple on an `interpreters.Queue` is *bound* to the subinterpreter
    that put it there; once that interpreter is destroyed the queued item turns
    into a useless `UnboundQueueItem`. So after the loop finishes we hold the
    interpreter open on `drained` until the main thread signals it has pulled
    every result — only then do we close."""

    interpreter = interpreters.create()
    try:
        interpreter.call(_worker_loop, work_q, result_q, plugins, select, ignore)
        drained.wait()
    finally:
        interpreter.close()


def _run_parallel(
    files: list[Path],
    n_workers: int,
    plugins: list[str],
    select: list[str],
    ignore: list[str],
) -> int:
    """Fan `files` out across `n_workers` subinterpreters and stream results in
    file order. Returns the total issue count.

    We only pass the CLI `plugins`/`select`/`ignore` tokens to workers (as
    shareable tuples); each subinterpreter re-runs `_load_plugins` — which
    merges `[tool.nib] plugins` from pyproject itself — and `_select_rules`
    against its own registry to build its rule instances.
    """
    work_q = interpreters.create_queue()
    result_q = interpreters.create_queue()
    for f in files:
        work_q.put(str(f))
    for _ in range(n_workers):
        work_q.put(None)  # sentinel per worker

    # Signalled once we've drained every result, releasing the workers to close
    # their subinterpreters (see `_worker_thread` for why that ordering matters).
    drained = threading.Event()
    threads = [
        threading.Thread(
            target=_worker_thread,
            args=(
                work_q,
                result_q,
                tuple(plugins),
                tuple(select),
                tuple(ignore),
                drained,
            ),
            daemon=True,
        )
        for _ in range(n_workers)
    ]
    for t in threads:
        t.start()

    # Stream results in file order as they arrive: hold a reorder buffer and
    # flush the contiguous run starting at `next_idx` each time a result lands.
    # Workers pull from a shared queue roughly in order, so the head-of-line
    # seldom stalls — output appears progressively rather than all at once,
    # while still matching serial's file order.
    order = {str(f): i for i, f in enumerate(files)}
    pending: dict[int, tuple] = {}
    next_idx = 0
    issues = 0
    for _ in range(len(files)):
        r = result_q.get()  # untyped cross-interp queue → element type is opaque
        pending[order[r[0]]] = r
        while next_idx in pending:
            issues += _emit_result(pending.pop(next_idx))
            next_idx += 1
    drained.set()  # all results pulled — workers may now close their interps
    for t in threads:
        t.join()
    return issues


def _max_workers() -> int:
    """How many workers to run: one per physical core. Parsing and linting keeps
    a core fully busy, so on a big machine the extra logical cores don't help —
    measured ~1.3x slower at one worker per logical core. Never more than the
    CPUs we're actually allowed to use, though.

    The `max(physical, 4)` floor is for small machines: with only 2-3 physical
    cores the coordinator + runtime + OS take up a big slice, so the extra
    logical cores let that overhead run without competing with the workers
    (measured ~1.2x faster there). It only raises the count when physical < 4,
    and `min(logical, ...)` still caps it at the real CPUs.

    `psutil.cpu_count(logical=False)` seems to be the only reliable cross-platform way
    to count physical cores. If psutil can't tell (returns `None`),
    fall back to the logical count.

    psutil is imported here and nowhere else: its C extension won't load in a
    subinterpreter, and workers import `nib.cli` (which never imports this
    module). This function only runs in the main interpreter, so the import
    stays clear of them.
    """
    import psutil

    logical = os.process_cpu_count() or 1
    physical = psutil.cpu_count(logical=False)
    return min(logical, max(physical, 4)) if physical else logical
