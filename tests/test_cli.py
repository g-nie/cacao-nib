"""End-to-end tests for the `nib` CLI, including loading the `demo/` package
via --plugins. Drives `nib.cli.main()` in-process so coverage sees the lines."""

import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest

import nib
import nib.cli
from nib.cli import main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def _run(monkeypatch, capsys):
    """Invoke `nib.cli.main()` in-process, returning a `CompletedProcess`-shaped
    namespace. Snapshots `Rule._registry`, `sys.path`, and `sys.modules` so each
    test starts with a clean rule registry and freshly-imported plugins."""
    # Start each CLI test with an empty registry — other test modules define
    # Rule subclasses at import time, and `_validate_registry` would reject
    # any leaked rule that lacks a `code`.
    registry_snapshot = list(nib.Rule._registry)
    nib.Rule._registry.clear()
    path_snapshot = list(sys.path)
    modules_snapshot = set(sys.modules)
    monkeypatch.setattr(nib.cli, "_USE_COLOR", False)

    def run(*args: str, cwd: Path = PROJECT_ROOT) -> SimpleNamespace:
        monkeypatch.chdir(cwd)
        monkeypatch.setattr(sys, "argv", ["nib", *args])
        nib.cli._config_nib.cache_clear()
        with warnings.catch_warnings():
            warnings.resetwarnings()
            warnings.simplefilter("always")
            try:
                rc = main()
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
        out, err = capsys.readouterr()
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    yield run

    nib.Rule._registry[:] = registry_snapshot
    sys.path[:] = path_snapshot
    for m in set(sys.modules) - modules_snapshot:
        del sys.modules[m]
    nib.cli._config_nib.cache_clear()


def test_check_dir_with_demo_plugin_flags_all_demo_codes(_run):
    result = _run("check", "demo", "--plugins", "demo.rules")
    assert result.returncode == 1
    # All five demo rules fire on demo/sample.py.
    codes = {
        line.split(" error[")[1].split("]")[0]
        for line in result.stdout.splitlines()
        if " error[" in line
    }
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


def test_check_single_file_full_output(_run):
    result = _run("check", "demo/sample.py", "--plugins", "demo.rules")
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "demo/sample.py:5:5: error[DEMO001] no print()",
        "demo/sample.py:8:7: error[DEMO002] lambda has 4 args, max 3 — use def",
        "demo/sample.py:12:12: error[DEMO003] or-chain of 4 — prefer `in {...}`",
        # DEMO004 fires twice — `"hello, " + name + "!"` is two nested BinOps,
        # each with a string operand, so both match the rule independently.
        "demo/sample.py:16:12: error[DEMO004] string concat — use an f-string or .join",
        "demo/sample.py:16:12: error[DEMO004] string concat — use an f-string or .join",
        "demo/sample.py:20:8: error[DEMO005] compare to None with `is`, not `==`",
        "demo/sample.py:25:1: error[DEMO006] function 'list' shadows a builtin",
        "demo/sample.py:29:1: error[DEMO007] class name 'bad_class' "
        "should be PascalCase",
        "demo/sample.py:34:5: error[DEMO008] chained assignment with 2 targets "
        "— split it",
        "demo/sample.py:38:1: error[DEMO009] function 'configure' has 7 parameters, "
        "max 5",
        "Found 10 issues.",
    ]


def test_check_uses_pyproject_tool_nib_plugins_without_flag(_run, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.nib]\nplugins = ["myrules"]\n')
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


