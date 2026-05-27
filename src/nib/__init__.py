import ast
import warnings
from collections.abc import Callable

__all__ = ["Diagnostic", "Rule", "ast", "parse_module", "run"]


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
        self.lineno = getattr(node, "lineno", 0)
        self.col_offset = getattr(node, "col_offset", 0)
        self.end_lineno = getattr(node, "end_lineno", None)
        self.end_col_offset = getattr(node, "end_col_offset", None)
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


def run(module: ast.Module, rules: list[Rule]) -> list[Diagnostic]:
    results: list[Diagnostic] = []

    def _warn(rule_cls: str, method: str, msg: str) -> None:
        # Unique message text per (rule, method, msg) → default warning filter
        # already dedupes;
        warnings.warn(f"{rule_cls}.{method} {msg}", stacklevel=3)

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
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(module)
    return results
