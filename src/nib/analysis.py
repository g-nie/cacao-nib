"""Static import analysis: the model behind `Rule.imports`/`resolve`.

A stdlib-only leaf module (no `Rule`/`Diagnostic`/`run` dependency), so
`engine.py` can import it one-directionally without a cycle. Holds the per-file
import *name table* (`_collect_imports`) and the relative-import resolution it
needs, plus the ambient `_imports` ContextVar the engine binds for each walk.
"""

import ast
import contextvars
import importlib.util
from collections import deque
from pathlib import Path

# The current module's name->origin import table, live for the duration of the
# walk. Kept here as shared per-run context rather than as state on Rule
# instances, which are reused across every file. A ContextVar also keeps
# concurrent `run` calls in the same interpreter isolated.
_imports: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "nib_imports", default={}
)


def _resolve_relative(*, package: str, level: int, module: str | None) -> str | None:
    """Resolve a relative import's module path against the importer's `package`.
    `level` is the leading-dot count and `module` the name after them.
    Returns None when the import reaches beyond the top-level package."""
    try:
        return importlib.util.resolve_name("." * level + (module or ""), package)
    except ImportError:  # reaches beyond the top-level package
        return None


def _module_package(file: Path) -> str | None:
    """The `__package__` of the module in `file`, derived statically by walking
    the directory tree: ascend while an `__init__.py` exists.
    Returns None for a module not inside any package, so relative imports there
    stay unresolved. PEP 420 namespace packages (no `__init__`) are a known gap,
    their root is misdetected."""
    parts: list[str] = []
    d = file.parent
    while (d / "__init__.py").is_file():
        parts.append(d.name)
        d = d.parent
    if not parts:
        return None
    return ".".join(reversed(parts))


def _collect_imports(module: ast.Module, file: Path) -> dict[str, str]:
    """Flat module-scope import table: local name -> fully-qualified origin.

    Module scope only — descends top-level blocks but stops at function and
    class bodies (so function/class-local imports aren't tracked - known gap).
    `file` resolves relative imports. Unresolvable relative imports are omitted."""

    imports: dict[str, str] = {}
    package: str | None = None
    package_derived = False
    queue = deque(module.body)
    while queue:
        node = queue.popleft()
        t = type(node)
        if t is ast.Import:
            for a in node.names:
                if a.asname:
                    imports[a.asname] = a.name  # import a.b as c -> c: a.b
                else:
                    top = a.name.partition(".")[0]  # import a.b.c -> a: a
                    imports[top] = top
        elif t is ast.ImportFrom:
            if node.level:  # relative
                if not package_derived:  # stat the layout once, lazily
                    package = _module_package(file)
                    package_derived = True
                if package is None:  # module isn't in a package -> unresolvable
                    continue
                base = _resolve_relative(
                    package=package, level=node.level, module=node.module
                )
                if base is None:  # reaches beyond the top-level package
                    continue
            else:
                base = node.module  # a non-relative `from` always names a module
            for a in node.names:
                if a.name != "*":
                    imports[a.asname or a.name] = f"{base}.{a.name}"
        elif t not in (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef):
            queue.extend(
                c
                for c in ast.iter_child_nodes(node)
                if isinstance(c, (ast.stmt, ast.excepthandler, ast.match_case))
            )
    return imports
