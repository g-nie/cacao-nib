"""Frozen rule set used by the benchmarking workflow.

Kept separate from `demo.rules` so adding/removing demo rules doesn't shift
the benchmark baseline — what we want to measure is nib core, with rule
workload held constant.
"""

from nib import Diagnostic, Rule, ast


class NoPrint(Rule):
    code = "DEMO001"
    group = "DEMO"

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            return [Diagnostic(node, "no print()")]


class NoStringConcatenation(Rule):
    code = "DEMO004"
    group = "DEMO"

    def visit_BinOp(self, node):
        if not isinstance(node.op, ast.Add):
            return
        left_is_str = isinstance(node.left, ast.Constant) and isinstance(
            node.left.value, str
        )
        right_is_str = isinstance(node.right, ast.Constant) and isinstance(
            node.right.value, str
        )
        if left_is_str or right_is_str:
            return [Diagnostic(node, "string concat — use an f-string or .join")]


class NoShadowingBuiltins(Rule):
    code = "DEMO006"
    group = "DEMO"

    def visit_FunctionDef(self, node):
        if node.name in ["list", "dict"]:
            return [Diagnostic(node, f"function {node.name!r} shadows a builtin")]


class MaxParameters(Rule):
    code = "DEMO009"
    group = "DEMO"
    MAX = 5

    def visit_FunctionDef(self, node):
        n = len(node.args.args)
        if n > self.MAX:
            return [
                Diagnostic(
                    node,
                    f"function {node.name!r} has {n} parameters, max {self.MAX}",
                )
            ]
