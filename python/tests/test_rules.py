import nib
from nib import Diagnostic, ast


class NoEval(nib.Rule):
    code = "X001"

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "eval":
            return [Diagnostic(node, "no eval")]


class NoPrint(nib.Rule):
    code = "X002"

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            return [Diagnostic(node, "no print")]


class CountNames(nib.Rule):
    def visit_Name(self, node):
        return [node.id]  # not a Diagnostic — should pass through untouched


class ModuleVisitor(nib.Rule):
    code = "MOD"

    def visit_Module(self, node):
        return [Diagnostic(node, f"saw {len(node.body)} statements")]


def test_visitor_fires_on_matching_node():
    mod = nib.parse_module("eval('x')\nprint('ok')\neval(y)\n")
    diags = nib.run(mod, [NoEval()])
    assert len(diags) == 2
    assert all(isinstance(d, Diagnostic) for d in diags)
    assert [(d.code, d.message, d.lineno) for d in diags] == [
        ("X001", "no eval", 1),
        ("X001", "no eval", 3),
    ]


def test_visitor_no_match():
    mod = nib.parse_module("print('hi')\n")
    diags = nib.run(mod, [NoEval()])
    assert diags == []


def test_visitor_multiple_rules_distinct_codes():
    mod = nib.parse_module("eval('x')\nprint('y')\n")
    diags = nib.run(mod, [NoEval(), NoPrint()])
    codes = sorted(d.code for d in diags)
    assert codes == ["X001", "X002"]


def test_non_diagnostic_returns_pass_through():
    mod = nib.parse_module("eval(eval('x'))\n")
    diags = nib.run(mod, [CountNames()])
    assert diags == ["eval", "eval"]


def test_visit_module_fires_once():
    mod = nib.parse_module("eval(1)\neval(2)\n")
    diags = nib.run(mod, [ModuleVisitor()])
    assert len(diags) == 1
    assert diags[0].code == "MOD"
    assert diags[0].message == "saw 2 statements"


def test_diagnostic_span_pulled_from_node():
    mod = nib.parse_module("    eval('hi')\n")
    diags = nib.run(mod, [NoEval()])
    assert len(diags) == 1
    d = diags[0]
    assert d.lineno == 1
    assert d.col_offset == 4
    assert d.end_lineno == 1
    assert d.end_col_offset == 14


def test_multiple_diagnostics_from_one_node():
    """A single visit_* call can return more than one diagnostic."""

    class FlagEachArg(nib.Rule):
        code = "ARG"

        def visit_Call(self, node):
            return [Diagnostic(arg, "arg") for arg in node.args]

    mod = nib.parse_module("foo('a', 'b', 'c')\n")
    diags = nib.run(mod, [FlagEachArg()])
    assert len(diags) == 3
    assert all(d.code == "ARG" for d in diags)
