import argparse
import ast
import functools
import importlib
import io
import os
import sys
import tokenize
import tomllib
from collections.abc import Iterator
from pathlib import Path

from nib import Rule, parse_module, run

# Exit codes
EXIT_OK = 0
EXIT_DIAGNOSTICS = 1  # lint ran cleanly but found violations
EXIT_USAGE = 2  # bad invocation / config / unloadable plugin

# Directory names always pruned during a recursive walk — same set ruff uses
# by default. An explicit path on the CLI bypasses this (unless --force-exclude).
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
    bypass that pruning by default (ruff parity) — pass `force_exclude=True`
    to apply the excludes even to explicitly-passed paths, the mode pre-commit
    hooks want so config excludes aren't silently ignored.
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
    """Warn (to stderr) about visit_* methods targeting unknown ast classes.

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
                print(
                    f"nib warning: {cls.__name__}.{attr} targets unknown ast "
                    f"class {ast_name!r}",
                    file=sys.stderr,
                )


def _parse_line_suppressions(source: str) -> dict[int, set[str] | None]:
    """Scan `source` for `# noqa` comments. Returns `{lineno: codes}` where
    `codes is None` means "suppress every code on this line" and a set means
    "suppress only these codes". Bare `# noqa` is blanket; `# noqa:` with no
    codes is a no-op (the colon signals "I'm listing codes" — empty list
    means none). The `noqa` keyword is case-insensitive (ruff/flake8 parity);
    rule codes themselves are matched literally. Bad/untokenizable sources
    yield `{}`.
    """
    out: dict[int, set[str] | None] = {}
    # Fast path: tokenizing every file is expensive. If no case variant of
    # "noqa" appears in the source, no directive can be present — skip the
    # tokenize entirely.
    if "noqa" not in source.lower():
        return out
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type != tokenize.COMMENT:
                continue
            # tok.string for a COMMENT always starts with '#'.
            body = tok.string[1:].lstrip(" \t")
            if len(body) < 4 or body[:4].lower() != "noqa":
                continue
            rest = body[4:].lstrip(" \t")
            if not rest:
                out[tok.start[0]] = None  # bare `# noqa` → blanket
                continue
            if rest[0] != ":":
                continue  # e.g. `# noqand` — not a directive
            codes = {c.strip() for c in rest[1:].split(",") if c.strip()}
            if codes:
                out[tok.start[0]] = codes
            # else: `# noqa:` with empty list — silently ignored.
    except tokenize.TokenError:
        pass
    return out


def _filter_suppressed(diags, suppressions: dict[int, set[str] | None]):
    kept = []
    for d in diags:
        codes = suppressions.get(d.lineno, "miss")
        if codes == "miss":
            kept.append(d)
            continue
        if codes is None or d.code in codes:
            continue
        kept.append(d)
    return kept


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
    CLI flag. Returns `EXIT_OK`, or `EXIT_USAGE` if any import fails."""
    sys.path.insert(0, str(Path.cwd()))
    cfg = _config_nib()
    for mod_name in dict.fromkeys(list(cfg.get("plugins", [])) + plugins_arg):
        try:
            importlib.import_module(mod_name)
        except ImportError as e:
            print(f"nib: failed to import plugin {mod_name!r}: {e}", file=sys.stderr)
            return EXIT_USAGE
    return EXIT_OK


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
            line = f"  {_c(code, '1', '4')}  {cls.__name__}"
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


def main() -> int:
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

    exit_code = EXIT_OK
    for file in _collect_py_files(args.path, force_exclude=args.force_exclude):
        try:
            source = file.read_text()
            mod = parse_module(source)
            diags = run(mod, rules)
        except (OSError, UnicodeDecodeError, SyntaxError) as e:
            print(f"{file}: skipped ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        if diags:
            if suppressions := _parse_line_suppressions(source):
                diags = _filter_suppressed(diags, suppressions)
            for d in diags:
                print(
                    f"{file}:{d.lineno}:{d.col_offset}: "
                    f"{_c('error', '31')}[{_c(d.code, '1', '4')}] {d.message}"
                )
                exit_code = EXIT_DIAGNOSTICS
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
