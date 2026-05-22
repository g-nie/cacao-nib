"""End-to-end tests for the `nib` CLI, including loading the `demo/` package
via --plugins. Uses `python -m nib.cli` rather than the `nib` script so tests
don't depend on PATH layout."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
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


def test_clean_dir_exits_zero_with_no_output(tmp_path):
    (tmp_path / "clean.py").write_text("x = 1\n")
    result = _run("check", str(tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""


def test_builtin_rule_fires_without_extra_plugin_flag(tmp_path):
    (tmp_path / "bad.py").write_text('eval("x")\n')
    result = _run("check", str(tmp_path))
    assert result.returncode == 1
    assert "error[X001] no eval" in result.stdout


def test_missing_path_fails_cleanly():
    result = _run("check", "does/not/exist.py")
    assert result.returncode == 2
    assert "path does not exist" in result.stderr
    assert "does/not/exist.py" in result.stderr


def test_unknown_plugin_module_fails_cleanly():
    result = _run("check", "demo", "--plugins", "does_not_exist_xyz")
    assert result.returncode == 2
    assert "failed to import" in result.stderr
    assert "does_not_exist_xyz" in result.stderr
