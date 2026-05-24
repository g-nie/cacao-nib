import argparse
import ast
import importlib
import os
import sys
import tomllib
from pathlib import Path

from nib import Rule, parse_module, run

# Color only when stdout is a real terminal and NO_COLOR isn't set
# (https://no-color.org). Piped/redirected output stays plain.
_USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _c(text: str, *codes: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


def _collect_py_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    return sorted(path.rglob("*.py"))


def _validate_rules(rules: list[Rule]) -> None:
    """Warn (to stderr) about visit_* methods targeting unknown ast classes.

    A `visit_<Name>` method is valid if `ast.<Name>` exists and subclasses
    `ast.AST` — the same contract `ast.NodeVisitor` dispatches on.
    """
    for rule in rules:
        cls = type(rule)
        if not cls.code:
            print(
                f"nib warning: {cls.__name__} has no `code` attribute (or it's empty)."
                "Diagnostics will print as 'error[]' and the rule can't be "
                "selected/ignored individually",
                file=sys.stderr,
            )
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="nib")
    sub = parser.add_subparsers(dest="cmd", required=True)

    check = sub.add_parser("check", help="lint .py files")
    check.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=Path("."),
        help="file or directory (default: current directory, recursive)",
    )
    check.add_argument(
        "--plugins",
        action="append",
        default=[],
        metavar="MODULE",
        help="also import MODULE (repeatable). Plugins listed in "
        "`[tool.nib] plugins = [...]` in cwd's pyproject.toml are loaded too.",
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

    args = parser.parse_args()
    if args.cmd != "check":
        parser.error(f"unknown command: {args.cmd}")

    if not args.path.exists():
        print(f"nib: path does not exist: {args.path}", file=sys.stderr)
        return 2

    sys.path.insert(0, str(Path.cwd()))

    cfg = _config_nib()
    for mod_name in dict.fromkeys(list(cfg.get("plugins", [])) + args.plugins):
        try:
            importlib.import_module(mod_name)
        except ImportError as e:
            print(f"nib: failed to import plugin {mod_name!r}: {e}", file=sys.stderr)
            return 2

    collisions = _check_code_group_collisions(Rule._registry)
    if collisions:
        print(
            f"nib: name used as both a code and a group: {sorted(collisions)}",
            file=sys.stderr,
        )
        return 2

    select = (
        args.select if args.select is not None else list(cfg.get("select", []))
    ) + args.extend_select
    ignore = (
        args.ignore if args.ignore is not None else list(cfg.get("ignore", []))
    ) + args.extend_ignore
    rules = [cls() for cls in _select_rules(Rule._registry, select, ignore)]
    _validate_rules(rules)

    if not rules:
        return 0  # nothing to enforce — skip the file walk entirely

    exit_code = 0
    for file in _collect_py_files(args.path):
        try:
            source = file.read_text()
            mod = parse_module(source)
            diags = run(mod, rules)
        except (OSError, UnicodeDecodeError, SyntaxError) as e:
            print(f"{file}: skipped ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        for d in diags:
            print(
                f"{file}:{d.lineno}:{d.col_offset}: "
                f"{_c('error', '31')}[{_c(d.code, '1', '4')}] {d.message}"
            )
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
