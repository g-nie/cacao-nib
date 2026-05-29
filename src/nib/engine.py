"""Core types and the AST-walking rule engine.

`Diagnostic`/`Rule` are the public API rule authors subclass; `parse_module`
and `run` drive parsing and rule dispatch. `nib/__init__.py` re-exports all of
them, so `from nib import Diagnostic, Rule, parse_module, run` keeps working.
"""

import ast
import importlib
import sys
import warnings
from collections.abc import Callable
from pathlib import Path


class NibWarning(UserWarning):
    """nib's own rule-author warnings (misdefined visitors, bad return types).
    A dedicated category lets the CLI format these distinctly from
    third-party/Python warnings, which it passes through in the default format.
    """


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
    `Diagnostic`s (or `None`/nothing for "no findings").

    Defining a subclass auto-registers it on `Rule._registry` — the CLI
    instantiates everything in the registry after importing rule modules.

    Optionally set `group` to opt rules into category-style selection
    (e.g. `--select DEMO` matches every rule with `group = "DEMO"`).

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


def parse_module(source: str) -> ast.Module:
    return ast.parse(source)


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


def _iter_children(node):
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


def run(module: ast.Module, rules: list[Rule]) -> list[Diagnostic]:
    results: list[Diagnostic] = []

    def _warn(rule_cls: str, method: str, msg: str) -> None:
        # Unique message text per (rule, method, msg) → default warning filter
        # already dedupes;
        warnings.warn(f"{rule_cls}.{method} {msg}", NibWarning, stacklevel=3)

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
                        cls_name,
                        method,
                        "returned a single Diagnostic; wrap it in a list",
                    )
                    out = [out]
                try:
                    items = list(out)
                except TypeError:
                    _warn(
                        cls_name,
                        method,
                        f"returned non-iterable {type(out).__name__}; "
                        "expected list of Diagnostic",
                    )
                    continue
                rule_code = getattr(type(rule), "code", "")
                for item in items:
                    if not isinstance(item, Diagnostic):
                        _warn(
                            cls_name,
                            method,
                            f"returned list contained {type(item).__name__}; "
                            "expected Diagnostic (dropped)",
                        )
                        continue
                    item.code = rule_code
                    results.append(item)
        for child in _iter_children(node):
            walk(child)

    walk(module)
    return results


def _select_rules(rule_classes, select: list[str], ignore: list[str]):
    """Filter `rule_classes` by code/group tokens. Ignore wins over select.

    Empty `select` means "all rules". Tokens that match neither a code nor a
    group are silently dropped — they just don't contribute any rules.
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


def _import_plugins(modules: list[str]) -> None:
    """Make cwd importable, then import each plugin module so its `Rule`
    subclasses register. The names are assumed already validated in the main
    interpreter (this runs inside worker subinterpreters), so import failures
    aren't reported here — see `cli._load_plugins` for the validated path."""
    sys.path.insert(0, str(Path.cwd()))
    for name in modules:
        importlib.import_module(name)


# Workers (in subinterpreters) can't ship `Diagnostic` instances back: each
# interpreter has its own `Diagnostic` class. `_check_file` returns plain tuples
# of shareable primitives so the same code path works whether it runs in the
# main interpreter or in a worker subinterpreter (results cross back over the
# queue).


def _check_file(file: Path, rules: list["Rule"]) -> tuple:
    """Parse and lint `file`. Returns `(file_str, source, diag_tuples, err)`.

    `diag_tuples` is `tuple[(lineno, col, end_lineno, end_col, message, code)]`
    — picked to be cross-interpreter shareable. `err` is `None` on success,
    otherwise `(kind, *details)` where `kind` is the first element and is one of:
    `("syntax", lineno, offset, msg)` for parse failures, or
    `("read", exc_type_name, str(exc))` for read failures.
    """
    file_str = str(file)
    try:
        source = file.read_text()
    except (OSError, UnicodeDecodeError) as e:
        return file_str, None, (), ("read", type(e).__name__, str(e))
    try:
        mod = parse_module(source)
    except SyntaxError as e:
        return file_str, source, (), ("syntax", e.lineno or 1, e.offset or 1, e.msg)
    diags = run(mod, rules)
    diag_tuples = tuple(
        (d.lineno, d.col_offset, d.end_lineno, d.end_col_offset, d.message, d.code)
        for d in diags
    )
    return file_str, source, diag_tuples, None
