import argparse
import ast
import contextlib
import functools
import importlib
import linecache
import os
import re
import sys
from pathlib import Path

from nib import Rule, cache, parallel
from nib.analysis import imported_among
from nib.utils import nib_version
from nib.engine import (
    FileError,
    _check_file,
    _file_targets,
    _find_config,
    _keep_deferred,
    _select_rules,
    _warn,
)
from nib.output import _color, _color_enabled

# Exit codes
EXIT_OK = 0
EXIT_DIAGNOSTICS = 1  # ran cleanly but found violations
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


def _c(text: str, *codes: str) -> str:
    """Colour `text` with ANSI `codes` for stdout (diagnostics)."""
    return _color(text, *codes, enabled=_color_enabled(sys.stdout))


def _collect_py_files(path: Path, *, force_exclude: bool = False) -> list[Path]:
    """Resolve a CLI path arg to a list of `.py` files.

    A directory is walked with `DEFAULT_EXCLUDE` dirs skipped in place (and sorted,
    so traversal order is deterministic). An explicit file path is checked even if
    it sits under an excluded dir — pass `force_exclude=True` to apply the excludes
    to explicitly-passed paths too.
    """
    if force_exclude and any(part in DEFAULT_EXCLUDE for part in path.parts):
        return []
    if path.is_file():
        return [path]
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = sorted(d for d in dirnames if d not in DEFAULT_EXCLUDE)
        files.extend(
            Path(dirpath) / name
            for name in sorted(n for n in filenames if n.endswith(".py"))
        )
    return files


def _collect_paths(paths: list[Path], *, force_exclude: bool = False) -> list[Path]:
    """Resolve several CLI path args (files and/or directories) to a flat list
    of `.py` files, de-duplicated — so overlapping args (or pre-commit passing
    the same file twice) lint each file once. (`dict.fromkeys` keeps first-seen
    order and drops repeats.)"""
    return list(
        dict.fromkeys(
            f
            for path in paths
            for f in _collect_py_files(path, force_exclude=force_exclude)
        )
    )


def _find_duplicate_codes(rule_classes) -> dict[str, list[str]]:
    """Codes claimed by more than one rule, as `{code: [rule names]}`.
    A clash would make `--select`/`--ignore` on that code ambiguous, so we treat it
    as a misconfiguration."""
    by_code: dict[str, list[str]] = {}
    for c in rule_classes:
        if c.code:
            by_code.setdefault(c.code, []).append(c.__name__)
    return {code: names for code, names in by_code.items() if len(names) > 1}


