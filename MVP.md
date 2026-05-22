# cacao-nib MVP plan

**Goal:** prove the architecture end-to-end with a single trivial rule. Python-only. No tests. No cross-file. No semantic model yet.

**Naming:** distribution is `cacao-nib` (PyPI), but the Python import name is `nib` and the CLI binary is `nib`. There's a stale `nib` package on PyPI (an abandoned static site generator); we accept the small collision risk since users rarely have it installed.

## Stack

- `pyo3` + `maturin` for the Rust↔Python boundary
- `tree-sitter` + `tree-sitter-python` for parsing
- `clap` for the CLI

## Milestones

### 1. Skeleton

- `maturin new` confirmed; `import nib` works from Python. (Wheel ships a top-level `nib/` package; `pyproject.toml` sets `module-name = "nib.nib"` and `[project.scripts] nib = "nib.cli:main"`.)
- Add `tree-sitter` deps; parse one hardcoded Python file in Rust and print the root node kind. Throw this code away after.

### 2. The 5 AST wrappers

Write by hand, no macro yet. One `#[pyclass]` each:

- `Module` — `body: list`
- `Call` — `func`, `args`, `keywords`, `lineno`, `col_offset`
- `Name` — `id`, `lineno`, `col_offset`
- `Attribute` — `value`, `attr`, `lineno`, `col_offset`
- `Constant` — `value`, `lineno`, `col_offset`

Each wraps a `tree_sitter::Node` + `Arc<[u8]>` source. Getters are lazy. Add one helper `wrap_expr(node)` that dispatches by `node.kind()` to the right wrapper — start with just these 5; panic on unknown kinds with a clear "unsupported node kind" message so you know what to add next.

### 3. Visitor dispatcher

- Define a Python-side `Rule` base class (in `nib/__init__.py` shipped with the wheel). Uses `__init_subclass__` to append every subclass to a class-level `Rule._registry` at class-definition time — defining a `Rule` subclass *is* registering it; no decorator, no module-level `rules = [...]` list, no `dir()` scan.
- At rule-registration time, introspect: `[m for m in dir(rule) if m.startswith("visit_")]` → build a `HashSet<&'static str>` of kinds the rule cares about (map `visit_Call` → tree-sitter kind `"call"`).
- Rust walks the tree with a cursor. On each node, if its kind is in the set, wrap it and call `rule.visit_<Kind>(wrapped_node)`. Collect returned `Diagnostic`s.

### 4. Diagnostic type

- `#[pyclass] Diagnostic { code, message, line, col, end_line, end_col }`.
- Python-side constructor takes `(node, message)` and pulls span from the node.

### 5. First rule — built-in (proves the dispatcher)

Ship one rule inline as an example:

```python
from nib import Rule, Diagnostic, ast

class NoEval(Rule):
    code = "X001"
    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "eval":
            return [Diagnostic(node, "no eval")]
```

### 6. Third-party rule (proves extensibility)

A separate package — not part of `cacao-nib` — should be able to define its own rule and have `cacao-nib` load it. This is the whole point of the project, so prove it works end-to-end in the MVP.

No wrapper class, no explicit registration list. Just subclass `Rule` and the `__init_subclass__` hook puts it on `Rule._registry`. Create `demo/` at the project root (flat layout, no build backend — not installable, loaded by path from cwd):

```python
# demo/__init__.py
from nib import Rule, Diagnostic, ast

class NoPrint(Rule):
    code = "DEMO001"
    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            return [Diagnostic(node, "no print()")]
```

Also include a `demo/sample.py` with some `print(...)` calls so the CLI has something to lint end-to-end.

### 7. CLI

