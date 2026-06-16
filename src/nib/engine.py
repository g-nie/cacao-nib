"""Core types and the AST-walking rule engine.

`Diagnostic`/`Rule` are the public API rule authors subclass; `run` drives rule
dispatch over a parsed module. `nib/__init__.py` re-exports them, so
`from nib import Diagnostic, Rule, run` keeps working.
"""

import ast
import functools
import importlib
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from nib.analysis import (
    _collect_imports,
    _collect_import_targets,
    _file,
    _imports,
    _module_name,
)
from nib.output import _color, _color_enabled


@functools.cache
def _warn(msg: str) -> None:
    """Warn a rule author about a misdefined rule (unknown visitor target, bad
    return type, …), printing `nib warning: <msg>` to stderr once per message.

    Deliberately not the `warnings` module: these fire per AST node, so the
    `@cache` dedupes to avoid flooding; and a plain print keeps the line clean
    (no `file:line: Category:` nib internals an end user doesn't care about) and
    reads identically in the main interpreter and in worker subinterpreters.
    """
    label = _color("nib warning:", "33", enabled=_color_enabled(sys.stderr))
    print(f"{label} {msg}", file=sys.stderr)


class Diagnostic:
    __slots__ = (
        "lineno",
        "col_offset",
        "end_lineno",
        "end_col_offset",
        "message",
        "code",
    )

    def __init__(self, node: ast.AST, message: str):
        # 1-based line and column to match editor/CI conventions and
        # SyntaxError.offset. stdlib ast gives 0-based columns, so we shift.
        self.lineno = getattr(node, "lineno", 1)
        self.col_offset = getattr(node, "col_offset", 0) + 1
        self.end_lineno = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        self.end_col_offset = None if end_col is None else end_col + 1
        self.message = message
        self.code = ""


class _DeferredDiagnostic:
    """A diagnostic whose emission depends on cross-file reachability, decided only
    after the whole run is walked (the import picture isn't complete mid-walk).
    Constructed like a `Diagnostic` — `node` and `message` — plus `module`, the
    dotted name whose reachability gates the finding."""

    __slots__ = ("diagnostic", "module")

    # The reachability state `module` must be in for the finding to survive:
    # True = keep when imported, False = keep when imported nowhere. Set by the
    # two concrete subclasses below.
    keep_when_imported: bool

    def __init__(self, node: ast.AST, message: str, module: str):
        self.diagnostic = Diagnostic(node, message)
        self.module = module


class UnimportedDiagnostic(_DeferredDiagnostic):
    """A deferred finding that emits only if `module` is imported nowhere in
    scope — the orphan case (e.g. a plugin entry point nobody imports)."""

    __slots__ = ()
    keep_when_imported = False


class ImportedDiagnostic(_DeferredDiagnostic):
    """A deferred finding that emits only if something in scope imports `module`."""

    __slots__ = ()
    keep_when_imported = True


def _keep_deferred(module: str, keep_when_imported: bool, imported_set) -> bool:
    """Whether a deferred finding survives: `module`'s actual reachability must
    match the polarity the finding fires on (`keep_when_imported`)."""
    is_imported = module in imported_set
    return is_imported == keep_when_imported