def _validate_registry(rule_classes) -> int:
    """Static checks on the loaded rule registry. Returns `EXIT_OK` or
    `EXIT_USAGE`, printing the first failure to stderr."""
    if collisions := _check_code_group_collisions(rule_classes):
        print(
            f"nib: name used as both a code and a group: {sorted(collisions)}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    # Rules with an empty `code` are unselectable and cannot be ignored.
    if missing := [c for c in rule_classes if not c.code]:
        names = ", ".join(c.__name__ for c in missing)
        print(f"nib: rule(s) without a `code` attribute: {names}", file=sys.stderr)
        return EXIT_USAGE
    if duplicates := _find_duplicate_codes(rule_classes):
        detail = "; ".join(
            f"{code!r}: {', '.join(names)}"
            for code, names in sorted(duplicates.items())
        )
        print(f"nib: code used by more than one rule: {detail}", file=sys.stderr)
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
            head, sep, ast_name = attr.partition("visit_")
            if head or not sep or not callable(getattr(cls, attr, None)):
                continue
            target = getattr(ast, ast_name, None)
            if not (isinstance(target, type) and issubclass(target, ast.AST)):
                _warn(f"{cls.__name__}.{attr} targets unknown ast class {ast_name!r}")


# `#` (possibly preceded by whitespace, followed by `noqa` and a word boundary)
# matches either bare or `:codes`. Case-insensitive on the keyword;
# Known limitation: a literal `# noqa` inside a string is indistinguishable
# from a real comment here — see `_parse_line_suppressions` for the trade-off.
_NOQA_RE = re.compile(
    r"#[ \t]*noqa(?![A-Za-z0-9_])[ \t]*(?::([^\n]*))?",
    re.IGNORECASE,
)


def _parse_line_suppressions(source: str) -> dict[int, set[str] | None]:
    """Scan `source` for `# noqa` directives. Returns `{lineno: codes}` where
    `codes is None` means "suppress every code on this line" and a set means
    "suppress only these codes". Bare `# noqa` is blanket; `# noqa:` with no
    codes is a no-op. The `noqa` keyword is case-insensitive; rule codes
    themselves are matched literally.

    False positive: a `# noqa` inside a string literal is treated as a real
    directive. We accept that to avoid a full `tokenize` pass on every file.
    """
    out: dict[int, set[str] | None] = {}
    for m in _NOQA_RE.finditer(source):
        lineno = source.count("\n", 0, m.start()) + 1
        rest = m.group(1)
        if rest is None:
            out[lineno] = None
            continue
        if codes := {c.strip() for c in rest.split(",") if c.strip()}:
            out[lineno] = codes
    return out


def _parse_codes(s: str) -> list[str]:
    return [p.strip() for p in s.split(",") if p.strip()]


def _check_code_group_collisions(rule_classes) -> set[str]:
    """Return any names used as both a `code` and a `group` across rules."""
    codes = {c.code for c in rule_classes if c.code}
    groups = {c.group for c in rule_classes if c.group}
    return codes & groups


def _load_plugins(plugins_arg: list[str]) -> int:
    """Make the project root importable, then import plugins from `[tool.nib]`
    config + CLI flag. Returns `EXIT_OK`; `EXIT_DIAGNOSTICS` if a plugin has a
    syntax error (emits an `invalid-syntax` diagnostic and exits — there's no
    useful work to do without rules loaded); or `EXIT_USAGE` if a plugin won't
    import."""
    root, cfg = _find_config()
    sys.path.insert(0, str(root))
    for mod_name in dict.fromkeys(list(cfg.get("plugins", [])) + plugins_arg):
        rules_before = len(Rule._registry)
        try:
            importlib.import_module(mod_name)
        except SyntaxError as e:
            _print_diagnostic_concise(
                e.filename or mod_name, e.lineno, e.offset, "invalid-syntax", e.msg
            )
            return EXIT_DIAGNOSTICS
        except ImportError as e:
            print(f"nib: failed to import plugin {mod_name!r}: {e}", file=sys.stderr)
            return EXIT_USAGE
        # A plugin that registers nothing is usually a wrong/misnamed module —
        # flag it rather than silently linting with no rules.
        if len(Rule._registry) == rules_before:
            _warn(f"plugin {mod_name!r} registered no rules")
    return EXIT_OK


def _print_diagnostic_concise(file, lineno: int, col: int, code: str, message: str):
    """Render a diagnostic line on stdout in the canonical `path:line:col`
    format. `lineno` and `col` are 1-based; `None` falls back to 1 so callers
    can pass `SyntaxError.lineno`/`offset` directly."""
    print(
        f"{file}:{lineno or 1}:{col or 1}: "
        f"{_c('error', '31')}[{_c(code, '1', '4')}] {message}"
    )


def _print_diagnostic_full(
    file, lineno: int, col: int, end_lineno, end_col, code: str, message: str
):
    """Print a diagnostic in the expanded format: a header, a `--> path:line:col`
    location, and the source line in context with a caret span underneath.
    Falls back to header + location when the source isn't available"""
    lineno = lineno or 1
    col = col or 1
    pipe = _c("|", "94", "1")
    header = f"{_c('error', '31')}[{_c(code, '1', '4')}] {message}"
    location = f"  {_c('-->', '94', '1')} {file}:{lineno}:{col}"
    lines = linecache.getlines(file)
    if not lines or not (1 <= lineno <= len(lines)):
        print(f"{header}\n{location}\n")
        return

    context = 2  # source lines shown on each side of the flagged line
    first = max(1, lineno - context)
    last = min(len(lines), lineno + context)
    gutter = len(str(last))  # width to right-align line numbers in the window
    sep = f"{' ' * gutter} {pipe}"  # blank gutter + pipe (top/bottom and caret rows)

    out = [header, location, sep]
    for n in range(first, last + 1):
        text = lines[n - 1].rstrip("\n")
        out.append(f"{_c(str(n).rjust(gutter), '94', '1')} {pipe} {text}")
        if n == lineno:
            if end_lineno == lineno and end_col is not None:
                width = max(1, end_col - col)
            else:
                width = max(1, len(text) - (col - 1))
            carets = _c("^" * width, "31", "1")
            out.append(f"{sep} {' ' * (col - 1)}{carets}")
    out.append(sep)
    out.append("")  # blank line between diagnostics
    print("\n".join(out))


def _summary_from_docstring(cls) -> str:
    """One-line summary from `cls.__doc__`: the first paragraph (up to a blank
    line) with its internal line breaks collapsed to single spaces. Keeps each
    rule on one line in `nib rules` without truncating a summary that happens to
    wrap across source lines; a very long paragraph is cut to 200 chars
    with a trailing `...`."""
    SUMMARY_MAX = 200
    first_para = (cls.__doc__ or "").strip().split("\n\n", 1)[0]
    summary = " ".join(first_para.split())
    if len(summary) > SUMMARY_MAX:
        summary = summary[:SUMMARY_MAX].rstrip() + "..."
    return summary


def _cmd_rules(args) -> int:
    if (err := _load_plugins(args.plugins)) != EXIT_OK:
        return err
    if (err := _validate_registry(Rule._registry)) != EXIT_OK:
        return err

    by_group: dict[str | None, list] = {}
    for cls in Rule._registry:
        by_group.setdefault(cls.group, []).append(cls)

    # Sorted groups, with the ungrouped rules listed last.
    group_order = sorted(g for g in by_group if g is not None)
    if None in by_group:
        group_order.append(None)

    for group in group_order:
        print(_c(group or "(no group)", "1"))
        for cls in sorted(by_group[group], key=lambda c: (c.code or "", c.__name__)):
            code = cls.code or "(no code)"
            line = f"  {_c(code, '1', '4')} {cls.__name__}"
            if summary := _summary_from_docstring(cls):
                line += f" — {summary}"
            print(line)
    return EXIT_OK


def _version_string() -> str:
    return f"nib {nib_version()}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nib")
    parser.add_argument(
        "--version",
        action="version",
        version=_version_string(),
        help="display nib's version",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # Shared `--plugins` flag attached to every subcommand that loads rules.
    plugins_parent = argparse.ArgumentParser(add_help=False)
    plugins_parent.add_argument(
        "--plugins",
        action="append",
        default=[],
        metavar="MODULE",
        help="also import MODULE (repeatable). Plugins listed in "
        "`[tool.nib] plugins = [...]` in pyproject.toml are loaded too.",
    )

    check = sub.add_parser("check", parents=[plugins_parent], help="lint .py files")
    check.add_argument(
        "paths",
        type=Path,
        nargs="*",
        default=[Path(".")],
        metavar="PATH",
        help="files or directories (default: current directory, recursive).",
    )
    check.add_argument(
        "--select",
        type=_parse_codes,
        default=None,
        metavar="CODES",
        help="comma-separated rule codes/groups to run; replaces "
        "`[tool.nib] select` from pyproject.toml.",
    )
    check.add_argument(
        "--ignore",
        type=_parse_codes,
        default=None,
        metavar="CODES",
        help="comma-separated rule codes/groups to skip; replaces "
        "`[tool.nib] ignore`. Ignore takes precedence over select.",
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
        help="apply directory excludes even to paths passed explicitly on the CLI.",
    )
    check.add_argument(
        "--format",
        choices=["full", "concise"],
        default="full",
    )
    check.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass the result cache (no read, no write); check every file.",
    )
    check.add_argument(
        "--cache-dir",
        default=None,
        metavar="DIR",
        help=f"directory for the result cache "
        f"(default: {cache.DEFAULT_CACHE_DIR} or ${cache.CACHE_DIR_ENV}).",
    )
    sub.add_parser(
        "rules",
        parents=[plugins_parent],
        help="list every registered rule, grouped by `group`",
    )
    sub.add_parser("version", help="display nib's version")

    return parser


