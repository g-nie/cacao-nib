"""Static import analysis behind the rule import surfaces.

Two models live here: the per-file import *name table* (`_collect_imports`,
behind `Rule.imports`/`resolve`) and the project-wide import *manifest*
(`_collect_import_targets`, queried by `imported_among` to resolve cross-file
`_DeferredDiagnostic`s), plus the relative-import resolution both need and the
implicit ContextVars (`_imports`/`_file`) the engine binds for each walk.
"""

import ast
import contextvars
import functools
import importlib.util
from collections import deque
from collections.abc import Iterable
from pathlib import Path

# The current module's name->origin import table, live for the duration of the
# walk. Kept here as shared per-run context rather than as state on Rule
# instances, which are reused across every file. A ContextVar also keeps
# concurrent `run` calls in the same interpreter isolated.
_imports: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "nib_imports", default={}
)

# The current file's path, live for the duration of the walk (same rationale as
# `_imports`). Backs `Rule.module`.
_file: contextvars.ContextVar["Path | None"] = contextvars.ContextVar(
    "nib_file", default=None
)


def _resolve_relative(*, package: str, level: int, module: str | None) -> str | None:
    """Resolve a relative import's module path against the importer's `package`.
    `level` is the leading-dot count and `module` the name after them.
    Returns None when the import reaches beyond the top-level package."""
    try:
        return importlib.util.resolve_name("." * level + (module or ""), package)
    except ImportError:  # reaches beyond the top-level package
        return None


@functools.cache
def _package_of_dir(directory: Path) -> str | None:
    """The dotted package name for `directory`, or None if it isn't a package
    (no `__init__.py`). Recurses to the parent and is cached per directory, so a
    tree of N files across M package directories costs O(M) stats total, not
    O(N · depth) — every file in a directory reuses the one cached result."""
    if not (directory / "__init__.py").is_file():
        return None
    parent = _package_of_dir(directory.parent)
    return f"{parent}.{directory.name}" if parent else directory.name


def _module_package(file: Path) -> str | None:
    """The `__package__` of the module in `file`, derived statically from its
    directory tree (ascend while an `__init__.py` exists). Returns None for a
    module not inside any package, so relative imports there stay unresolved.
    PEP 420 namespace packages (no `__init__`) are a known gap, their root is
    misdetected."""
    return _package_of_dir(file.parent)


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


def _module_name(file: Path) -> str:
    """The dotted module name for `file` (`pkg/sub/mod.py` -> "pkg.sub.mod"),
    derived from its package layout. An `__init__.py` is the package itself"""
    package = _module_package(file)
    if file.name == "__init__.py":
        return package or file.parent.name
    return f"{package}.{file.stem}" if package else file.stem


def _collect_import_targets(module: ast.Module, file: Path) -> set[str]:
    """The import *manifest* for one file: every fully-qualified module target it
    imports. Distinct from `_collect_imports`, which is module-scope only and
    keeps binding names rather than full dotted paths.

      import a.b.c            -> {"a.b.c"}
      from a.b import c, d    -> {"a.b", "a.b.c", "a.b.d"}

    Descends statement positions only (imports never live in an expression), but
    unlike `_collect_imports` it recurses *into* function and class bodies, so
    local imports count too. Relative imports are resolved against the file's package,
    unresolved ones omitted; a `from a import *` contributes only the from-module."""
    targets: set[str] = set()
    package: str | None = None
    package_derived = False
    queue = deque(module.body)
    while queue:
        node = queue.popleft()
        t = type(node)
        if t is ast.Import:
            for a in node.names:
                targets.add(a.name)  # import a.b.c [as x] -> a.b.c
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
            targets.add(base)
            for a in node.names:
                if a.name != "*":
                    targets.add(f"{base}.{a.name}")
        else:  # descend into every statement, including function/class bodies
            queue.extend(
                c
                for c in ast.iter_child_nodes(node)
                if isinstance(c, (ast.stmt, ast.excepthandler, ast.match_case))
            )
    return targets


def imported_among(
    modules: Iterable[str], targets_per_file: Iterable[Iterable[str]]
) -> frozenset[str]:
    """Return the subset of `modules` that some file imports — i.e. whose dotted
    path appears as an import target somewhere in the run.

    `modules` is the handful of modules deferred findings depend on;
    `targets_per_file` is each file's import targets from the check pass. We keep
    only targets that fall in `modules`, so the result stays small instead of
    holding the project's whole import universe."""
    wanted = set(modules)
    return frozenset(
        target for targets in targets_per_file for target in targets if target in wanted
    )
