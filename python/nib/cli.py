import argparse
import importlib
import os
import sys
from pathlib import Path

from nib import Rule, parse_module, run
from nib.nib import collect_py_files

# Color only when stdout is a real terminal and NO_COLOR isn't set
# (https://no-color.org). Piped/redirected output stays plain.
_USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _c(text: str, *codes: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"


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
        help="import MODULE so its Rule subclasses register (repeatable)",
    )

    args = parser.parse_args()
    if args.cmd != "check":
        parser.error(f"unknown command: {args.cmd}")

    # Make cwd-relative packages (like demo/) importable without install.
    sys.path.insert(0, str(Path.cwd()))

    # Built-ins always loaded; --plugins modules imported for their side effects
    # (class definitions trigger Rule.__init_subclass__).
    importlib.import_module("nib.builtin_rules")
    for mod_name in args.plugins:
        try:
            importlib.import_module(mod_name)
        except ImportError as e:
            print(f"nib: failed to import --plugins {mod_name!r}: {e}", file=sys.stderr)
            return 2

    rules = [cls() for cls in Rule._registry]

    exit_code = 0
    for file_str in collect_py_files(str(args.path)):
        file = Path(file_str)
        try:
            source = file.read_text()
            mod = parse_module(source)
            diags = run(mod, rules)
        except (OSError, UnicodeDecodeError, RuntimeError) as e:
            # Don't let one bad file kill the whole run. Unsupported AST kinds
            # currently surface as RuntimeError from the parser wrappers.
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
