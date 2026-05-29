import argparse
import ast
import functools
import importlib
import os
import re
import sys
import threading
import tomllib
import warnings
from collections.abc import Iterator
from concurrent import interpreters
from pathlib import Path

from nib import Rule, parse_module, run

# Exit codes
EXIT_OK = 0
EXIT_DIAGNOSTICS = 1  # lint ran cleanly but found violations
EXIT_USAGE = 2  # bad invocation / config / unloadable plugin

# Directory names always pruned during a recursive walk.
# An explicit path on the CLI bypasses this (unless --force-exclude).
DEFAULT_EXCLUDE = frozenset(
    {
        ".bzr",
        ".direnv",
        ".eggs",
        ".git",
        ".hg",
        ".ipynb_checkpoints",
        ".mypy_cache",
        ".nox",
        ".pants.d",
        ".pytest_cache",
        ".pytype",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "__pypackages__",
        "_build",
        "buck-out",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)

# Color only when stdout is a real terminal and NO_COLOR isn't set
# (https://no-color.org). Piped/redirected output stays plain.
_USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _c(text: str, *codes: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


def _walk_py_files(root: Path) -> Iterator[Path]:
    """Walk `root` yielding `.py` files, pruning `DEFAULT_EXCLUDE` dirs in-place."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in DEFAULT_EXCLUDE)
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield Path(dirpath) / name


def _collect_py_files(path: Path, *, force_exclude: bool = False) -> list[Path]:
    """Resolve a CLI path arg to a list of `.py` files.

    Directories are walked with `DEFAULT_EXCLUDE` pruning. Explicit file paths
    bypass that pruning by default — pass `force_exclude=True`
    to apply the excludes even to explicitly-passed paths.
    """
    if force_exclude and any(part in DEFAULT_EXCLUDE for part in path.parts):
        return []
    if path.is_file():
        return [path]
    return list(_walk_py_files(path))


def _find_rules_missing_code(rule_classes) -> list[type]:
    """Rule classes with empty `code` are unselectable and cannot be ignored."""
    return [c for c in rule_classes if not c.code]


def _validate_registry(rule_classes) -> int:
    """Static checks on the loaded rule registry. Returns `EXIT_OK` or
    `EXIT_USAGE`, printing the first failure to stderr."""
    collisions = _check_code_group_collisions(rule_classes)
    if collisions:
        print(
            f"nib: name used as both a code and a group: {sorted(collisions)}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    missing = _find_rules_missing_code(rule_classes)
    if missing:
        names = ", ".join(c.__name__ for c in missing)
        print(f"nib: rule(s) without a `code` attribute: {names}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


def _validate_rules(rules: list[Rule]) -> None:
    """Warn about visit_* methods targeting unknown ast classes.

    A `visit_<Name>` method is valid if `ast.<Name>` exists and subclasses
    `ast.AST` — the same contract `ast.NodeVisitor` dispatches on.
    """
    for rule in rules:
        cls = type(rule)
        for attr in dir(cls):
            if not attr.startswith("visit_") or not callable(getattr(cls, attr, None)):
                continue
            ast_name = attr.removeprefix("visit_")
            target = getattr(ast, ast_name, None)
            if not (isinstance(target, type) and issubclass(target, ast.AST)):
                warnings.warn(
                    f"{cls.__name__}.{attr} targets unknown ast class {ast_name!r}"
                )


# `#` (possibly preceded by whitespace, followed by `noqa` and a word boundary)
# matches either bare or `:codes`. Case-insensitive on the keyword; the codes
# capture stays case-sensitive. Known limitation: a literal `# noqa` inside a
# string is indistinguishable from a real comment here — see
# `_parse_line_suppressions` for the trade-off.
_NOQA_RE = re.compile(
    r"#[ \t]*noqa(?![A-Za-z0-9_])[ \t]*(?::([^\n]*))?",
    re.IGNORECASE,
)


def _parse_line_suppressions(source: str) -> dict[int, set[str] | None]:
    """Scan `source` for `# noqa` directives. Returns `{lineno: codes}` where
    `codes is None` means "suppress every code on this line" and a set means
    "suppress only these codes". Bare `# noqa` is blanket; `# noqa:` with no
    codes is a no-op (the colon signals "I'm listing codes" — empty list
    means none). The `noqa` keyword is case-insensitive;
    rule codes themselves are matched literally.

    Implementation: regex-scan, no tokenize. False positive: a literal
    `# noqa` inside a string is treated as a real directive. We accept this
    in exchange for skipping a full `tokenize` pass on every file.
    """
    out: dict[int, set[str] | None] = {}
    for m in _NOQA_RE.finditer(source):
        lineno = source.count("\n", 0, m.start()) + 1
        rest = m.group(1)
        if rest is None:
            out[lineno] = None
            continue
        codes = {c.strip() for c in rest.split(",") if c.strip()}
        if codes:
            out[lineno] = codes
    return out


@functools.cache
def _config_nib() -> dict:
    """Read the `[tool.nib]` table from cwd's pyproject.toml, if any."""
    pyproject = Path.cwd() / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    return data.get("tool", {}).get("nib", {})


def _parse_codes(s: str) -> list[str]:
    return [p.strip() for p in s.split(",") if p.strip()]


def _check_code_group_collisions(rule_classes) -> set[str]:
    """Return any names used as both a `code` and a `group` across rules."""
    codes = {c.code for c in rule_classes if c.code}
    groups = {c.group for c in rule_classes if c.group}
    return codes & groups


def _select_rules(rule_classes, select: list[str], ignore: list[str]):
    """Filter `rule_classes` by code/group tokens. Ignore wins over select.

    Empty `select` means "all rules". Tokens that match neither a code nor a
    group are silently dropped — they just don't contribute any rules.
    """
    by_code = {c.code: c for c in rule_classes if c.code}
    by_group: dict[str, list] = {}
    for c in rule_classes:
        if c.group:
            by_group.setdefault(c.group, []).append(c)

    def resolve(tokens: list[str]) -> set:
        out: set = set()
        for tok in tokens:
            if tok in by_code:
                out.add(by_code[tok])
            if tok in by_group:
                out.update(by_group[tok])
        return out

    selected = resolve(select) if select else set(rule_classes)
    ignored = resolve(ignore)
    return [c for c in rule_classes if c in selected and c not in ignored]


def _load_plugins(plugins_arg: list[str]) -> int:
    """Make cwd importable, then import plugins from `[tool.nib]` config +
    CLI flag. Returns `EXIT_OK`, or `EXIT_USAGE` on failure. A plugin with
    a syntax error emits an `invalid-syntax` diagnostic and bails — there's
    no useful work to do without rules loaded."""
    sys.path.insert(0, str(Path.cwd()))
    cfg = _config_nib()
    for mod_name in dict.fromkeys(list(cfg.get("plugins", [])) + plugins_arg):
        try:
            importlib.import_module(mod_name)
        except SyntaxError as e:
            _print_diagnostic(
                e.filename or mod_name, e.lineno, e.offset, "invalid-syntax", e.msg
            )
            return EXIT_DIAGNOSTICS
        except ImportError as e:
            print(f"nib: failed to import plugin {mod_name!r}: {e}", file=sys.stderr)
            return EXIT_USAGE
    return EXIT_OK


def _print_diagnostic(file, lineno: int, col: int, code: str, message: str):
    """Render a diagnostic line on stdout in the canonical `path:line:col`
    format. `lineno` and `col` are 1-based; `None` falls back to 1 so callers
    can pass `SyntaxError.lineno`/`offset` directly."""
    print(
        f"{file}:{lineno or 1}:{col or 1}: "
        f"{_c('error', '31')}[{_c(code, '1', '4')}] {message}"
    )


def _cmd_rules(args) -> int:
    if (err := _load_plugins(args.plugins)) != EXIT_OK:
        return err
    if (err := _validate_registry(Rule._registry)) != EXIT_OK:
        return err

    by_group: dict[str | None, list] = {}
    for cls in Rule._registry:
        by_group.setdefault(cls.group, []).append(cls)

    # Sorted groups, then "(no group)" bucket last.
    group_order = sorted(g for g in by_group if g is not None)
    if None in by_group:
        group_order.append(None)

    for group in group_order:
        print(_c(group or "(no group)", "1"))
        for cls in sorted(by_group[group], key=lambda c: (c.code or "", c.__name__)):
            code = cls.code or "(no code)"
            doc = (cls.__doc__ or "").strip().split("\n", 1)[0]
            line = f"  {_c(code, '1', '4')} {cls.__name__}"
            if doc:
                line += f" — {doc}"
            print(line)
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nib")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # Shared `--plugins` flag attached to every subcommand that loads rules.
    plugins_parent = argparse.ArgumentParser(add_help=False)
    plugins_parent.add_argument(
        "--plugins",
        action="append",
        default=[],
        metavar="MODULE",
        help="also import MODULE (repeatable). Plugins listed in "
        "`[tool.nib] plugins = [...]` in cwd's pyproject.toml are loaded too.",
    )

    check = sub.add_parser("check", parents=[plugins_parent], help="lint .py files")
    check.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path("."),
        help="file or directory (default: current directory, recursive)",
    )
    check.add_argument(
        "--select",
        type=_parse_codes,
        default=None,
        metavar="CODES",
        help="comma-separated rule codes/prefixes to run; replaces "
        "`[tool.nib] select` from pyproject.toml.",
    )
    check.add_argument(
        "--ignore",
        type=_parse_codes,
        default=None,
        metavar="CODES",
        help="comma-separated rule codes/prefixes to skip; replaces "
        "`[tool.nib] ignore`. Ignore wins over select.",
    )
    check.add_argument(
        "--extend-select",
        type=_parse_codes,
        default=[],
        metavar="CODES",
        help="like --select, but adds to (rather than replaces) the config value.",
    )
    check.add_argument(
        "--extend-ignore",
        type=_parse_codes,
        default=[],
        metavar="CODES",
        help="like --ignore, but adds to (rather than replaces) the config value.",
    )
    check.add_argument(
        "--force-exclude",
        action="store_true",
        help="apply directory excludes even to paths passed explicitly on the "
        "CLI. Match the mode pre-commit hooks want — without it, an explicit "
        "path inside an excluded dir is linted anyway.",
    )
    sub.add_parser(
        "rules",
        parents=[plugins_parent],
        help="list every registered rule, grouped by `group`",
    )

    return parser


# --- file checking, shared by serial + parallel paths -------------------
# Workers (in subinterpreters) can't ship `Diagnostic` instances back: each
# interpreter has its own `Diagnostic` class. We return plain tuples of shareable
# primitives so the same code path works whether the caller is the main
# interpreter or a subinterpreter via `Interpreter.call`.


def _check_file(file: Path, rules: list[Rule]) -> tuple:
    """Parse and lint `file`. Returns `(file_str, source, diag_tuples, err)`.

    `diag_tuples` is `tuple[(lineno, col, end_lineno, end_col, message, code)]`
    — picked to be cross-interpreter shareable. `err` is `None` on success,
    `("syntax", lineno, offset, msg)` for parse failures, or
    `("read", exc_type_name, str(exc))` for read failures.
    """
    file_str = str(file)
    try:
        source = file.read_text()
    except (OSError, UnicodeDecodeError) as e:
        return file_str, None, (), ("read", type(e).__name__, str(e))
    try:
        mod = parse_module(source)
    except SyntaxError as e:
        return file_str, source, (), ("syntax", e.lineno or 1, e.offset or 1, e.msg)
    diags = run(mod, rules)
    diag_tuples = tuple(
        (d.lineno, d.col_offset, d.end_lineno, d.end_col_offset, d.message, d.code)
        for d in diags
    )
    return file_str, source, diag_tuples, None


def _emit_result(result: tuple) -> int:
    """Print whatever `_check_file` produced. Returns the number of issues
    emitted — the caller derives the exit code from the running total."""
    file_str, source, diag_tuples, err = result
    if err is not None:
        kind = err[0]
        if kind == "syntax":
            _, lineno, offset, msg = err
            _print_diagnostic(file_str, lineno, offset, "invalid-syntax", msg)
            return 1
        # kind == "read"
        _, type_name, msg = err
        print(f"{file_str}: skipped ({type_name}: {msg})", file=sys.stderr)
        return 0
    suppressions = _parse_line_suppressions(source) if source else {}
    issues = 0
    for lineno, col, _end_lineno, _end_col, message, code in diag_tuples:
        if lineno in suppressions:
            codes = suppressions[lineno]
            if codes is None or code in codes:  # blanket, or this code listed
                continue
        _print_diagnostic(file_str, lineno, col, code, message)
        issues += 1
    return issues


# --- subinterpreter worker plumbing -------------------------------------
# Each subinterpreter gets its own `nib` module table, registry, and rule
# instances. We resolve the final select/ignore token lists in the main
# interpreter (so plugin import errors and registry-validation errors surface
# once) and ship them to workers; each worker re-runs `_load_plugins` +
# `_select_rules` against its own registry to build its rule instances. The
# whole worker lifecycle runs in one `Interpreter.exec` so the rule list is
# just a local in `_worker_loop` — no module-level state to manage.


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


def _run_serial(files: list[Path], rules: list[Rule]) -> int:
    """Check `files` in this interpreter, emitting each result as it's produced.
    Returns the total issue count."""
    return sum(_emit_result(_check_file(file, rules)) for file in files)


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


def _show_warning(message, category, filename, lineno, file=None, line=None):
    # Keep nib's rule-author warnings clean: no `__main__.py:42: UserWarning:`
    # prefix. Other warnings keep the default format.
    if category is UserWarning:
        print(f"{_c('nib warning:', '33')} {message}", file=sys.stderr)
    else:
        sys.stderr.write(
            warnings.formatwarning(message, category, filename, lineno, line)
        )


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
    subinterpreter, and workers import this module. This function only runs in
    the main interpreter, so the import stays clear of them.
    """
    import psutil

    logical = os.process_cpu_count() or 1
    physical = psutil.cpu_count(logical=False)
    return min(logical, max(physical, 4)) if physical else logical


def main() -> int:
    warnings.showwarning = _show_warning

    parser = _build_parser()
    args = parser.parse_args()

    if args.cmd == "rules":
        return _cmd_rules(args)

    if args.cmd != "check":
        parser.error(f"unknown command: {args.cmd}")

    if not args.path.exists():
        print(f"nib: path does not exist: {args.path}", file=sys.stderr)
        return EXIT_USAGE

    cfg = _config_nib()
    if (err := _load_plugins(args.plugins)) != EXIT_OK:
        return err

    if (err := _validate_registry(Rule._registry)) != EXIT_OK:
        return err

    select = (
        args.select if args.select is not None else list(cfg.get("select", []))
    ) + args.extend_select
    ignore = (
        args.ignore if args.ignore is not None else list(cfg.get("ignore", []))
    ) + args.extend_ignore
    rules = [cls() for cls in _select_rules(Rule._registry, select, ignore)]
    _validate_rules(rules)

    if not rules:
        return EXIT_OK  # nothing to enforce — skip the file walk entirely

    files = _collect_py_files(args.path, force_exclude=args.force_exclude)

    # Run files in parallel across subinterpreters, one worker per core
    # (see `_max_workers`). Scale down when there are few files, so small
    # runs stay cheap or serial. The `> 1` guard also skips `_max_workers`
    # (and its ~150ms psutil import) on small/serial runs.
    _FILES_PER_WORKER = 50
    by_files = max(1, len(files) // _FILES_PER_WORKER)
    n_workers = min(_max_workers(), by_files) if by_files > 1 else 1

    if n_workers > 1:
        issues = _run_parallel(files, n_workers, args.plugins, select, ignore)
    else:  # few files or single core
        issues = _run_serial(files, rules)

    print(f"Found {issues} issue{'s' if issues != 1 else ''}.")
    return EXIT_DIAGNOSTICS if issues else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
