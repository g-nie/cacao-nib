import nib
from nib import ast


class NoEval(nib.Rule):
    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id == "eval":
            yield "X001", node.lineno


class CountNames(nib.Rule):
    def visit_Name(self, node):
        yield node.id


class ModuleVisitor(nib.Rule):
    def visit_Module(self, node):
        yield "module-seen", len(node.body)


def test_visitor_fires_on_matching_node():
    mod = nib.parse_module("eval('x')\nprint('ok')\neval(y)\n")
    diags = nib.run(mod, [NoEval()])
    assert diags == [("X001", 1), ("X001", 3)]


def test_visitor_no_match():
    mod = nib.parse_module("print('hi')\n")
    diags = nib.run(mod, [NoEval()])
    assert diags == []


def test_visitor_multiple_rules_and_nesting():
    mod = nib.parse_module("eval(eval('x'))\n")
    diags = nib.run(mod, [NoEval(), CountNames()])
    assert diags.count(("X001", 1)) == 2
    assert diags.count("eval") == 2


def test_visit_module_fires_once():
    mod = nib.parse_module("eval(1)\neval(2)\n")
    diags = nib.run(mod, [ModuleVisitor()])
    assert diags == [("module-seen", 2)]
