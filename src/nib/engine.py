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

from nib.analysis import _collect_imports, _imports
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

    def __init__(self, node, message: str):
        # 1-based line and column to match editor/CI conventions and
        # SyntaxError.offset. stdlib ast gives 0-based columns, so we shift.
        self.lineno = getattr(node, "lineno", 1)
        self.col_offset = getattr(node, "col_offset", 0) + 1
        self.end_lineno = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        self.end_col_offset = None if end_col is None else end_col + 1
        self.message = message
        self.code = ""


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

    def resolve(self, node) -> str | None:
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


def _child_nodes(node):
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


def run(module: ast.Module, rules: list[Rule], file: Path) -> list[Diagnostic]:
    """Drive rule dispatch over `module`. `file` is the module's path on disk,
    used to resolve relative imports for `Rule.resolve` (only touched if the
    module actually has a relative import)."""
    results: list[Diagnostic] = []

    # Build visitors_by_node_type table once: ast_class -> [(rule, bound_fn, method_name), ...].
    # Per-node lookup becomes one dict.get plus only the relevant visitors;
    # rules with no visitor for this node type aren't iterated at all.
    visitors_by_node_type: dict[type, list[tuple[Rule, Callable, str]]] = {}
    for r in rules:
        for node_type, method_name in _rule_visitors(type(r)).items():
            visitors_by_node_type.setdefault(node_type, []).append(
                (r, getattr(r, method_name), method_name)
            )

    def walk(node):
        handlers = visitors_by_node_type.get(type(node))
        if handlers is not None:
            for rule, fn, method in handlers:
                out = fn(node)
                if out is None:
                    continue
                cls_name = type(rule).__name__
                if isinstance(out, Diagnostic):
                    _warn(
                        f"{cls_name}.{method} returned a single Diagnostic; "
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
                    continue
                rule_code = getattr(type(rule), "code", "")
                for item in items:
                    if not isinstance(item, Diagnostic):
                        _warn(
                            f"{cls_name}.{method} returned list contained "
                            f"{type(item).__name__}; expected Diagnostic (dropped)"
                        )
                        continue
                    item.code = rule_code
                    results.append(item)
        for child in _child_nodes(node):
            walk(child)

    # Expose this module's import table to `Rule.resolve`/`Rule.imports` for the
    # duration of the walk only; then restore, so it never leaks past the run.
    token = _imports.set(_collect_imports(module, file))
    try:
        walk(module)
    finally:
        _imports.reset(token)
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


def _check_file(file: Path, rules: list["Rule"]) -> tuple:
    """Parse and lint `file`. Returns `(file_str, source, diag_tuples, err)`.

    Workers run in subinterpreters, each with its own `Diagnostic` class, so a
    `Diagnostic` instance can't be shipped back over the queue. We return plain
    tuples of shareable primitives instead, so the same code path works in the
    main interpreter and in a worker.

    `diag_tuples` is `tuple[(lineno, col, end_lineno, end_col, message, code)]`.
    `err` is `None` on success, otherwise the wire form of a `FileError`
    (`tuple(FileError(...))`); rebuild it with `FileError(*err)`.
    """
    file_str = str(file)
    try:
        source = file.read_text()
    except (OSError, UnicodeDecodeError) as e:
        err = FileError("read", str(e), exc_type=type(e).__name__)
        return file_str, None, (), tuple(err)
    try:
        mod = ast.parse(source)
    except SyntaxError as e:
        err = FileError("syntax", e.msg, lineno=e.lineno or 1, offset=e.offset or 1)
        return file_str, source, (), tuple(err)
    diags = run(mod, rules, file=file)
    diag_tuples = tuple(
        (d.lineno, d.col_offset, d.end_lineno, d.end_col_offset, d.message, d.code)
        for d in diags
    )
    return file_str, source, diag_tuples, None
