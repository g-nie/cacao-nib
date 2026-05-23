import argparse
import importlib
import os
import sys
import tomllib
from pathlib import Path

import nib.builtin_rules  # noqa: F401  -- imported to register built-in Rules
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


def _config_plugins() -> list[str]:
    """Read `[tool.nib] plugins = [...]` from cwd's pyproject.toml, if any."""
    pyproject = Path.cwd() / "pyproject.toml"
    if not pyproject.is_file():
        return []
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    return list(data.get("tool", {}).get("nib", {}).get("plugins", []))


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

    args = parser.parse_args()
    if args.cmd != "check":
        parser.error(f"unknown command: {args.cmd}")

    if not args.path.exists():
        print(f"nib: path does not exist: {args.path}", file=sys.stderr)
        return 2

    sys.path.insert(0, str(Path.cwd()))

    for mod_name in dict.fromkeys(_config_plugins() + args.plugins):
        try:
            importlib.import_module(mod_name)
        except ImportError as e:
            print(f"nib: failed to import plugin {mod_name!r}: {e}", file=sys.stderr)
            return 2

    rules = [cls() for cls in Rule._registry]

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