class Rule:
    """Subclass and define `visit_<AstName>` methods that return a list of
    `Diagnostic`s (or None for "no findings").

    Defining a subclass auto-registers it on `Rule._registry` — the CLI
    instantiates everything in the registry after importing rule modules.

    Optionally set `group` to opt rules into category-style selection
    (e.g. `--select DEMO` matches every rule with `group = "DEMO"`).

    Inside a visitor, `self.resolve(node)` fully-qualifies a name/attribute via
    the module's imports (e.g. `np.array` -> "numpy.array"), and `self.imports`
    is the module-scope name->origin table.

    For cross-file rules, return deferred diagnostics gated on `self.module`'s
    reachability (see `UnimportedDiagnostic`/`ImportedDiagnostic`);
    nib resolves them once the whole run is walked.

    Example:

        class NoEval(Rule):
            code = "X001"
            group = "X"
            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == "eval":
                    return [Diagnostic(node, "no eval")]
    """

    code: str = ""
    group: str | None = None
    _registry: list[type["Rule"]] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        Rule._registry.append(cls)

    @property
    def imports(self) -> dict[str, str]:
        """The current module's name->origin import table.

        Only meaningful inside a visitor; empty outside a `run`. Module scope
        only: function/class-local imports aren't tracked, and a local/param
        that shadows an import name still resolves to the import anyway.
        """
        return _imports.get()

    @property
    def module(self) -> str | None:
        """The current file's dotted module name (e.g. "pkg.plugin"). Pass it (or
        a dotted path built from it) to a `_DeferredDiagnostic` to gate a cross-file
        finding on that module's/function's reachability. None outside a run."""
        file = _file.get()
        return _module_name(file) if file is not None else None

    def resolve(self, node: ast.AST) -> str | None:
        """Fully-qualify a Name/Attribute chain via the module's import table.

        `np.array` -> "numpy.array" when `np` was imported as numpy; a bare
        imported name resolves too (`c` -> "a.b.c"). Returns None when the head
        name isn't an imported module or the chain isn't a plain Name/Attribute.
        """
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        origin = self.imports.get(node.id)
        if origin is None:
            return None
        parts.append(origin)
        parts.reverse()
        return ".".join(parts)

    def enter_module(self, node: ast.AST):
        """Called once before any visitor fires for this file — the explicit place
        to set per-file state on `self` (one `Rule` instance is reused across
        every file in a run). Return diagnostics like a visitor, or None."""
        return None

    def leave_module(self, node: ast.AST):
        """Called once after every visitor has fired for this file. Return summary
        diagnostics that depend on having seen the whole module, or None."""
        return None


def _rule_visitors(cls: type) -> dict[type, str]:
    """Discover `visit_<AstName>` methods on a Rule subclass, returning
    `{ast_class: method_name}`. Cached on the class itself."""
    cached = cls.__dict__.get("_nib_visitors")
    if cached is not None:
        return cached
    visitors: dict[type, str] = {}
    for attr in dir(cls):
        if not attr.startswith("visit_") or not callable(getattr(cls, attr, None)):
            continue
        target = getattr(ast, attr.removeprefix("visit_"), None)
        if isinstance(target, type) and issubclass(target, ast.AST):
            visitors[target] = attr
    cls._nib_visitors = visitors
    return visitors


# Per-class cache of fields. Avoids re-reading `node_type._fields` on every
# node — the walk may visit millions of nodes on big runs, so even cheap
# attribute lookups add up.
_fields_cache: dict[type, tuple] = {}


def _child_nodes(node: ast.AST) -> list[ast.AST]:
    """Return a list of `node`'s direct AST children.

    Deliberately not using `ast.iter_child_nodes`. The stdlib version is two nested
    generators (`iter_child_nodes` → `iter_fields`), and the overhead dominates
    on large walks. Inlining the field scan and returning a list saves
    ~7% wall-clock on big runs.
    """
    node_type = type(node)
    fields = _fields_cache.get(node_type)
    if fields is None:
        fields = node_type._fields
        _fields_cache[node_type] = fields
    children: list = []
    for f in fields:
        v = getattr(node, f, None)
        if isinstance(v, ast.AST):
            children.append(v)
        elif type(v) is list:
            for item in v:
                if isinstance(item, ast.AST):
                    children.append(item)
    return children