- `nib check <path>` — recursively finds `.py` files, parses each, runs all registered rules, prints diagnostics in `path:line:col: CODE message` format.
- `--rules module` flag (repeatable) — imports `module` purely for its side effects. The import triggers `__init_subclass__` for every `Rule` subclass defined in (or imported by) that module, populating `Rule._registry`. Built-in rules (e.g. `NoEval`) live in a module that's always imported.
- After all imports, CLI instantiates every class in `Rule._registry` (rules are nullary for the MVP) and hands the instances to Rust's `run()`.
- Prepend cwd to `sys.path` before resolving `--rules` so a local uninstalled package (like `demo/` in this repo) is loadable from the project root without `pip install`. Installed third-party packages still resolve normally via site-packages — the cwd entry is additive, not a replacement. Matches Ruff/flake8/pytest behavior.
- Verify with:
  ```
  nib check demo/sample.py --rules demo
  ```
  Both `X001` (built-in) and `DEMO001` (third-party) diagnostics should appear.
- Defer the TOML config file.

### 8. Stop.

Don't add: the semantic model, the project index, fix application, parallelism, configuration files, more node wrappers than the example rule needs, the macro.

## What this proves

By the end you can run `nib check foo.py` and have a Python-authored rule, dispatched from Rust, flag `eval(...)` calls. Every architectural decision is exercised — the wrappers, the dispatcher, the FFI boundary, the diagnostic flow. Everything else is filling in this skeleton.

## The natural next milestone (not MVP, but the obvious follow-up)

- Add `visit_Assign` + ~5 more wrappers.
- Add a minimal semantic model (imports table only).
- Add the project index (two `DashMap`s, populated by a pre-pass).
- Implement the Django signal rule as the second example.
- **Parse-error handling.** Tree-sitter is error-tolerant — it returns a tree with `ERROR` nodes for invalid syntax instead of failing. `parse_module` should check `tree.root_node().has_error()` and refuse to lint files that don't parse cleanly (emit a single `E000` diagnostic and skip rule dispatch), matching Ruff's behavior. Otherwise rules silently lint partially-broken code.
- **Rule-author warnings (CLI).** Currently invalid rules fail silently or with cryptic PyO3 errors. The CLI should surface these as warnings at startup, naming the offending rule + method:
  - **Unknown `visit_*` name** — e.g. `visit_Cal` (typo) registers no kinds. Warn: `RuleX.visit_Cal targets unknown AST class 'Cal'. Known: Module, Call, ...`. Validation point: `build_dispatch`.
  - **Bad return type** — a rule that returns `Diagnostic(...)` instead of `[Diagnostic(...)]` blows up in `try_iter` with no rule context. Wrap the failure with: `RuleX.visit_Call returned <type>, expected None or list of Diagnostic`. Validation point: `fire_methods`. Thread `rule.__class__.__name__` through `DispatchEntry` to make these messages useful.
  - **Non-Diagnostic items in returned list** — `return [node.id]` passes through silently today; the item lands in the results uncoded and confuses downstream formatters. Warn: `RuleX.visit_Name returned item of type <type>, expected Diagnostic`. Validation point: `fire_methods`, in the item loop right before the `cast::<Diagnostic>`.

That's when the architecture stops being a demo and starts being interesting. Keep it out of the MVP — you'll learn things in step 5 that change how you'd build the index.

## For later: plugin loading via entry points

The MVP uses an explicit `--rules module:attr` CLI flag because it's the smallest thing that proves third-party rules work. The proper Python convention is **`importlib.metadata` entry points** (how pytest/flake8/sphinx plugins are discovered):

- Third-party packages declare themselves in their own `pyproject.toml`:

  ```toml
  [project.entry-points."nib.rules"]
  nib_demo = "nib_demo:rules"
  ```

- `nib` discovers them at startup with `entry_points(group="nib.rules")` — no CLI flag, no config. `pip install` is the only user action.

- Keep `--rules` as a dev-time override (load a local package without installing it), but make entry points the primary path.

- Pair with a `[tool.nib] select = [...]` config in pyproject.toml to let users opt rules in/out by code, matching the Ruff/flake8 UX.
