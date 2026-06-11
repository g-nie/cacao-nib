from pathlib import Path

import nib
import nib.analysis

# Placeholder path for tests that lint in-memory source with no relative imports,
# so the file is never actually stat-walked (only relative imports trigger that).
MODULE = Path("module.py")


class _CaptureCalls(nib.Rule):
    """Records `self.resolve(call.func)` for every Call, in walk order."""

    code = "CAP"

    def __init__(self):
        self.resolved = []

    def visit_Call(self, node):
        self.resolved.append(self.resolve(node.func))


def _resolved(src):
    rule = _CaptureCalls()
    nib.run(nib.ast.parse(src), [rule], MODULE)
    return rule.resolved


def _resolved_in(file):
    """Resolve over a real on-disk module, so relative imports resolve against
    the `__package__` derived from the file's location."""
    rule = _CaptureCalls()
    nib.run(nib.ast.parse(file.read_text()), [rule], file=file)
    return rule.resolved


def test_import_origin_forms():
    src = (
        "import a.b.c\n"  # binds top name `a`
        "import a.b as c\n"
        "from x.y import z\n"
        "from x.y import w as v\n"
    )
    assert nib.analysis._collect_imports(nib.ast.parse(src), MODULE) == {
        "a": "a",
        "c": "a.b",
        "z": "x.y.z",
        "v": "x.y.w",
    }


def test_resolve_attribute_chain():
    assert _resolved("import numpy as np\nnp.array([1])\n") == ["numpy.array"]


def test_submodule_import_deep_resolve():
    assert _resolved("import a.b.c\na.b.c.f()\n") == ["a.b.c.f"]


def test_bare_from_import_name_resolves():
    assert _resolved("from a.b import c\nc()\n") == ["a.b.c"]


def test_top_level_compound_imports_are_collected():
    # Imports under module-level if/try are module scope; def/class bodies aren't.
    src = (
        "import typing\n"
        "if typing.TYPE_CHECKING:\n"
        "    from a import b\n"
        "try:\n"
        "    import fast as impl\n"
        "except ImportError:\n"
        "    import slow as impl\n"
    )
    table = nib.analysis._collect_imports(nib.ast.parse(src), MODULE)
    assert table["b"] == "a.b"
    assert table["impl"] == "slow"  # last binding wins


def test_flat_table_ignores_local_imports_and_shadowing():
    # Documented limits of the module-scope table (no scope model): a function-
    # local import isn't seen, and a param sharing an import's name still
    # resolves to the import.
    assert _resolved("def f():\n    import numpy as np\n    np.array()\n") == [None]
    assert _resolved("import json\ndef f(json):\n    json.dumps(x)\n") == ["json.dumps"]


def test_resolve_none_for_non_name_base_and_unknown():
    assert set(_resolved("foo().bar()\nunknown.thing()\n")) == {None}


def test_imports_attribute_is_module_scope_only():
    captured = {}

    class Grab(nib.Rule):
        code = "G"

        def visit_Module(self, node):
            captured.update(self.imports)  # only populated during the walk

    nib.run(
        nib.ast.parse("import os\nfrom x import y\ndef f():\n    import sys\n"),
        [Grab()],
        MODULE,
    )
    assert captured == {"os": "os", "y": "x.y"}  # function-local `sys` excluded


# --- relative imports --------------------------------------------------------


def _make_package(tmp_path, src):
    """Write `src` to proj/sub/mod.py under an installed package layout and
    return the module's path (so its `__package__` resolves to "proj.sub")."""
    sub = tmp_path / "proj" / "sub"
    sub.mkdir(parents=True)
    (tmp_path / "proj" / "__init__.py").write_text("")
    (sub / "__init__.py").write_text("")
    mod = sub / "mod.py"
    mod.write_text(src)
    return mod


def test_relative_imports_resolve_against_package(tmp_path):
    mod = _make_package(
        tmp_path,
        "from . import sibling\n"
        "from ..pkg import thing\n"
        "from .other import func\n"
        "sibling()\n"
        "thing()\n"
        "func()\n",
    )
    assert _resolved_in(mod) == [
        "proj.sub.sibling",
        "proj.pkg.thing",
        "proj.sub.other.func",
    ]


def test_relative_beyond_top_level_is_unresolved(tmp_path):
    # `from ...` is level 3, but proj.sub is only 2 deep -> beyond top-level.
    mod = _make_package(tmp_path, "from ... import x\nx()\n")
    assert _resolved_in(mod) == [None]


def test_relative_without_package_is_unresolved(tmp_path):
    # A bare script not inside any package (no __init__.py) has no __package__.
    script = tmp_path / "script.py"
    script.write_text("from . import x\nx()\n")
    assert _resolved_in(script) == [None]


def test_module_package_walks_init_files(tmp_path):
    sub = tmp_path / "proj" / "sub"
    sub.mkdir(parents=True)
    (tmp_path / "proj" / "__init__.py").write_text("")
    (sub / "__init__.py").write_text("")
    (sub / "mod.py").write_text("")
    (tmp_path / "script.py").write_text("")

    assert nib.analysis._module_package(sub / "mod.py") == "proj.sub"
    assert nib.analysis._module_package(sub / "__init__.py") == "proj.sub"
    assert nib.analysis._module_package(tmp_path / "script.py") is None
