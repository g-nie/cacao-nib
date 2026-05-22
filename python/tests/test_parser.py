import nib
from nib import ast


def test_parse_call_and_name():
    mod = nib.parse_module("eval('x')\n")
    assert isinstance(mod, ast.Module)
    body = mod.body
    assert len(body) == 1
    call = body[0]
    assert isinstance(call, ast.Call)
    assert call.lineno == 1
    assert isinstance(call.func, ast.Name)
    assert call.func.id == "eval"
    assert len(call.args) == 1
    assert isinstance(call.args[0], ast.Constant)
    assert call.args[0].value == "x"


def test_attribute_chain():
    mod = nib.parse_module("os.path.join('a', 'b')\n")
    call = mod.body[0]
    assert isinstance(call, ast.Call)
    func = call.func
    assert isinstance(func, ast.Attribute)
    assert func.attr == "join"
    inner = func.value
    assert isinstance(inner, ast.Attribute)
    assert inner.attr == "path"
    assert isinstance(inner.value, ast.Name)
    assert inner.value.id == "os"


def test_unsupported_node_kind_skipped_in_body():
    # list comprehension isn't wrapped yet; the lenient walk should skip it,
    # leaving body empty rather than raising.
    mod = nib.parse_module("[x for x in y]\n")
    assert mod.body == []
