from pathlib import Path

import nib
import nib.analysis
from nib import (
    ImportedDiagnostic,
    UnimportedDiagnostic,
    Diagnostic,
    ast,
)
from nib.analysis import imported_among
from nib.engine import _check_file

# Placeholder path for in-memory source with no relative imports (never stat-walked).
MODULE = Path("module.py")


# --- _collect_import_targets: the import manifest --------------------------------


def _targets(src, file=MODULE):
    return nib.analysis._collect_import_targets(ast.parse(src), file)


def test_collect_import_targets_import_forms():
    assert _targets("import a.b.c\n") == {"a.b.c"}
    assert _targets("import a.b as x\n") == {"a.b"}
    assert _targets("from a.b import c, d\n") == {"a.b", "a.b.c", "a.b.d"}


def test_collect_import_targets_star_keeps_only_from_module():
    assert _targets("from a.b import *\n") == {"a.b"}


def test_collect_import_targets_sees_function_local():
    # Unlike the module-scope name table, the manifest sees function-local
    # imports — a registering import is often deferred inside a function body.
    src = "def register():\n    import myapp.plugin\n"
    assert _targets(src) == {"myapp.plugin"}


def test_collect_import_targets_resolves_relative(make_package):
    mod = make_package("from . import sibling\nfrom ..pkg import thing\n")
    assert _targets(mod.read_text(), mod) == {
        "proj.sub",
        "proj.sub.sibling",
        "proj.pkg",
        "proj.pkg.thing",
    }


# --- imported_among: which queried modules are imported -------------------


def test_imported_among_keeps_only_queried_and_imported():
    # Of the modules a deferred finding asks about, keep those some file imports:
    # `shared` is queried and imported; `b` is queried but never imported; `os` is
    # imported but not queried, so it's never materialised. Targets come from the
    # check pass.
    targets_per_file = [set(), {"shared", "os"}]  # a file imports shared and os
    assert imported_among({"shared", "b"}, targets_per_file) == frozenset({"shared"})


# --- self.module: the current file's dotted name --------------------------


def test_module_name_forms(tmp_path):
    sub = tmp_path / "pkg" / "sub"
    sub.mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (sub / "__init__.py").write_text("")
    (sub / "mod.py").write_text("")
    (tmp_path / "script.py").write_text("")

    name = nib.analysis._module_name
    assert name(sub / "mod.py") == "pkg.sub.mod"
    assert name(sub / "__init__.py") == "pkg.sub"
    assert name(tmp_path / "script.py") == "script"


def test_module_property_reflects_file(make_package):
    captured = {}
    mod = make_package("")

    class Grab(nib.Rule):
        code = "M"

        def visit_Module(self, node):
            captured["module"] = self.module

    nib.run(ast.parse(""), [Grab()], mod)
    assert captured["module"] == "proj.sub.mod"


# --- deferred diagnostics --------------------------------------------------


def test_run_resolves_deferred_against_imported():
    class R(nib.Rule):
        code = "R"

        def visit_Module(self, node):
            return [UnimportedDiagnostic(node, "x", "pkg.mod")]

    mod = ast.parse("")
    # unimported polarity: kept when pkg.mod is imported nowhere ...
    kept = nib.run(mod, [R()], MODULE, imported=frozenset())
    assert len(kept) == 1 and kept[0].code == "R"
    # ... dropped when pkg.mod is imported.
    assert nib.run(mod, [R()], MODULE, imported=frozenset({"pkg.mod"})) == []


def test_deferred_imported_polarity():
    class R(nib.Rule):
        code = "R"

        def visit_Module(self, node):
            return [ImportedDiagnostic(node, "x", "pkg.mod")]

    mod = ast.parse("")
    # imported polarity is the inverse: kept only when pkg.mod IS imported.
    assert nib.run(mod, [R()], MODULE, imported=frozenset()) == []
    assert len(nib.run(mod, [R()], MODULE, imported=frozenset({"pkg.mod"}))) == 1


def test_check_file_reports_targets_and_deferred(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("import os\n")

    class R(nib.Rule):
        code = "R"

        def visit_Module(self, node):
            return [
                Diagnostic(node, "immediate"),
                UnimportedDiagnostic(node, "deferred", self.module),
            ]

    _fs, _src, diag_tuples, err, targets, deferred = _check_file(f, [R()])
    assert err is None
    assert "os" in targets
    assert [d[4] for d in diag_tuples] == ["immediate"]  # immediate channel
    # deferred wire: (lineno, col, end_lineno, end_col, message, code, module, keep)
    assert len(deferred) == 1
    assert deferred[0][4:8] == ("deferred", "R", "m", False)


def test_module_and_deferred_compose(make_package):
    # The DEMO011 composition: `self.module` names the function's module, so its
    # fully-qualified path gates a deferred finding on that function being
    # imported (by name) nowhere.
    class OrphanSetup(nib.Rule):
        code = "R"

        def visit_FunctionDef(self, node):
            if node.name == "setup":
                qualified = f"{self.module}.{node.name}"
                return [UnimportedDiagnostic(node, "orphan", qualified)]

    plugin = make_package("def setup():\n    pass\n")  # module name: proj.sub.mod
    mod = ast.parse(plugin.read_text())

    # Imported nowhere -> flagged; imported by name -> silent.
    assert len(nib.run(mod, [OrphanSetup()], plugin, imported=frozenset())) == 1
    assert (
        nib.run(
            mod, [OrphanSetup()], plugin, imported=frozenset({"proj.sub.mod.setup"})
        )
        == []
    )
