# cacao-nib

A pluggable framework for writing Python lint rules on top of the stdlib
`ast` module. Ships zero builtin rules: rules live in plugin packages you write or install.

Plugins are loaded explicitly via `[tool.nib] plugins = [...]` in `pyproject.toml`
(mypy-style), so what runs is exactly what you list. CLI is `nib`; a rule is a Python class with
`visit_<AstName>` methods.

## Install

```sh
python -m pip install cacao-nib
```

## Usage

```sh
nib check                # lint current directory
nib check path/to/file.py
nib check --plugins custom_rules
```

`[tool.nib]` in `pyproject.toml` is read from the current directory:

```toml
[tool.nib]
plugins = ["custom_rules"]
```

### In-repo rules (no install needed)

The plugin doesn't have to be a published package. nib prepends the current
directory to `sys.path` before importing plugins, so any importable module
sitting next to your `pyproject.toml` works — typical layout:

```
your_repo/
  pyproject.toml          # [tool.nib] plugins = ["custom_rules"]
  custom_rules/
    __init__.py           # your Rule subclasses live here
  src/your_app/...
```

Then `cd your_repo && nib check` picks them up. No `pip install -e .`, no
`[build-system]` block, no entry-point registration.

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
- Suppression comments — decide the syntax later. Candidates:
  - `# noqa` / `# noqa: X001,X002` (flake8/ruff style, widely recognized)
  - `# nib: ignore[X001]` / `# nib: ignore-file` (namespaced, explicit)
  - `# type: ignore`-style `# nib: ignore` (terse but ambiguous)
  Also needs a file-level form and a way to report unused suppressions.
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
- Minimal semantic model — an imports table per module (mapping local names to
  their fully-qualified origin, including `import x as y` and `from a.b import c`).
  Rules currently can't reliably answer "is this `Call` really
  `some.library.target`?" without re-implementing the walk themselves. Build
  it once, expose to rules.
- Project-wide index + `self.project` on rules. Once per-module imports tables
  exist, aggregate them (plus top-level defs) into a project index built by a
  scan pass that runs before any rule fires. Framework injects it on every rule
  as `self.project`, so cross-file rules stay normal per-file visitors that
  just query the index when they need it — no separate `ProjectRule` base
  class or `visit_project()` hook. Motivating shape: rules that need to ask
  "is this symbol referenced anywhere else?" or "is this module ever
  imported from an entry point?" — the kind of check that fails silently
  without whole-project visibility.
- Autofixes. The shape of this depends on a tradeoff worth flagging early:
  stdlib `ast` discards whitespace, comments, and exact formatting, so we
  can't round-trip source through it (Fixit avoids this by building on
  LibCST; ruff sidesteps it by working on byte ranges). Three viable paths:
  - **Text-range edits (ruff-style).** `Diagnostic` gains an optional `fix`
    field carrying one or more `(start_offset, end_offset, replacement)`
    edits. Rules compute these from `node.lineno`/`col_offset`/
    `end_lineno`/`end_col_offset` (already populated by stdlib ast). CLI
    applies non-overlapping edits in reverse order. Keeps the framework
    pure-Python and lets rules stay simple, but every rule must hand-build
    its replacement string — no structural editing helpers.
  - **CST detour.** Pull in LibCST (or `ast` + `tokenize` for whitespace)
    only when a rule opts into fixing. Heavier dependency, but rule authors
    get safe structural edits.
  - **`ast.unparse` reformat.** Lossy — strips comments, normalizes
    formatting. Only acceptable for whole-file regeneration tools, not a
    linter. Mentioned so it's explicitly ruled out.
  Recommended starting point: text-range edits, gated behind `--fix` /
  `--fix-only`, with a `--diff` preview mode. Revisit CST if rules start
  needing structural rewrites that text edits can't express cleanly.
- `--strict` mode that turns per-file skips into failures.
- Final summary line (`N files checked, M skipped, K issues`).
- Rule-testing utilities for plugin authors — a `nib.testing` helper that takes
  a source string + rule class and returns diagnostics, so plugins can write
  tight unit tests without spawning the CLI. Fixit ships something similar.
- Output formats — `--format json` for machine consumption and
  `--format github` for `::error file=...,line=...` CI annotations. Cheap to
  add, removes the need for wrapper scripts in CI.
- Severity levels on diagnostics (`error` / `warning` / `info`) with an
  exit-code policy (e.g. `--exit-zero`, or non-zero only on errors). Lets rule
  authors signal intent instead of every diagnostic being equally fatal.
- Parallel file processing via `multiprocessing.Pool`. Rules are already
  per-file-independent, so this is mostly plumbing — worthwhile once rule sets
  grow.
- Result caching — skip files whose `(mtime, size, rule-set-hash)` is
  unchanged since the last run. More work, but the baseline ruff/mypy users
  now expect.
- `generic_visit` hook — let rules opt into "visit every node" without
  listing every `visit_<Name>`. Mirrors stdlib `ast.NodeVisitor`.
- Per-file lifecycle hooks (`enter_module(node)` / `leave_module(node)`) so
  rules can reset accumulated state cleanly instead of stashing it on `self`
  and hoping.
