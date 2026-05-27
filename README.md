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

- Check-loop performance — make a single cold `nib check` run faster
  without relying on caching. Profile the parse + walk + dispatch pipeline
  and look for wins (parallelism across files, cheaper per-node dispatch,
  fewer redundant traversals when many rules visit the same node types).
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
- Result caching — skip files whose `(mtime, size, rule-set-hash)` is
  unchanged since the last run. More work, but the baseline ruff/mypy users
  now expect.