def _is_suppressed(
    lineno: int, code: str, suppressions: dict[int, set[str] | None]
) -> bool:
    """Whether a `# noqa` on `lineno` silences `code` (blanket, or code listed)."""
    if lineno not in suppressions:
        return False
    codes = suppressions[lineno]
    return codes is None or code in codes


def _process_checked(
    result: tuple,
) -> tuple[list[cache.Diag], tuple[str, ...], list[cache.DeferredDiag]] | None:
    """Turn a `_check_file` result into `(diags, targets, deferred)` — its
    immediate diagnostics and deferred findings post-`# noqa`, plus the file's
    import targets. That triple is exactly what gets cached and what the run
    emits/resolves. Prints nothing except a read-error notice"""
    file_str, source, diag_tuples, err_wire, targets, deferred_tuples = result
    if err_wire is not None:
        err = FileError(*err_wire)
        if err.kind == "syntax":
            return (
                [(err.lineno, err.offset, None, None, "invalid-syntax", err.message)],
                (),
                [],
            )
        # err.kind == "read"
        print(f"{file_str}: skipped ({err.exc_type}: {err.message})", file=sys.stderr)
        return None
    suppressions = _parse_line_suppressions(source) if source else {}
    diags = [
        (lineno, col, end_lineno, end_col, code, message)
        for lineno, col, end_lineno, end_col, message, code in diag_tuples
        if not _is_suppressed(lineno, code, suppressions)
    ]
    deferred = [
        (lineno, col, end_lineno, end_col, code, message, module, keep)
        for lineno, col, end_lineno, end_col, message, code, module, keep in deferred_tuples
        if not _is_suppressed(lineno, code, suppressions)
    ]
    return diags, targets, deferred


