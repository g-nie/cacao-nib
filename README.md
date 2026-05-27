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
nib check                                  # lint current directory
nib check path/to/file.py
nib check --plugins nib_rules
nib check --select X001,DJ                 # only these codes/groups
nib check --ignore X002 --extend-ignore DJ # add to config's ignore list
```

`[tool.nib]` in `pyproject.toml` is read from the current directory:

```toml
[tool.nib]
plugins = ["nib_rules"]
select  = ["X001", "DJ"]   # optional — empty = run everything
ignore  = ["X002"]         # optional — wins over select on conflicts
```

CLI `--select` / `--ignore` *replace* their config counterparts;
`--extend-select` / `--extend-ignore` *add* to them. Each token is matched
exactly against either a rule's `code` or a rule's `group` — no string-prefix
fallback, so `--ignore X` won't accidentally take out everything starting
with `X`. Unknown tokens silently match nothing.

### In-repo rules (no install needed)

The plugin doesn't have to be a published package. nib prepends the current
directory to `sys.path` before importing plugins, so any importable module
sitting next to your `pyproject.toml` works — typical layout:

```
your_repo/
  pyproject.toml          # [tool.nib] plugins = ["nib_rules"]
  nib_rules/
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
    group = "X"  # optional — lets `--select X` pick up the whole family
    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "eval":
            return [Diagnostic(node, "no eval")]
```

Subclassing `Rule` auto-registers it. Define `visit_<AstName>` methods
mirroring `ast.NodeVisitor`. `code` identifies the individual rule; `group`
(optional) is a category label shared by related rules so users can
select/ignore them as a set. A name can't be used as both a `code` and a
`group` across loaded rules — nib refuses to start if it sees a collision.

## Suppressing diagnostics

```python
eval("x")              # noqa            — suppress every code on this line
eval("x")              # noqa: X001      — suppress only X001
eval("x"); print("y")  # noqa: X001,X002 — multiple codes
```

The comment must sit on the same line as the diagnostic's reported position
(for multi-line nodes, that's the start line). Case-insensitive.

## Roadmap

- run test coverage now

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
- Rule-testing utilities for plugin authors — a `nib.testing` helper that takes
  a source string + rule class and returns diagnostics, so plugins can write
  tight unit tests without spawning the CLI. Fixit ships something similar.
- Output formats — `--format json` for machine consumption and
  `--format github` for `::error file=...,line=...` CI annotations. Cheap to
  add, removes the need for wrapper scripts in CI.
- Expanded default diagnostic format with a source snippet and a
  caret span pointing at the offending tokens, plus a `help:` line when a rule
  has a suggested fix. Sketch:

  ```
  E711 Comparison to `None` should be `cond is None`
    --> demo/sample.py:20:13
     |
  19 | def needs_value(x):
  20 |     if x == None:  # DEMO005
     |             ^^^^
  21 |         return "missing"
  22 |     return x
     |
  help: Replace with `cond is None`
  ```

  Add `--format concise` to keep the current one-line `path:line:col: ...`
  output for grep/editor workflows. Needs `end_lineno`/`end_col_offset` to be
  populated (already on `Diagnostic`) and a source-snippet renderer.
- Severity levels on diagnostics (`error` / `warning` / `info`) with an
  exit-code policy (e.g. `--exit-zero`, or non-zero only on errors). Lets rule
  authors signal intent instead of every diagnostic being equally fatal.
- Result caching — skip files whose `(mtime, size, rule-set-hash)` is
  unchanged since the last run. More work, but the baseline ruff/mypy users
  now expect.
- `generic_visit` hook — let rules opt into "visit every node" without
  listing every `visit_<Name>`. Mirrors stdlib `ast.NodeVisitor`.
- Per-file lifecycle hooks (`enter_module(node)` / `leave_module(node)`) so
  rules can reset accumulated state cleanly instead of stashing it on `self`
  and hoping.
