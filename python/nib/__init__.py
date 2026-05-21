from .nib import Diagnostic, ast, parse_module, run


class Rule:
    """Subclass and define `visit_<AstName>` methods that return a list of
    `Diagnostic`s (or `None`/nothing for "no findings").

    Example:

        class NoEval(Rule):
            code = "X001"
            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == "eval":
                    return [Diagnostic(node, "no eval")]
    """


__all__ = ["Diagnostic", "Rule", "ast", "parse_module", "run"]
