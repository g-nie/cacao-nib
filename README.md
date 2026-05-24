# cacao-nib

A small Python linter. CLI is `nib`; rules are plain Python classes built on
the stdlib `ast` module.

## Install

```sh
python -m pip install cacao-nib
```

## Usage

```sh
nib check                # lint current directory
nib check path/to/file.py
nib check --plugins my_rules_pkg
```

`[tool.nib]` in `pyproject.toml` is read from the current directory:

```toml
[tool.nib]
plugins = ["my_rules_pkg"]
```

## Writing a rule

```python
from nib import Rule, Diagnostic, ast

class NoEval(Rule):
    code = "X001"
    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "eval":
            return [Diagnostic(node, "no eval")]
```

Subclassing `Rule` auto-registers it. Define `visit_<AstName>` methods
mirroring `ast.NodeVisitor`.

## Roadmap

- `--select` / `--ignore` to filter by rule code, with `[tool.nib]` equivalents
  in `pyproject.toml`. CLI flags replace the config values; `--extend-select` /
  `--extend-ignore` will add to them.
- Gitignore-aware file discovery (currently `pathlib.rglob` descends into
  `.venv/`, `.git/`, `__pycache__/`, `node_modules/`, etc.). Either bolt on
  per-extension excludes or shell out to a gitignore-respecting walker. Pair
  with `--no-respect-gitignore` for parity with ruff/ripgrep.
- Structured parse-error diagnostics. `ast.parse` raises `SyntaxError`; we
  currently skip the file and continue. Emit a single `E000`-style diagnostic
  instead of a stderr line, so it shows up in the regular output stream.
- Switch rule-author warnings from `print(..., file=sys.stderr)` to
  `warnings.warn(...)` — the idiomatic Python channel for "you're using the API
  wrong, but I'll do my best". Supports dedup, filtering, and capture via
  `warnings.catch_warnings()` for free. CLI would install a
  `warnings.showwarning` shim (~5 lines) to keep output clean (no
  `__main__.py:42: UserWarning:` noise). Worth doing once nib gets embedded
  somewhere other than the CLI (editor plugin, etc.).
- `--strict` mode that turns per-file skips into failures.
- Final summary line (`N files checked, M skipped, K issues`).
