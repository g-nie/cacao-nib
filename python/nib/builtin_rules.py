"""Rules shipped with cacao-nib itself. Imported by the CLI on every run."""

from nib import Diagnostic, Rule, ast


class NoEval(Rule):
    code = "X001"

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "eval":
            return [Diagnostic(node, "no eval")]
