"""Parallel checking across CPU cores via subinterpreters (Python 3.14+).

`_run_parallel` yields result tuples in file order; `cli` does the printing.

Each subinterpreter gets its own `nib` module table, registry, and rule
instances. We resolve the final select/ignore token lists in the main
interpreter (so plugin import errors and registry-validation errors surface
once) and ship them to the workers, along with the already-merged plugin list.
Each worker then re-imports the plugins and rebuilds its own rule instances
against its own registry.
"""

import threading
from concurrent import interpreters
from pathlib import Path

from nib.engine import Rule, _check_file, _reimport_plugins, _select_rules


def _worker_loop(
    work_q,
    result_q,
    plugins: tuple,
    select: tuple,
    ignore: tuple,
):
    """Runs *inside* a subinterpreter: build rules once, then drain `work_q`,
    pushing each `_check_file` result tuple onto `result_q` until the `None`
    sentinel arrives."""
    _reimport_plugins(list(plugins))
    rules = [cls() for cls in _select_rules(Rule._registry, list(select), list(ignore))]
    while (file_str := work_q.get()) is not None:
        result_q.put(_check_file(Path(file_str), rules))


def _worker_thread(loop_fn, loop_args, drained):
    """Driver thread: own one subinterpreter and run `loop_fn(*loop_args)` in it.

    A result item on an `interpreters.Queue` is *bound* to the subinterpreter
    that put it there; once that interpreter is destroyed the queued item is no
    longer usable. So after the loop finishes we hold the interpreter open on
    `drained` until the main thread signals it has pulled every result; only
    then do we close."""
    interpreter = interpreters.create()
    try:
        interpreter.call(loop_fn, *loop_args)
        drained.wait()
    finally:
        interpreter.close()


def _spawn_workers(n_workers: int, loop_fn, loop_args: tuple, drained) -> list:
    """Start `n_workers` driver threads, each owning a subinterpreter running
    `loop_fn(*loop_args)`. Returns the threads for the caller to join once it has
    drained every result and set `drained`."""
    threads = [
        threading.Thread(
            target=_worker_thread, args=(loop_fn, loop_args, drained), daemon=True
        )
        for _ in range(n_workers)
    ]
    for t in threads:
        t.start()
    return threads


def _fill_queue(work_q, files: list[Path], n_workers: int) -> None:
    """Enqueue every file path, then one `None` sentinel per worker."""
    for f in files:
        work_q.put(str(f))
    for _ in range(n_workers):
        work_q.put(None)


def _run_parallel(
    files: list[Path],
    n_workers: int,
    plugins: list[str],
    select: list[str],
    ignore: list[str],
):
    """Distribute `files` across `n_workers` subinterpreters, yielding each
    `_check_file` result tuple in file order. The caller emits them, keeping
    all printing in `cli` and this module free of any `cli` dependency.

    `plugins`/`select`/`ignore` are the resolved token lists (plugins already
    merged with `[tool.nib]`); each worker re-imports the plugins and re-runs
    `_select_rules` against its own registry to build the rule instances.
    """
    work_q = interpreters.create_queue()
    result_q = interpreters.create_queue()
    _fill_queue(work_q, files, n_workers)

    # Signalled once we've drained every result, releasing the workers to close
    # their subinterpreters (see `_worker_thread` for why that ordering matters).
    drained = threading.Event()
    loop_args = (
        work_q,
        result_q,
        tuple(plugins),
        tuple(select),
        tuple(ignore),
    )
    threads = _spawn_workers(n_workers, _worker_loop, loop_args, drained)

    # Yield results in file order as they arrive: hold a reorder buffer and
    # flush the consecutive run starting at `next_idx` each time a result lands.
    # Workers pull from a shared queue roughly in order, so the caller's output
    # appears as results arrive rather than all at once, while still matching
    # serial's file order.
    order = {str(f): i for i, f in enumerate(files)}
    pending: dict[int, tuple] = {}
    next_idx = 0
    try:
        for _ in range(len(files)):
            r = result_q.get()
            # the queue is untyped, so `r[0]` trips the type-checker
            pending[order[r[0]]] = r
            while next_idx in pending:
                yield pending.pop(next_idx)
                next_idx += 1
    finally:
        # Release the workers to close their subinterpreters.
        drained.set()
        for t in threads:
            t.join()