def _emit_diags(file_str: str, diags, fmt: str = "full") -> int:
    """Print each `(lineno, col, end_lineno, end_col, code, message)` for
    `file_str` and return the count — renders freshly-checked files, cache hits,
    and surviving deferred findings alike.
    `fmt` picks the layout: `concise` is the one-line form, `full` adds a source
    snippet with a caret span."""
    for lineno, col, end_lineno, end_col, code, message in diags:
        if fmt == "concise":
            _print_diagnostic_concise(file_str, lineno, col, code, message)
        else:
            _print_diagnostic_full(
                file_str, lineno, col, end_lineno, end_col, code, message
            )
    return len(diags)


_FILES_PER_WORKER = 50


def _worker_count(files) -> int:
    """How many subinterpreter workers to use for `files`: scale with the file
    count but only once there's enough work to justify the startup cost, capped
    at `_max_workers()`. 1 means run serially in-process."""
    chunks = len(files) // _FILES_PER_WORKER
    return min(_max_workers(), chunks) if chunks > 1 else 1


def _dispatch_checks(files, rules, plugins, select, ignore):
    """Yield `_check_file` results for `files`: across subinterpreters when
    there's enough work to justify their startup cost, else serially in-process.
    Either way results come back in `files` order."""
    n_workers = _worker_count(files)
    if n_workers > 1:
        return parallel._run_parallel(files, n_workers, plugins, select, ignore)
    return (_check_file(f, rules) for f in files)


def _emit(
    files, cache_hits, results, session: cache.Session, fmt: str = "full"
) -> tuple[int, list[tuple[str, ...]], list[tuple[str, list[cache.DeferredDiag]]]]:
    """Walk `files` in order: replay a cache hit inline, else pull the next
    checked result (misses arrive in `files` order). Immediate findings stream as
    we go and each file's `(diags, targets, deferred)` is recorded for next run.

    Returns `(issues, targets_per_file, deferred_holds)`. Deferred findings aren't
    resolved here — the caller does that via `_resolve_deferred` once it has the
    project-wide import picture, since a finding may hinge on a file `files`
    doesn't include.

    `closing` matters for the parallel branch: we pull one result per miss and
    never exhaust the generator, so closing it runs the cleanup that releases the
    worker subinterpreters."""
    issues = 0
    targets_per_file: list[tuple[str, ...]] = []
    deferred_holds: list[tuple[str, list[cache.DeferredDiag]]] = []
    with contextlib.closing(results):
        miss_results = iter(results)
        for f in files:
            file_str = str(f)
            if file_str in cache_hits:
                diags, targets, deferred = cache_hits[file_str]
            else:
                processed = _process_checked(next(miss_results))
                if processed is None:  # read error - not cacheable
                    continue
                diags, targets, deferred = processed
                session.record(file_str, tuple(diags), targets, tuple(deferred))
            issues += _emit_diags(file_str, diags, fmt)
            targets_per_file.append(targets)
            if deferred:
                deferred_holds.append((file_str, deferred))
    return issues, targets_per_file, deferred_holds


def _package_scan_root(path: Path) -> Path:
    """The directory to scan for reachability when `path` is linted: ascend out of
    any enclosing package to its parent, so a file deep in `pkg/sub/` resolves
    against the whole `pkg` tree. A path outside a package scans its own dir."""
    directory = path if path.is_dir() else path.parent
    while (directory / "__init__.py").is_file():
        directory = directory.parent
    return directory