def test_no_plugins_exits_clean(_run, tmp_path):
    # nib ships zero rules — without plugins, every file is "clean."
    (tmp_path / "anything.py").write_text('eval("x")\n')
    result = _run("check", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


def test_missing_path_fails_cleanly(_run):
    result = _run("check", "does/not/exist.py")
    assert result.returncode == 2
    assert "path does not exist" in result.stderr
    assert "does/not/exist.py" in result.stderr


def test_warns_on_visit_method_targeting_unknown_ast_class(_run, tmp_path):
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


def test_plugin_with_syntax_error_emits_invalid_syntax_diagnostic(_run, tmp_path):
    (tmp_path / "badsyntax.py").write_text("def oops(:\n")
    (tmp_path / "clean.py").write_text("x = 1\n")
    result = _run("check", "clean.py", "--plugins", "badsyntax", cwd=tmp_path)
    assert result.returncode == 1
    assert "error[invalid-syntax]" in result.stdout
    assert "badsyntax.py" in result.stdout


def test_source_file_with_syntax_error_emits_invalid_syntax_diagnostic(_run, tmp_path):
    _three_rule_plugin(tmp_path)
    (tmp_path / "broken.py").write_text("def oops(:\n")
    result = _run("check", "broken.py", "--plugins", "multirule", cwd=tmp_path)
    assert result.returncode == 1
    assert "broken.py" in result.stdout
    assert "error[invalid-syntax]" in result.stdout


def test_unknown_plugin_module_fails_cleanly(_run):
    result = _run("check", "demo", "--plugins", "does_not_exist_xyz")
    assert result.returncode == 2
    assert "failed to import" in result.stderr
    assert "does_not_exist_xyz" in result.stderr


def _three_rule_plugin(tmp_path):
    """Plugin with three rules: AAA001/AAA002 in group AAA, BBB001 in group BBB."""
    (tmp_path / "multirule.py").write_text(
        "from nib import Diagnostic, Rule\n"
        "class A1(Rule):\n"
        "    code = 'AAA001'\n"
        "    group = 'AAA'\n"
        "    def visit_Name(self, node):\n"
        "        if node.id == 'a': return [Diagnostic(node, 'a')]\n"
        "class A2(Rule):\n"
        "    code = 'AAA002'\n"
        "    group = 'AAA'\n"
        "    def visit_Name(self, node):\n"
        "        if node.id == 'a': return [Diagnostic(node, 'a')]\n"
        "class B1(Rule):\n"
        "    code = 'BBB001'\n"
        "    group = 'BBB'\n"
        "    def visit_Name(self, node):\n"
        "        if node.id == 'a': return [Diagnostic(node, 'a')]\n"
    )
    (tmp_path / "x.py").write_text("a\n")


def _error_lines(stdout: str) -> list[str]:
    """Diagnostic lines only — drops the trailing `Found N issues.` summary."""
    return [line for line in stdout.splitlines() if "error[" in line]


def _codes(stdout: str) -> set[str]:
    return {line.split("error[")[1].split("]")[0] for line in _error_lines(stdout)}


def test_select_filters_to_named_codes(_run, tmp_path):
    _three_rule_plugin(tmp_path)
    result = _run(
        "check", "x.py", "--plugins", "multirule", "--select", "AAA001", cwd=tmp_path
    )
    assert _codes(result.stdout) == {"AAA001"}


def test_select_supports_group_match(_run, tmp_path):
    _three_rule_plugin(tmp_path)
    result = _run(
        "check", "x.py", "--plugins", "multirule", "--select", "AAA", cwd=tmp_path
    )
    assert _codes(result.stdout) == {"AAA001", "AAA002"}


def test_ignore_drops_codes(_run, tmp_path):
    _three_rule_plugin(tmp_path)
    result = _run(
        "check",
        "x.py",
        "--plugins",
        "multirule",
        "--ignore",
        "AAA001,BBB",
        cwd=tmp_path,
    )
    assert _codes(result.stdout) == {"AAA002"}


def test_unknown_token_matches_nothing(_run, tmp_path):
    # "ZZZ" is neither a registered code nor a group → silently no-op,
    # which combined with --select means *nothing* is selected.
    _three_rule_plugin(tmp_path)
    result = _run(
        "check", "x.py", "--plugins", "multirule", "--select", "ZZZ", cwd=tmp_path
    )
    assert result.returncode == 0
    assert _codes(result.stdout) == set()


def test_errors_on_rule_without_code(_run, tmp_path):
    (tmp_path / "codeless.py").write_text(
        "from nib import Diagnostic, Rule\n"
        "class NoCode(Rule):\n"  # no `code` attribute set
        "    def visit_Name(self, node):\n"
        "        return [Diagnostic(node, 'noop')]\n"
    )
    (tmp_path / "clean.py").write_text("pass\n")
    result = _run("check", "clean.py", "--plugins", "codeless", cwd=tmp_path)
    assert result.returncode == 2
    assert "without a `code` attribute" in result.stderr
    assert "NoCode" in result.stderr


def test_code_group_collision_errors(_run, tmp_path):
    (tmp_path / "collide.py").write_text(
        "from nib import Diagnostic, Rule\n"
        "class R1(Rule):\n"
        "    code = 'X'\n"  # collides with R2.group
        "    def visit_Name(self, node):\n"
        "        return [Diagnostic(node, '')]\n"
        "class R2(Rule):\n"
        "    code = 'X001'\n"
        "    group = 'X'\n"  # collides with R1.code
        "    def visit_Name(self, node):\n"
        "        return [Diagnostic(node, '')]\n"
    )
    (tmp_path / "f.py").write_text("a\n")
    result = _run("check", "f.py", "--plugins", "collide", cwd=tmp_path)
    assert result.returncode == 2
    assert "both a code and a group" in result.stderr
    assert "'X'" in result.stderr


def test_duplicate_code_errors(_run, tmp_path):
    (tmp_path / "dupes.py").write_text(
        "from nib import Diagnostic, Rule\n"
        "class R1(Rule):\n"
        "    code = 'X001'\n"
        "    def visit_Name(self, node):\n"
        "        return [Diagnostic(node, '')]\n"
        "class R2(Rule):\n"
        "    code = 'X001'\n"  # same code as R1
        "    def visit_Name(self, node):\n"
        "        return [Diagnostic(node, '')]\n"
    )
    (tmp_path / "f.py").write_text("a\n")
    result = _run("check", "f.py", "--plugins", "dupes", cwd=tmp_path)
    assert result.returncode == 2
    assert "more than one rule" in result.stderr
    assert "'X001'" in result.stderr
    assert "R1" in result.stderr and "R2" in result.stderr


def test_ignore_wins_over_select(_run, tmp_path):
    _three_rule_plugin(tmp_path)
    result = _run(
        "check",
        "x.py",
        "--plugins",
        "multirule",
        "--select",
        "AAA001",
        "--ignore",
        "AAA001",
        cwd=tmp_path,
    )
    assert result.returncode == 0
    assert _codes(result.stdout) == set()


def test_config_select_used_when_no_flag(_run, tmp_path):
    _three_rule_plugin(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.nib]\nplugins = ["multirule"]\nselect = ["AAA001"]\n'
    )
    result = _run("check", "x.py", cwd=tmp_path)
    assert _codes(result.stdout) == {"AAA001"}


