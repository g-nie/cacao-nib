# only-ast handoff

You are continuing work on **cacao-nib**, a Ruff-like Python linter. This branch (`only-ast`) is an experiment: rebuild the linter in **pure Python using stdlib `ast`**, no Rust, no tree-sitter, no PyO3. The user will then manually benchmark this against the Rust+tree-sitter hybrid on `main` to decide whether the hybrid is worth its complexity.

## Why this experiment exists

The hybrid on `main` uses PyO3 to expose tree-sitter nodes to Python rules. Every `node.func` / `node.args` access crosses the FFI boundary and lazily builds a wrapper, which likely eats most of the Rust parsing win. Pure Python on top of CPython's already-C `ast.parse` may be competitive *and* dramatically simpler. Verify empirically.

## Constraints — keep the user-facing API identical

The benchmark only means something if the two implementations expose the same surface.

- **CLI:** `nib check <path?> --plugins MODULE [--plugins MODULE]`
  - `path` defaults to `.`, walks recursively, only `*.py` files, sorted.
  - `--plugins` is repeatable; each value is a module name imported for its side effects.
  - cwd is prepended to `sys.path` so local packages (like `demo/`) import without install.
  - Exit codes: `0` clean, `1` findings, `2` usage error / missing path / failed import.
  - Output: `path:line:col: error[CODE] message` with ANSI red on `error` and bold-underline on `CODE` (only when stdout is a TTY and `NO_COLOR` is unset).
  - Per-file try/except: one bad file shouldn't kill the run; print to stderr and continue.
- **Rule API:**
  ```python
  from nib import Diagnostic, Rule, ast

  class NoEval(Rule):
      code = "X001"
      def visit_Call(self, node):
          if isinstance(node.func, ast.Name) and node.func.id == "eval":
              return [Diagnostic(node, "no eval")]
  ```
  - Subclasses auto-register via `Rule.__init_subclass__` — no explicit `rules = [...]` list.
  - `visit_<ClassName>` matches stdlib `ast.NodeVisitor` naming. This is intentional — the whole point is that rule authors transfer knowledge from CPython's `ast` directly.
  - `Diagnostic(node, message)` reads span (`lineno`, `col_offset`, `end_lineno`, `end_col_offset`) from the node. `code` is filled in by the dispatcher from the owning rule's class attribute.
  - A `visit_*` method may return `None` (no findings) or an iterable of `Diagnostic`s.
- **Builtin rules:** `nib.builtin_rules` is always imported. Currently just `NoEval` (`X001`).
- **Demo plugin:** `demo/__init__.py` with codes DEMO001–DEMO009. Violations live in `demo/sample.py`. Don't change either — they're shared test fixtures across branches.

## Simplification opportunities (this is the whole point)

In pure Python you get for free what the Rust side hand-rolls:
- No wrapper classes — rules consume stdlib `ast` nodes directly. `ast.Call.func` is already an `ast.Name`. Drop the entire `parser.rs` translation layer.
- No `kinds_for_visit` map — `type(node).__name__` is the dispatch key, naturally matching `visit_<ClassName>`.
- `ast.NodeVisitor` already does pre-order traversal; either subclass it or write a small walker (`ast.walk` exists but doesn't give parent context).
- `Diagnostic` becomes a tiny dataclass.
- File walking: `pathlib.Path.rglob("*.py")` with a `sorted(...)`. Don't reach for `walkdir` analogues.

## Repo shape (current `main`, for reference)

- `Cargo.toml`, `src/` — Rust crate (delete on this branch).
- `pyproject.toml` — maturin build backend (replace with a plain hatchling/setuptools setup or just `[project.scripts]`).
- `python/nib/__init__.py` — `Rule` class.
- `python/nib/builtin_rules.py` — `NoEval`.
- `python/nib/cli.py` — argparse CLI (port nearly as-is; swap the Rust imports for pure Python equivalents).
- `python/tests/test_cli.py` — 6 integration tests against demo plugin (keep these; they're the acceptance criteria).
- `python/tests/test_parser.py` — 2 wrapper tests (delete; no wrappers on this branch).
- `demo/__init__.py`, `demo/sample.py` — shared fixtures (keep as-is).

## Suggested plan

1. Delete Rust artifacts: `Cargo.toml`, `Cargo.lock`, `src/`, `target/`, anything maturin-specific in `pyproject.toml`.
2. Switch `pyproject.toml` to a normal Python build (hatchling is fine). Keep `[project.scripts] nib = "nib.cli:main"`.
3. Rewrite `python/nib/__init__.py`:
   - `Rule` stays exactly the same.
   - `Diagnostic` becomes a `dataclass`. Constructor takes `(node, message)` and pulls span attrs.
   - Drop the `parse_module`/`run`/`ast` re-exports from the Rust module. Re-export stdlib `ast` directly so `from nib import ast` still works for rules.
4. Write a tiny `nib.runner` (or fold into `cli.py`):
   - `parse(source) -> ast.Module`: `ast.parse(source)`.
   - `run(module, rules) -> list[Diagnostic]`: walk the tree; for each node, look up `visit_<type(node).__name__>` on each rule; collect returns; tag each Diagnostic's `code` from the rule's `code` attr.
   - File collection: `sorted(Path(root).rglob("*.py"))`.
5. Port `cli.py` — same argparse shape, same output format, same exit codes, same per-file try/except (`OSError`, `UnicodeDecodeError`, `SyntaxError` instead of `RuntimeError`).
6. Run `uv run pytest python/tests/test_cli.py` — all 6 should pass unchanged.
7. Manual benchmark: tell the user when ready; they'll run it.

## User preferences (apply throughout)

- New to Rust, comfortable in Python. Terse responses. No trailing summary paragraphs when the diff is self-evident.
- Pause for the user to pick the next checkpoint after a logical unit lands; don't auto-proceed.
- Avoid macros / unnecessary abstractions; prefer plain functions.
- Don't write comments that just restate what the code does. Don't reference "the migration" / "from the hybrid" — those belong in commit messages.

## What to do first

Confirm with the user that this plan matches what they want before deleting Rust files. Then execute step-by-step, stopping after each checkpoint.
