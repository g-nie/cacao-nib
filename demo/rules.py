from nib import UnimportedDiagnostic, Diagnostic, Rule, ast


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
        # Starts with uppercase, alphanumeric only, no underscores.
        is_pascal_case = (
            bool(node.name) and node.name[0].isupper() and node.name.isalnum()
        )
        if not is_pascal_case:
            return [Diagnostic(node, f"class name {node.name!r} should be PascalCase")]


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


class NoPickleLoads(Rule):
    code = "DEMO010"
    group = "DEMO"

    def visit_Call(self, node):
        if self.resolve(node.func) in {"pickle.loads", "pickle.load"}:
            return [Diagnostic(node, "pickle.load(s) is unsafe on untrusted data")]


class NoUnimportedSetup(Rule):
    """Cross-file rule: flags a `setup()` entry point that no other file imports
    by name.

    Shows the cross-file surfaces composing: `self.module` is this file's dotted
    name, so `f"{self.module}.{node.name}"` is the function's fully-qualified path,
    and the `UnimportedDiagnostic` defers the "is it imported anywhere" verdict
    to the end of the run."""

    code = "DEMO011"
    group = "DEMO"

    def visit_FunctionDef(self, node):
        if node.name == "setup":
            qualified = f"{self.module}.{node.name}"
            return [
                UnimportedDiagnostic(node, f"{qualified} is never imported", qualified)
            ]


class TooManyFunctions(Rule):
    """Per-file lifecycle rule: counts module-level-and-nested function defs and
    flags a module with too many.

    Shows the `enter_module`/`leave_module` hooks: `enter_module` resets the
    counter for each file (one `Rule` instance is reused across the whole run),
    `visit_FunctionDef` accumulates, and `leave_module` emits once it has seen the
    whole module."""

    code = "DEMO012"
    group = "DEMO"

    def enter_module(self, node):
        self.def_count = 0

    def visit_FunctionDef(self, node):
        self.def_count += 1

    def leave_module(self, node):
        if self.def_count > 50:
            return [Diagnostic(node, f"module has {self.def_count} functions, max 50")]