def _reachability_targets(
    paths: list[Path], checked: set[str], session: cache.Session, force_exclude: bool
) -> list[tuple[str, ...]]:
    """Import targets for every project file *outside* `checked`, so a deferred
    verdict resolves against the whole project, not just the files passed. Scans
    the package tree(s) enclosing the path arguments, reusing cached targets for
    unchanged files and parsing the rest fresh (no rules run)."""
    extra: list[tuple[str, ...]] = []
    seen = set(checked)
    for root in {_package_scan_root(p) for p in paths}:
        for f in _collect_py_files(root, force_exclude=force_exclude):
            abspath = os.path.abspath(str(f))
            if abspath in seen:
                continue
            seen.add(abspath)
            targets = session.cached_targets(str(f))
            extra.append(_file_targets(f) if targets is None else targets)
    return extra


def _resolve_deferred(
    deferred_holds: list[tuple[str, list[cache.DeferredDiag]]],
    reachable: list[tuple[str, ...]],
    fmt: str = "full",
) -> int:
    """Resolve held deferred findings against `reachable` (every project file's
    import targets) and print the survivors in file order. Returns the count.

    A `DeferredDiag` is `(lineno, col, end_lineno, end_col, code, message, module,
    keep)`: the first six fields are the printable diagnostic, the last two gate it
    on `module`'s reachability."""
    gated = {d[6] for _f, deferred in deferred_holds for d in deferred}
    imported = imported_among(gated, reachable)
    issues = 0
    for file_str, deferred in deferred_holds:
        survivors = [d[:6] for d in deferred if _keep_deferred(d[6], d[7], imported)]
        issues += _emit_diags(file_str, survivors, fmt)
    return issues


@functools.cache
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

    `psutil.cpu_count(logical=False)` seems to be the only reliable cross-platform
    way to count physical cores. If psutil can't tell, fall back to the logical count.
    Imported lazily so serial runs (which never call this) don't pay psutil's import cost.
    """
    import psutil

    logical = os.process_cpu_count() or 1
    physical = psutil.cpu_count(logical=False)
    return min(logical, max(physical, 4)) if physical else logical


def main() -> int:
    try:
        return _run_cli()
    except KeyboardInterrupt:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)  # 128 + SIGINT


def _run_cli() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.cmd == "version":
        print(_version_string())
        return EXIT_OK

    if args.cmd == "rules":
        return _cmd_rules(args)

    if args.cmd != "check":
        parser.error(f"unknown command: {args.cmd}")

    for path in args.paths:
        if not path.exists():
            print(f"nib: path does not exist: {path}", file=sys.stderr)
            return EXIT_USAGE

    if (err := _load_plugins(args.plugins)) != EXIT_OK:
        return err

    if (err := _validate_registry(Rule._registry)) != EXIT_OK:
        return err

    root, cfg = _find_config()
    select = (
        args.select if args.select is not None else list(cfg.get("select", []))
    ) + args.extend_select
    ignore = (
        args.ignore if args.ignore is not None else list(cfg.get("ignore", []))
    ) + args.extend_ignore
    # Final plugin list (`[tool.nib]` + CLI); workers re-import these by name.
    plugins = list(dict.fromkeys([*cfg.get("plugins", []), *args.plugins]))
    selected = _select_rules(Rule._registry, select, ignore)
    rules = [cls() for cls in selected]
    _validate_rules(rules)

    if not rules:
        return EXIT_OK  # nothing to enforce - skip the file walk entirely

    files = _collect_paths(args.paths, force_exclude=args.force_exclude)

    # Result cache: replay stored diagnostics for files unchanged since last run
    # and only actually check the misses. The session is keyed by nib version +
    # enabled rule set, so a different ruleset never reads another's hits. The
    # default cache lives under the project root. It also keeps each file's import
    # targets, which the reachability scan reuses to resolve cross-file findings
    # without re-parsing unchanged files.
    session = cache.Session.open(
        selected, args.cache_dir, enabled=not args.no_cache, root=root
    )
    cache_hits, cache_misses = session.partition(files)

    results = _dispatch_checks(cache_misses, rules, plugins, select, ignore)
    issues, targets_per_file, deferred_holds = _emit(
        files, cache_hits, results, session, args.format
    )
    if deferred_holds:
        # A cross-file rule fired: resolve its findings against the whole project,
        # not just the files passed this run — scan the enclosing package tree(s)
        # for the imports those verdicts hinge on (cheap, and skipped when no
        # deferred finding is held).
        checked = {os.path.abspath(str(f)) for f in files}
        extra = _reachability_targets(args.paths, checked, session, args.force_exclude)
        issues += _resolve_deferred(
            deferred_holds, targets_per_file + extra, args.format
        )
    session.flush()

    print(f"Found {issues} issue{'s' if issues != 1 else ''}.")
    return EXIT_DIAGNOSTICS if issues else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
