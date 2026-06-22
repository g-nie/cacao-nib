# cacao-nib (nib)

A pluggable framework for writing Python lint rules on top of the stdlib
`ast` module. Ships zero builtin rules: rules live in plugin packages you write or install.

Plugins are loaded explicitly via `[tool.nib] plugins = [...]` in `pyproject.toml`,
so what runs is exactly what you list.

## Installation

Requires Python 3.14+.

```sh
python -m pip install cacao-nib
```

## Usage

```sh
nib check                                   # lint current directory (recursive)
nib check path/to/file.py
nib check --plugins nib_rules
nib check --select X001,DJ                  # only these codes/groups
nib check --ignore X002 --extend-ignore DJ  # ignore X002, then also skip the DJ group
nib check --format concise                  # one-line output
```

Run `nib --help` for the full set of options; each subcommand takes `--help`
too (e.g. `nib check --help`).

`[tool.nib]` is read from the nearest `pyproject.toml`, found by searching up
from the current directory (a `pyproject.toml` without a `[tool.nib]` table
is skipped and the search continues in the parent). So running `nib` from a
subdirectory still picks up the project's config:

```toml
[tool.nib]
plugins = ["nib_rules"]
select  = ["X001", "DJ"]   # optional - empty = run everything
ignore  = ["X002"]         # optional
```

- `plugins` - the rule modules to load. With none, nib has no rules and every file is "clean".
- `select` - codes/groups to run; omitted or empty runs every loaded rule.
- `ignore` - codes/groups to skip; takes precedence over `select` on overlap.

CLI `--select` / `--ignore` replace their config counterparts;
`--extend-select` / `--extend-ignore` add to them. Each token is matched
exactly against either a rule's `code` or a rule's `group`.

Each entry in `plugins` is just an importable module name: nib imports it with
[`importlib.import_module`](https://docs.python.org/3/library/importlib.html#importlib.import_module)
and the `Rule` subclasses defined there register themselves. So a plugin can be a
third-party package you `pip install`, or a module that  lives in your own repo.
Anything that resolves under a normal import works: list several
(`plugins = ["a", "b"]`, or repeat `--plugins`), or point at a package. A package
only runs its `__init__.py`, though, so re-export the rule submodules there (or name
them directly, e.g. `nib_rules.style`).

### In-repo rules (no install needed)

The plugin doesn't have to be a published package. nib prepends the project
root (the directory of the `pyproject.toml` it found) to `sys.path` before
importing plugins, so any importable module sitting next to your
`pyproject.toml` works from anywhere in the tree. Example layout:

```
your_repo/
  pyproject.toml  # [tool.nib] plugins = ["nib_rules"]
  nib_rules/
    __init__.py   # your Rule subclasses live here
  src/your_app/...
```

Then `nib check` picks them up from `your_repo` or any subdirectory of it.

## Writing a rule

A rule is a Python class with `visit_<AstName>` methods:

```python
from nib import Rule, Diagnostic, ast

class NoEval(Rule):
    code = "X001"
    group = "X"   # optional. `--select X` then picks every rule in this group

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "eval":
            return [Diagnostic(node, "no eval")]
```

Subclassing `Rule` auto-registers it; the `visit_<AstName>` methods mirror
[`ast.NodeVisitor`](https://docs.python.org/3/library/ast.html#ast.NodeVisitor)'s
dispatch. `code` identifies the individual rule; `group` (optional) is a category label
shared by related rules so users can  select/ignore them as a set.

A visitor can also return an `UnimportedDiagnostic`/`ImportedDiagnostic` to defer a
finding to whether its module is imported anywhere in the run (cross-file rules).

### Per-file state: `enter_module` / `leave_module`

One `Rule` instance is built once and reused for every file in the run, so any
state you store on `self` carries over from one file to the next. Two optional hooks
give a clean per-file boundary, each receiving the module's root node:

- `enter_module(node)` runs before any visitor for the file. Reset per-file
  state here.
- `leave_module(node)` runs after every visitor for the file. The place to emit
  a summary that needs the whole module.

Both default to no-ops and may return diagnostics just like a visitor.

```python
class TooManyFunctions(Rule):
    code = "X999"

    def enter_module(self, node):
        self.defs = 0  # reset for each file

    def visit_FunctionDef(self, node):
        self.defs += 1

    def leave_module(self, node):
        if self.defs > 50:
            return [Diagnostic(node, f"module has {self.defs} functions, max 50")]
```

`enter_module` is the explicit per-file setup hook; `visit_Module` also works but
fires inside the walk.

## Suppressing diagnostics

```python
eval("x")              # noqa            — suppress every code on this line
eval("x")              # noqa: X001      — suppress only X001
eval("x"); print("y")  # noqa: X001,X002 — multiple codes
```

The comment must sit on the same line as the diagnostic's reported position
(for multi-line nodes, that's the start line).

## Performance

On large runs nib checks files in parallel across CPU cores using
[subinterpreters](https://docs.python.org/3/library/concurrent.interpreters.html).
Small runs stay single-process to skip the startup overhead.
Repeat runs are cached on top of that (see below).

nib is benchmarked against a full Django checkout; the results are
[tracked here](https://g-nie.github.io/cacao-nib/dev/bench/).

## Caching

A re-run skips files that haven't changed since they last passed, so repeated
`nib` invocations are very fast on an unchanged tree. The cache lives in
`./.cacao_nib_cache`, keyed in two parts: the nib version and enabled rule set
select the cache file and within it each file is keyed by path and replayed only
while unchanged. Both clean and flagged files are cached: until a file changes, its
diagnostics (none, or last run's findings) are replayed without re-parsing.

```sh
nib check --no-cache             # ignore the cache: check every file
nib check --cache-dir path/to/c  # use a different cache directory
```

## Pre-commit

nib ships a [pre-commit](https://pre-commit.com) hook. Add to your `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/g-nie/cacao-nib
    rev: v0.2.0
    hooks:
      - id: nib
```
