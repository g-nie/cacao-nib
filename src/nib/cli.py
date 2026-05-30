import argparse
import ast
import functools
import importlib
import os
import re
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

from nib import Rule, cache, parallel
from nib.engine import _check_file, _select_rules, _warn
from nib.output import _color, _color_enabled

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


def _c(text: str, *codes: str) -> str:
    """Colour `text` with ANSI `codes` for stdout (diagnostics)."""
    return _color(text, *codes, enabled=_color_enabled(sys.stdout))


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
            if not attr.startswith("visit_") or not callable(getattr(cls, attr, None)):
                continue
            ast_name = attr.removeprefix("visit_")
            target = getattr(ast, ast_name, None)
            if not (isinstance(target, type) and issubclass(target, ast.AST)):
                _warn(f"{cls.__name__}.{attr} targets unknown ast class {ast_name!r}")


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


def _load_plugins(plugins_arg: list[str]) -> int:
    """Make cwd importable, then import plugins from `[tool.nib]` config +
    CLI flag. Returns `EXIT_OK`; `EXIT_DIAGNOSTICS` if a plugin has a syntax
    error (emits an `invalid-syntax` diagnostic and bails — there's no useful
    work to do without rules loaded); or `EXIT_USAGE` if a plugin won't import."""
    sys.path.insert(0, str(Path.cwd()))
    cfg = _config_nib()
    for mod_name in dict.fromkeys(list(cfg.get("plugins", [])) + plugins_arg):
        rules_before = len(Rule._registry)
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
        # A plugin that registers nothing is usually a wrong/misnamed module —
        # flag it rather than silently linting with no rules.
        if len(Rule._registry) == rules_before:
            _warn(f"plugin {mod_name!r} registered no rules")
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
        help="comma-separated rule codes/groups to run; replaces "
        "`[tool.nib] select` from pyproject.toml.",
    )
    check.add_argument(
        "--ignore",
        type=_parse_codes,
        default=None,
        metavar="CODES",
        help="comma-separated rule codes/groups to skip; replaces "
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
    check.add_argument(
        "--no-cache",
        action="store_true",
        help="don't read or write the result cache; check every file.",
    )
    check.add_argument(
        "--cache-dir",
        default=None,
        metavar="DIR",
        help=f"directory for the result cache "
        f"(default: ./{cache.DEFAULT_CACHE_DIR}, or ${cache.CACHE_DIR_ENV}).",
    )
    sub.add_parser(
        "rules",
        parents=[plugins_parent],
        help="list every registered rule, grouped by `group`",
    )

    return parser


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
    way to count physical cores. If psutil can't tell (returns `None`), fall back
    to the logical count. Imported lazily so serial runs (which never call this)
    don't pay psutil's import cost.
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
    # Final plugin list (`[tool.nib]` + CLI); workers re-import these by name.
    plugins = list(dict.fromkeys([*cfg.get("plugins", []), *args.plugins]))
    selected = _select_rules(Rule._registry, select, ignore)
    rules = [cls() for cls in selected]
    _validate_rules(rules)

    if not rules:
        return EXIT_OK  # nothing to enforce — skip the file walk entirely

    files = _collect_py_files(args.path, force_exclude=args.force_exclude)

    # Result cache: drop files unchanged since they last linted clean. The cache
    # file is keyed by nib version + enabled rule set, so a different ruleset
    # never reads another's hits. Skipped files had zero diagnostics last time
    # and (being unchanged) still do, so the issue total is unaffected.
    cached: dict[str, cache.FileData] = {}
    cache_path = None
    if not args.no_cache:
        cache_path = cache.cache_file(cache.ruleset_hash(selected), args.cache_dir)
        cached = cache.load(cache_path)
        files = [f for f in files if cache.is_changed(str(f), cached)]

    # Parallelise across cores when there's enough work; otherwise stay serial.
    # `_max_workers` only runs past the `chunks` guard.
    _FILES_PER_WORKER = 50
    chunks = len(files) // _FILES_PER_WORKER
    n_workers = min(_max_workers(), chunks) if chunks > 1 else 1

    if n_workers > 1:
        results = parallel._run_parallel(files, n_workers, plugins, select, ignore)
    else:  # few files or single core
        results = (_check_file(file, rules) for file in files)

    # Emit each result as it arrives, recording every file that linted clean so
    # the next run can skip it. Files with diagnostics (or a read/syntax error)
    # stay out of the cache and are re-checked every run until they're fixed.
    issues = 0
    for result in results:
        n = _emit_result(result)
        issues += n
        file_str, _source, _diags, err = result
        if cache_path is not None and err is None and n == 0:
            cache.record(file_str, cached)
    if cache_path is not None:
        cache.write(cache_path, cached)

    print(f"Found {issues} issue{'s' if issues != 1 else ''}.")
    return EXIT_DIAGNOSTICS if issues else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
