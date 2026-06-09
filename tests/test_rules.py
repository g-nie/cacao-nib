import pytest

import nib
import nib.engine
from nib import Diagnostic, ast


@pytest.fixture(autouse=True)
def _reset_warn_dedup():
    # `_warn` dedupes per process (functools.cache); clear it so each test
    # sees its own warning regardless of order.
    nib.engine._warn.cache_clear()


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
        return [node.id]  # not a Diagnostic - should pass through untouched


class ModuleVisitor(nib.Rule):
    code = "MOD"

    def visit_Module(self, node):
        return [Diagnostic(node, f"saw {len(node.body)} statements")]


def test_visitor_fires_on_matching_node():
    mod = nib.ast.parse("eval('x')\nprint('ok')\neval(y)\n")
    diags = nib.run(mod, [NoEval()])
    assert len(diags) == 2
    assert all(isinstance(d, Diagnostic) for d in diags)
    assert [(d.code, d.message, d.lineno) for d in diags] == [
        ("X001", "no eval", 1),
        ("X001", "no eval", 3),
    ]


def test_visitor_no_match():
    mod = nib.ast.parse("print('hi')\n")
    diags = nib.run(mod, [NoEval()])
    assert diags == []


def test_visitor_multiple_rules_distinct_codes():
    mod = nib.ast.parse("eval('x')\nprint('y')\n")
    diags = nib.run(mod, [NoEval(), NoPrint()])
    codes = sorted(d.code for d in diags)
    assert codes == ["X001", "X002"]


def test_non_diagnostic_items_are_dropped_with_warning(capsys):
    mod = nib.ast.parse("eval(eval('x'))\n")
    diags = nib.run(mod, [CountNames()])
    assert diags == []
    err = capsys.readouterr().err
    assert "CountNames.visit_Name" in err and "expected Diagnostic" in err
    # Deduped → fires once across many nodes.
    assert err.count("CountNames.visit_Name") == 1


def test_single_diagnostic_return_wrapped_with_warning(capsys):
    class BadReturn(nib.Rule):
        code = "BAD"

        def visit_Name(self, node):
            return Diagnostic(node, "missing list")

    mod = nib.ast.parse("a; b\n")
    diags = nib.run(mod, [BadReturn()])
    assert len(diags) == 2
    assert all(d.code == "BAD" for d in diags)
    err = capsys.readouterr().err
    assert "BadReturn.visit_Name" in err and "single Diagnostic" in err


def test_non_iterable_return_warns_and_skips(capsys):
    class BadReturn(nib.Rule):
        code = "BAD"

        def visit_Name(self, node):
            return 42  # not None, not a Diagnostic, not iterable

    mod = nib.ast.parse("a\n")
    diags = nib.run(mod, [BadReturn()])
    assert diags == []
    err = capsys.readouterr().err
    assert "BadReturn.visit_Name" in err and "non-iterable int" in err


def test_visit_module_fires_once():
    mod = nib.ast.parse("eval(1)\neval(2)\n")
    diags = nib.run(mod, [ModuleVisitor()])
    assert len(diags) == 1
    assert diags[0].code == "MOD"
    assert diags[0].message == "saw 2 statements"


def test_diagnostic_span_pulled_from_node():
    mod = nib.ast.parse("x = eval('hi')\n")
    diags = nib.run(mod, [NoEval()])
    assert len(diags) == 1
    d = diags[0]
    assert d.lineno == 1
    assert d.col_offset == 5
    assert d.end_lineno == 1
    assert d.end_col_offset == 15


def test_multiple_diagnostics_from_one_node():
    # A single visit_* call can return more than one diagnostic.

    class FlagEachArg(nib.Rule):
        code = "ARG"

        def visit_Call(self, node):
            return [Diagnostic(arg, "arg") for arg in node.args]

    mod = nib.ast.parse("foo('a', 'b', 'c')\n")
    diags = nib.run(mod, [FlagEachArg()])
    assert len(diags) == 3
    assert all(d.code == "ARG" for d in diags)
