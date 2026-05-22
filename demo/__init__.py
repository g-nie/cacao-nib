from nib import Diagnostic, Rule, ast


class NoPrint(Rule):
    code = "DEMO001"

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            return [Diagnostic(node, "no print()")]
