from nib import Diagnostic, Rule, ast


class NoPrint(Rule):
    code = "DEMO001"
    group = "DEMO"

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            return [Diagnostic(node, "no print()")]


class NoLambdaWithMoreThanThreeArgs(Rule):
    """Flags lambdas that accept more than three parameters — at that point
    a real `def` is clearer."""

    code = "DEMO002"
    group = "DEMO"

    def visit_Lambda(self, node):
        n = len(node.args.args)
        if n > 3:
            return [Diagnostic(node, f"lambda has {n} args, max 3 — use def")]


class NoOrChain(Rule):
    """Flags long `or` chains (`a or b or c or d`) that should usually be
    a membership test (`x in {...}`) instead."""

    code = "DEMO003"
    group = "DEMO"

    def visit_BoolOp(self, node):
        if isinstance(node.op, ast.Or) and len(node.values) > 3:
            return [
                Diagnostic(
                    node, f"or-chain of {len(node.values)} — prefer `in {{...}}`"
                )
            ]


class NoStringConcatenation(Rule):
    """Flags `"a" + x` style concatenation; use f-strings or `.join`."""

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


def _is_pascal_case(name: str) -> bool:
    """Starts with uppercase, alphanumeric only, no underscores."""
    return bool(name) and name[0].isupper() and name.isalnum()


class NoShadowingBuiltins(Rule):
    """Flags `def list(...)` / `def id(...)` — defining a function with a
    builtin's name silently shadows the builtin in that scope, a common
    source of "why is this broken" bugs."""

    code = "DEMO006"
    group = "DEMO"

    def visit_FunctionDef(self, node):
        if node.name in ["list", "dict"]:  # just a dummy subset
            return [Diagnostic(node, f"function {node.name!r} shadows a builtin")]


class ClassShouldBePascalCase(Rule):
    """PEP 8 N801 — class names should be PascalCase."""

    code = "DEMO007"
    group = "DEMO"

    def visit_ClassDef(self, node):
        if not _is_pascal_case(node.name):
            return [Diagnostic(node, f"class name {node.name!r} should be PascalCase")]


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


class NoChainedAssignment(Rule):
    """Chained `a = b = 1` makes intent (alias? separate vars?) ambiguous."""

    code = "DEMO008"
    group = "DEMO"

    def visit_Assign(self, node):
        if len(node.targets) > 1:
            return [
                Diagnostic(
                    node,
                    f"chained assignment with {len(node.targets)} targets — split it",
                )
            ]


class UseIsForNone(Rule):
    """E711-style: `x == None` → use `x is None`. Same for `!=` → `is not`.
    Identity is the right comparison for the singleton None."""

    code = "DEMO005"
    group = "DEMO"

    def visit_Compare(self, node):
        diags = []
        for op, right in zip(node.ops, node.comparators):
            if isinstance(right, ast.Constant) and right.value is None:
                if isinstance(op, ast.Eq):
                    diags.append(
                        Diagnostic(node, "compare to None with `is`, not `==`")
                    )
                elif isinstance(op, ast.NotEq):
                    diags.append(
                        Diagnostic(node, "compare to None with `is not`, not `!=`")
                    )
        return diags