def test_cli_select_replaces_config_select(_run, tmp_path):
    _three_rule_plugin(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.nib]\nplugins = ["multirule"]\nselect = ["AAA001"]\n'
    )
    result = _run("check", "x.py", "--select", "BBB001", cwd=tmp_path)
    assert _codes(result.stdout) == {"BBB001"}


def test_rules_subcommand_lists_groups_and_codes(_run, tmp_path):
    _three_rule_plugin(tmp_path)
    result = _run("rules", "--plugins", "multirule", cwd=tmp_path)
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    # Group headers + their rules appear in sorted order.
    assert "AAA" in lines
    assert "BBB" in lines
    assert any("AAA001" in line and "A1" in line for line in lines)
    assert any("AAA002" in line and "A2" in line for line in lines)
    assert any("BBB001" in line and "B1" in line for line in lines)


def test_rules_subcommand_groups_ungrouped_under_no_group(_run, tmp_path):
    (tmp_path / "mixed.py").write_text(
        "from nib import Diagnostic, Rule\n"
        "class Grouped(Rule):\n"
        "    code = 'G1'\n"
        "    group = 'GRP'\n"
        "    def visit_Name(self, node): return []\n"
        "class Loose(Rule):\n"  # no `group` set
        "    '''first line of docstring\n\nrest is ignored'''\n"
        "    code = 'L1'\n"
        "    def visit_Name(self, node): return []\n"
    )
    result = _run("rules", "--plugins", "mixed", cwd=tmp_path)
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert "GRP" in lines
    assert "(no group)" in lines
    # The "(no group)" bucket is rendered last.
    assert lines.index("GRP") < lines.index("(no group)")
    assert any(
        "L1" in line and "Loose" in line and "first line of docstring" in line
        for line in lines
    )


def test_walk_prunes_default_excluded_dirs(_run, tmp_path):
    """A recursive `nib check .` should skip files inside .venv/, __pycache__/, etc."""
    (tmp_path / "pyproject.toml").write_text('[tool.nib]\nplugins = ["multirule"]\n')
    _three_rule_plugin(tmp_path)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "trapped.py").write_text("a\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "trapped.py").write_text("a\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "trapped.py").write_text("a\n")
    result = _run("check", ".", cwd=tmp_path)
    assert result.returncode == 1
    # x.py at the root fires; nothing from the excluded dirs should appear.
    files = {line.split(":", 1)[0] for line in _error_lines(result.stdout)}
    assert files == {"x.py"}


