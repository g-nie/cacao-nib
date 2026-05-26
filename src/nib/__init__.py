import ast
import sys

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


# Per-process cache of `type(node) -> "visit_<Name>"`. The AST class set is
# fixed and small, so this is bounded; saves a string-concat per node per file.
_METHOD_NAME: dict[type, str] = {}


def run(module: ast.Module, rules: list[Rule]) -> list[Diagnostic]:
    results: list[Diagnostic] = []
    # (rule_class_name, method, kind) — dedupe runtime warnings per rule+method.
    warned: set[tuple[str, str, str]] = set()

    def _warn(rule_cls: str, method: str, kind: str, msg: str) -> None:
        key = (rule_cls, method, kind)
        if key in warned:
            return
        warned.add(key)
        print(f"nib warning: {rule_cls}.{method} {msg}", file=sys.stderr)

    def walk(node):
        node_type = type(node)
        method = _METHOD_NAME.get(node_type)
        if method is None:
            method = f"visit_{node_type.__name__}"
            _METHOD_NAME[node_type] = method
        for rule in rules:
            fn = getattr(rule, method, None)
            if fn is None:
                continue
            out = fn(node)
            if out is None:
                continue
            cls_name = type(rule).__name__
            if isinstance(out, Diagnostic):
                _warn(
                    cls_name,
                    method,
                    "single",
                    "returned a single Diagnostic; wrap it in a list",
                )
                out = [out]
            try:
                items = list(out)
            except TypeError:
                _warn(
                    cls_name,
                    method,
                    "noniter",
                    f"returned non-iterable {type(out).__name__}; "
                    "expected list of Diagnostic",
                )
                continue
            for item in items:
                if not isinstance(item, Diagnostic):
                    _warn(
                        cls_name,
                        method,
                        "nondiag",
                        f"returned list contained {type(item).__name__}; "
                        "expected Diagnostic (dropped)",
                    )
                    continue
                item.code = getattr(type(rule), "code", "")
                results.append(item)
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(module)
    return results
