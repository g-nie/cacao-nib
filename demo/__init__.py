from nib import Diagnostic, Rule, ast


class NoPrint(Rule):
    code = "DEMO001"

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            return [Diagnostic(node, "no print()")]


class NoLambdaWithMoreThanThreeArgs(Rule):
    """Flags lambdas that accept more than three parameters — at that point
    a real `def` is clearer."""

    code = "DEMO002"

    def visit_Lambda(self, node):
        if len(node.args) > 3:
            return [
                Diagnostic(node, f"lambda has {len(node.args)} args, max 3 — use def")
            ]


class NoOrChain(Rule):
    """Flags long `or` chains (`a or b or c or d`) that should usually be
    a membership test (`x in {...}`) instead."""

    code = "DEMO003"

    def visit_BoolOp(self, node):
        if node.op == "or" and len(node.values) > 3:
            return [
                Diagnostic(node, f"or-chain of {len(node.values)} — prefer `in {{...}}`")
            ]


class NoStringConcatenation(Rule):
    """Flags `"a" + x` style concatenation; use f-strings or `.join`."""

    code = "DEMO004"

    def visit_BinOp(self, node):
        if node.op != "+":
            return
        left_is_str = isinstance(node.left, ast.Constant) and isinstance(
            node.left.value, str
        )
        right_is_str = isinstance(node.right, ast.Constant) and isinstance(
            node.right.value, str
        )
        if left_is_str or right_is_str:
            return [Diagnostic(node, "string concat — use an f-string or .join")]


class UseIsForNone(Rule):
    """E711-style: `x == None` → use `x is None`. Same for `!=` → `is not`.
    Identity is the right comparison for the singleton None."""

    code = "DEMO005"

    def visit_Compare(self, node):
        # node.ops is a list of operator strings ("==", "<", "is", ...).
        # node.comparators is a list of right-hand expressions, one per op.
        # For a chained compare like `a == None == b`, both pairs are checked.
        diags = []
        for op, right in zip(node.ops, node.comparators):
            if op in ("==", "!=") and isinstance(right, ast.Constant) and right.value is None:
                hint = "is" if op == "==" else "is not"
                diags.append(Diagnostic(node, f"compare to None with `{hint}`, not `{op}`"))
        return diags
