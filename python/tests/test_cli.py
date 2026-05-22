"""End-to-end tests for the `nib` CLI, including loading the `demo/` package
via --rules. Uses `python -m nib.cli` rather than the `nib` script so tests
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


def test_check_dir_with_demo_rules_flags_print():
    result = _run("check", "demo", "--rules", "demo")
    assert result.returncode == 1
    assert "DEMO001 no print()" in result.stdout
    assert "demo/sample.py:5:4" in result.stdout


def test_check_single_file_with_demo_rules():
    result = _run("check", "demo/sample.py", "--rules", "demo")
    assert result.returncode == 1
    assert result.stdout.splitlines() == ["demo/sample.py:5:4: DEMO001 no print()"]


def test_clean_dir_exits_zero_with_no_output(tmp_path):
    (tmp_path / "clean.py").write_text("x = 1\n")
    result = _run("check", str(tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""


def test_builtin_rule_fires_without_extra_rules_flag(tmp_path):
    (tmp_path / "bad.py").write_text('eval("x")\n')
    result = _run("check", str(tmp_path))
    assert result.returncode == 1
    assert "X001 no eval" in result.stdout


def test_unknown_rules_module_fails_cleanly():
    result = _run("check", "demo", "--rules", "does_not_exist_xyz")
    assert result.returncode == 2
    assert "failed to import" in result.stderr
    assert "does_not_exist_xyz" in result.stderr