def _run(
    module: ast.Module, rules: list[Rule], file: Path
) -> tuple[list[Diagnostic], list[_DeferredDiagnostic]]:
    """Drive rule dispatch over `module`, returning `(immediate, deferred)`:
    plain `Diagnostic`s emitted directly, and `_DeferredDiagnostic`s whose verdict
    waits on whole-run reachability. `file` resolves relative imports for
    `Rule.resolve` and derives `Rule.module` (only stat-walked if a rule reads
    them)."""
    results: list[Diagnostic] = []
    deferred: list[_DeferredDiagnostic] = []

    # Build visitors_by_node_type table once: ast_class -> [(rule, bound_fn, method_name), ...].
    # Per-node lookup becomes one dict.get plus only the relevant visitors;
    # rules with no visitor for this node type aren't iterated at all.
    visitors_by_node_type: dict[type, list[tuple[Rule, Callable, str]]] = {}
    for r in rules:
        for node_type, method_name in _rule_visitors(type(r)).items():
            visitors_by_node_type.setdefault(node_type, []).append(
                (r, getattr(r, method_name), method_name)
            )

    def dispatch(
        out: list[Diagnostic | _DeferredDiagnostic] | None, rule: Rule, method: str
    ) -> None:
        """Handle what a visitor or hook returned: tag each item with the rule's
        code and sort it into the immediate (`Diagnostic`) or deferred
        (`_DeferredDiagnostic`) results, warning on anything that breaks the expected
        `list[Diagnostic | _DeferredDiagnostic] | None` shape."""
        if out is None:
            return
        cls_name = type(rule).__name__
        if isinstance(out, (Diagnostic, _DeferredDiagnostic)):
            _warn(
                f"{cls_name}.{method} returned a single {type(out).__name__}; "
                "wrap it in a list"
            )
            out = [out]
        try:
            items = list(out)
        except TypeError:
            _warn(
                f"{cls_name}.{method} returned non-iterable "
                f"{type(out).__name__}; expected list of Diagnostic"
            )
            return
        rule_code = getattr(type(rule), "code", "")
        for item in items:
            if isinstance(item, Diagnostic):
                item.code = rule_code
                results.append(item)
            elif isinstance(item, _DeferredDiagnostic):
                item.diagnostic.code = rule_code
                deferred.append(item)
            else:
                _warn(
                    f"{cls_name}.{method} returned list contained "
                    f"{type(item).__name__}; expected Diagnostic (dropped)"
                )

    def walk(node: ast.AST) -> None:
        handlers = visitors_by_node_type.get(type(node))
        if handlers is not None:
            for rule, fn, method in handlers:
                dispatch(fn(node), rule, method)
        for child in _child_nodes(node):
            walk(child)

    # Expose this run's context (import table, file) to the rule properties for
    # the duration of the walk only; then restore, so none of it leaks past it.
    # The hooks run inside the same window, so `self.imports`/`self.module` work.
    # The base hooks are no-ops returning None, which `dispatch` ignores.
    import_token = _imports.set(_collect_imports(module, file))
    file_token = _file.set(file)
    try:
        for r in rules:
            dispatch(r.enter_module(module), r, "enter_module")
        walk(module)
        for r in rules:
            dispatch(r.leave_module(module), r, "leave_module")
    finally:
        _imports.reset(import_token)
        _file.reset(file_token)
    return results, deferred


def run(
    module: ast.Module,
    rules: list[Rule],
    file: Path,
    imported: frozenset[str] | None = None,
) -> list[Diagnostic]:
    """Drive rule dispatch over `module` and return all `Diagnostic`s, resolving
    any `_DeferredDiagnostic`s against `imported` — the set of reachable module
    names (None means "nothing imported", so `imported=False` conditions fire).
    The single-pass CLI defers resolution to the main process instead; this keeps
    direct callers and tests simple."""
    results, deferred = _run(module, rules, file)
    imported = imported or frozenset()
    results.extend(
        d.diagnostic
        for d in deferred
        if _keep_deferred(d.module, d.keep_when_imported, imported)
    )
    return results


def _select_rules(rule_classes, select: list[str], ignore: list[str]):
    """Filter `rule_classes` by code/group tokens. Ignore wins over select.

    Empty `select` means "all rules". Tokens that match neither a code nor a
    group are silently dropped, they just don't contribute any rules.
    """
    by_code = {c.code: c for c in rule_classes if c.code}
    by_group: dict[str, list] = {}
    for c in rule_classes:
        if c.group:
            by_group.setdefault(c.group, []).append(c)

    def resolve(tokens: list[str]) -> set:
        out: set = set()
        for tok in tokens:
            if tok in by_code:
                out.add(by_code[tok])
            if tok in by_group:
                out.update(by_group[tok])
        return out

    selected = resolve(select) if select else set(rule_classes)
    ignored = resolve(ignore)
    return [c for c in rule_classes if c in selected and c not in ignored]


