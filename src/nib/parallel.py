"""Parallel checking across CPU cores via subinterpreters (Python 3.14+).

Depends only on `nib.engine` (the core check primitives), never on `nib.cli` —
so `cli` can import this module at the top without a cycle. `_run_parallel`
yields result tuples in file order; the caller (`cli`) does the printing.

Each subinterpreter gets its own `nib` module table, registry, and rule
instances. We resolve the final select/ignore token lists in the main
interpreter (so plugin import errors and registry-validation errors surface
once) and ship them — plus the already-merged plugin list — to workers, which
re-import the plugins and rebuild their rule instances against their own
registry.
"""

import threading
from concurrent import interpreters
from pathlib import Path

from nib.engine import Rule, _check_file, _import_plugins, _select_rules


def _worker_loop(work_q, result_q, plugins: tuple, select: tuple, ignore: tuple):
    """Runs *inside* a subinterpreter: build rules once, then drain `work_q`,
    pushing each `_check_file` result tuple onto `result_q` until the `None`
    sentinel arrives."""
    _import_plugins(list(plugins))
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
):
    """Distribute `files` across `n_workers` subinterpreters, yielding each
    `_check_file` result tuple in file order. The caller emits them — keeping
    all printing in `cli` and this module free of any `cli` dependency.

    `plugins`/`select`/`ignore` are the resolved token lists (plugins already
    merged with `[tool.nib]`); each worker re-imports the plugins and re-runs
    `_select_rules` against its own registry to build its rule instances.
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

    # Yield results in file order as they arrive: hold a reorder buffer and
    # flush the contiguous run starting at `next_idx` each time a result lands.
    # Workers pull from a shared queue roughly in order, so the head-of-line
    # seldom stalls — the caller's output appears progressively rather than all
    # at once, while still matching serial's file order.
    order = {str(f): i for i, f in enumerate(files)}
    pending: dict[int, tuple] = {}
    next_idx = 0
    for _ in range(len(files)):
        r = result_q.get()  # untyped cross-interpreter queue → element type is opaque
        pending[order[r[0]]] = r
        while next_idx in pending:
            yield pending.pop(next_idx)
            next_idx += 1
    drained.set()  # all results pulled — workers may now close their interpreters
    for t in threads:
        t.join()
