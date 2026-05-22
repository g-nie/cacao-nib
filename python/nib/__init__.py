from .nib import Diagnostic, ast, parse_module, run


class Rule:
    """Subclass and define `visit_<AstName>` methods that return a list of
    `Diagnostic`s (or `None`/nothing for "no findings").

    Defining a subclass auto-registers it on `Rule._registry` — the CLI
    instantiates everything in the registry after importing rule modules.

    Example:

        class NoEval(Rule):
            code = "X001"
            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == "eval":
                    return [Diagnostic(node, "no eval")]
    """

    _registry: list[type["Rule"]] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        Rule._registry.append(cls)


__all__ = ["Diagnostic", "Rule", "ast", "parse_module", "run"]
