"""End-to-end tests for the `nib` CLI, including loading the `demo/` package
via --plugins. Uses `python -m nib.cli` rather than the `nib` script so tests
don't depend on PATH layout."""

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, cwd: Path = PROJECT_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "nib.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def test_check_dir_with_demo_plugin_flags_all_demo_codes():
    result = _run("check", "demo", "--plugins", "demo")
    assert result.returncode == 1
    # All five demo rules fire on demo/sample.py.
    codes = {line.split(" error[")[1].split("]")[0]
             for line in result.stdout.splitlines() if " error[" in line}
    assert codes == {
        "DEMO001",
        "DEMO002",
        "DEMO003",
        "DEMO004",
        "DEMO005",
        "DEMO006",
        "DEMO007",
        "DEMO008",
        "DEMO009",
    }


def test_check_single_file_full_output():
    result = _run("check", "demo/sample.py", "--plugins", "demo")
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "demo/sample.py:5:4: error[DEMO001] no print()",
        "demo/sample.py:8:6: error[DEMO002] lambda has 4 args, max 3 — use def",
        "demo/sample.py:12:11: error[DEMO003] or-chain of 4 — prefer `in {...}`",
        # DEMO004 fires twice — `"hello, " + name + "!"` is two nested BinOps,
        # each with a string operand, so both match the rule independently.
        "demo/sample.py:16:11: error[DEMO004] string concat — use an f-string or .join",
        "demo/sample.py:16:11: error[DEMO004] string concat — use an f-string or .join",
        "demo/sample.py:20:7: error[DEMO005] compare to None with `is`, not `==`",
        "demo/sample.py:25:0: error[DEMO006] function 'list' shadows a builtin",
        "demo/sample.py:29:0: error[DEMO007] class name 'bad_class' should be PascalCase",
        "demo/sample.py:34:4: error[DEMO008] chained assignment with 2 targets — split it",
        "demo/sample.py:38:0: error[DEMO009] function 'configure' has 7 parameters, max 5",
    ]


def test_check_uses_pyproject_tool_nib_plugins_without_flag(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.nib]\nplugins = ["myrules"]\n'
    )
    (tmp_path / "myrules.py").write_text(
        "from nib import Diagnostic, Rule\n"
        "class NoFoo(Rule):\n"
        '    code = "FOO"\n'
        "    def visit_Name(self, node):\n"
        '        if node.id == "foo":\n'
        '            return [Diagnostic(node, "no foo")]\n'
    )
    (tmp_path / "bad.py").write_text("foo\n")
    result = _run("check", "bad.py", cwd=tmp_path)
    assert result.returncode == 1
    assert "error[FOO] no foo" in result.stdout


def test_no_plugins_exits_clean(tmp_path):
    # nib ships zero rules — without plugins, every file is "clean."
    (tmp_path / "anything.py").write_text('eval("x")\n')
    result = _run("check", str(tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""


def test_missing_path_fails_cleanly():
    result = _run("check", "does/not/exist.py")
    assert result.returncode == 2
    assert "path does not exist" in result.stderr
    assert "does/not/exist.py" in result.stderr


def test_warns_on_visit_method_targeting_unknown_ast_class(tmp_path):
    (tmp_path / "typoplugin.py").write_text(
        "from nib import Diagnostic, Rule\n"
        "class Typo(Rule):\n"
        '    code = "T1"\n'
        "    def visit_Cal(self, node):\n"  # typo of Call
        '        return [Diagnostic(node, "noop")]\n'
    )
    (tmp_path / "clean.py").write_text("x = 1\n")
    result = _run("check", "clean.py", "--plugins", "typoplugin", cwd=tmp_path)
    assert result.returncode == 0
    assert "Typo.visit_Cal" in result.stderr
    assert "'Cal'" in result.stderr


def test_unknown_plugin_module_fails_cleanly():
    result = _run("check", "demo", "--plugins", "does_not_exist_xyz")
    assert result.returncode == 2
    assert "failed to import" in result.stderr
    assert "does_not_exist_xyz" in result.stderr