@functools.cache
def _find_config() -> tuple[Path, dict]:
    """Locate the `[tool.nib]` config by ascending from the current directory.

    Returns `(root, table)` where `root` is the directory holding the nearest
    ancestor `pyproject.toml` that carries a `[tool.nib]` table, and `table` is
    that table. A `pyproject.toml` without the tool's own section is skipped
    and the search continues upward. Falls back to `(cwd, {})` when no `[tool.nib]`
    exists anywhere, so plugin imports still anchor at the invocation directory.
    """
    cwd = Path.cwd()
    for d in (cwd, *cwd.parents):
        pyproject = d / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            with pyproject.open("rb") as f:
                data = tomllib.load(f)
        except OSError, tomllib.TOMLDecodeError:
            continue
        if (table := data.get("tool", {}).get("nib")) is not None:
            return d, table
    return cwd, {}


def _reimport_plugins(modules: list[str]) -> None:
    """Re-import plugins inside a worker subinterpreter to rebuild its own
    `Rule._registry` (each interpreter starts with an empty one and can't share
    the main interpreter's). Adds the project root to `sys.path`, then imports
    each module so its `Rule` subclasses register. The names are assumed already
    validated in the main interpreter, so import failures aren't reported here —
    see `cli._load_plugins` for the validated path."""
    sys.path.insert(0, str(_find_config()[0]))
    for name in modules:
        importlib.import_module(name)


class FileError(NamedTuple):
    """A whole-file read or parse failure (not a lint finding).

    `kind` discriminates: `"read"` populates `exc_type`/`message`; `"syntax"`
    populates `lineno`/`offset`/`message`. The unused fields stay `None`, so the
    shape is uniform regardless of kind.
    """

    kind: str  # "read" | "syntax"
    message: str
    lineno: int | None = None  # syntax only
    offset: int | None = None  # syntax only
    exc_type: str | None = None  # read only


def _diag_wire(d: Diagnostic) -> tuple:
    """The shareable tuple form of a `Diagnostic` (it can't cross a subinterpreter
    boundary as an instance — each interpreter has its own `Diagnostic` class)."""
    return d.lineno, d.col_offset, d.end_lineno, d.end_col_offset, d.message, d.code


def _file_targets(file: Path) -> tuple[str, ...]:
    """This file's import targets alone — no rules run — for building the
    project-wide reachability set a cross-file rule resolves against. An unreadable
    or unparsable file contributes nothing."""
    try:
        mod = ast.parse(file.read_text())
    except OSError, UnicodeDecodeError, SyntaxError:
        return ()
    return tuple(_collect_import_targets(mod, file))


def _check_file(file: Path, rules: list["Rule"]) -> tuple:
    """Parse and lint `file`. Returns
    `(file_str, source, diag_tuples, err, target_tuple, deferred_tuples)`.

    Workers run in subinterpreters, each with its own classes, so nothing is
    shipped back as an instance — we return plain tuples of primitives, so the
    same code path works in the main interpreter and in a worker.

    - `diag_tuples`: the wire form of the immediate diagnostics.
    - `deferred_tuples`: `tuple[(*diag_wire, module, keep_when_imported)]` —
      findings whose verdict the main process resolves once it knows the project's
      reachable set.
    - `target_tuple`: this file's import targets, fed into that reachability set.
    - `err`: `None` on success, else the wire form of a `FileError` (rebuild with
      `FileError(*err)`)."""
    file_str = str(file)
    try:
        source = file.read_text()
    except (OSError, UnicodeDecodeError) as e:
        err = FileError("read", str(e), exc_type=type(e).__name__)
        return file_str, None, (), tuple(err), (), ()
    try:
        mod = ast.parse(source)
    except SyntaxError as e:
        err = FileError("syntax", e.msg, lineno=e.lineno or 1, offset=e.offset or 1)
        return file_str, source, (), tuple(err), (), ()
    diags, deferred = _run(mod, rules, file=file)
    diag_tuples = tuple(_diag_wire(d) for d in diags)
    deferred_tuples = tuple(
        (*_diag_wire(d.diagnostic), d.module, d.keep_when_imported) for d in deferred
    )
    targets = tuple(_collect_import_targets(mod, file))
    return file_str, source, diag_tuples, None, targets, deferred_tuples