def test_explicit_path_bypasses_default_excludes(_run, tmp_path):
    """Default behavior: passing a file inside an excluded dir lints it anyway."""
    (tmp_path / "pyproject.toml").write_text('[tool.nib]\nplugins = ["multirule"]\n')
    _three_rule_plugin(tmp_path)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "explicit.py").write_text("a\n")
    result = _run("check", ".venv/explicit.py", cwd=tmp_path)
    assert result.returncode == 1
    assert ".venv/explicit.py" in result.stdout


def test_force_exclude_applies_to_explicit_paths(_run, tmp_path):
    """`--force-exclude` makes explicit paths honor the exclude list too."""
    (tmp_path / "pyproject.toml").write_text('[tool.nib]\nplugins = ["multirule"]\n')
    _three_rule_plugin(tmp_path)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "explicit.py").write_text("a\n")
    result = _run("check", ".venv/explicit.py", "--force-exclude", cwd=tmp_path)
    assert result.returncode == 0
    assert "error[" not in result.stdout


def test_extend_select_appends_to_config(_run, tmp_path):
    _three_rule_plugin(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.nib]\nplugins = ["multirule"]\nselect = ["AAA001"]\n'
    )
    result = _run("check", "x.py", "--extend-select", "BBB001", cwd=tmp_path)
    assert _codes(result.stdout) == {"AAA001", "BBB001"}


def _noqa_plugin(tmp_path):
    _three_rule_plugin(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[tool.nib]\nplugins = ["multirule"]\n')


def test_bare_noqa_blankets_its_line_and_does_not_leak(_run, tmp_path):
    # Line 1 has bare `# noqa` (blanket) — should silence all three rules.
    # Line 2 has no directive — should still fire all three.
    _noqa_plugin(tmp_path)
    (tmp_path / "x.py").write_text("a  # noqa\na\n")
    result = _run("check", "x.py", cwd=tmp_path)
    assert result.returncode == 1  # line 2's unsuppressed diags
    assert all(":2:" in line for line in _error_lines(result.stdout))
    assert _codes(result.stdout) == {"AAA001", "AAA002", "BBB001"}


def test_noqa_with_code_list_only_suppresses_listed(_run, tmp_path):
    _noqa_plugin(tmp_path)
    (tmp_path / "x.py").write_text("a  # noqa: AAA001, BBB001\n")
    result = _run("check", "x.py", cwd=tmp_path)
    assert _codes(result.stdout) == {"AAA002"}


def test_noqa_keyword_is_case_insensitive(_run, tmp_path):
    # The `noqa` keyword matches case-insensitively; rule codes themselves
    # are matched literally.
    _noqa_plugin(tmp_path)
    (tmp_path / "x.py").write_text("a  # NoQA: AAA001\n")
    result = _run("check", "x.py", cwd=tmp_path)
    assert _codes(result.stdout) == {"AAA002", "BBB001"}


def test_noqa_inside_string_is_a_known_false_positive(_run, tmp_path):
    # KNOWN LIMITATION of the regex-based scanner: it cannot tell a real
    # `# noqa` comment from the text "# noqa" sitting inside a string
    # literal. We accept this to skip a full tokenize pass on every file.
    # Real-world occurrences are vanishingly rare.
    _noqa_plugin(tmp_path)
    (tmp_path / "x.py").write_text('a = "# noqa"\na\n')
    result = _run("check", "x.py", cwd=tmp_path)
    # Line 1's diags get suppressed by the in-string false positive; line 2
    # still fires all three rules.
    assert all(":2:" in line for line in _error_lines(result.stdout))
    assert _codes(result.stdout) == {"AAA001", "AAA002", "BBB001"}


def test_noqa_with_empty_colon_suppresses_nothing(_run, tmp_path):
    # `# noqa:` with no codes is a malformed directive — not the blanket form.
    # Only bare `# noqa` (no colon) blankets the line.
    _noqa_plugin(tmp_path)
    (tmp_path / "x.py").write_text("a  # noqa:\n")
    result = _run("check", "x.py", cwd=tmp_path)
    assert _codes(result.stdout) == {"AAA001", "AAA002", "BBB001"}
