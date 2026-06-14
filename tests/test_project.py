from pathlib import Path

import nib
from nib import (
    ImportedDiagnostic,
    UnimportedDiagnostic,
    Diagnostic,
    ast,
)
from nib.engine import _check_file

# Placeholder path for in-memory source with no relative imports (never stat-walked).
MODULE = Path("module.py")


# --- self.module: the current file's dotted name --------------------------


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
