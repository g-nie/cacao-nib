import os
from pathlib import Path

import nib.cli
from helpers import error_lines, noqa_plugin, reported_codes, three_rule_plugin


def test_check_dir_with_demo_plugin_flags_all_demo_codes(run_cli):
    result = run_cli("check", "demo", "--plugins", "demo.rules")
    assert result.returncode == 1
    # Every demo rule except DEMO010 (pickle) fires on demo/sample.py — including
    # the cross-file DEMO011, since nothing imports its `setup`.
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
        "DEMO011",
    }


def test_check_single_file_concise_output(run_cli):
    result = run_cli(
        "check", "demo/sample.py", "--plugins", "demo.rules", "--format", "concise"
    )
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
        # DEMO011 is cross-file (deferred), so it prints after the immediate
        # findings rather than sorted in at line 33.
        "demo/sample.py:33:1: error[DEMO011] sample.setup is never imported",
        "Found 11 issues.",
    ]


def test_format_defaults_to_full():
    assert nib.cli._build_parser().parse_args(["check"]).format == "full"


def test_full_format_renders_snippet_with_caret(run_cli):
    result = run_cli(
        "check", "demo/sample.py", "--plugins", "demo.rules", "--format", "full"
    )
    assert result.returncode == 1
    out = result.stdout
    # Header keeps the `error[CODE]` token; a `-->` location and a caret row follow.
    assert "error[DEMO001] no print()" in out
    assert "  --> demo/sample.py:5:5" in out
    # The flagged source line appears in a gutter, with carets under it.
    assert "5 |     print(f" in out
    caret_rows = [ln for ln in out.splitlines() if ln.lstrip().startswith("| ^")]
    assert any("^^^^" in ln for ln in caret_rows)


def _render_full(tmp_path, capsys, src, lineno, col, end_lineno, end_col):
    # Render one full-format diagnostic over `src` and return the printed lines
    f = tmp_path / "m.py"
    f.write_text(src)
    nib.cli._print_diagnostic_full(str(f), lineno, col, end_lineno, end_col, "C", "msg")
    return capsys.readouterr().out.splitlines()


def test_full_render_single_line_caret_spans_start_to_end_col(tmp_path, capsys):
    out = _render_full(tmp_path, capsys, "x = eval('hi')\n", 1, 5, 1, 14)
    assert out[0] == "error[C] msg"
    assert out[1].endswith("m.py:1:5") and out[1].startswith("  --> ")
    assert "1 | x = eval('hi')" in out
    assert "  |     ^^^^^^^^^" in out  # 9 carets: cols 5..14, under `eval('hi')`


def test_full_render_context_window_and_gutter_alignment(tmp_path, capsys):
    src = "".join(f"line {n}\n" for n in range(1, 13))  # 12 lines
    out = _render_full(tmp_path, capsys, src, 10, 1, 10, 5)
    assert " 8 | line 8" in out  # right-aligned to width 2
    assert "10 | line 10" in out
    assert "12 | line 12" in out
    assert "   | ^^^^" in out  # caret row aligns under the 2-wide gutter


def test_full_render_multi_line_span_carets_only_start_line(tmp_path, capsys):
    out = _render_full(tmp_path, capsys, "def f(\n    a, b,\n): pass\n", 1, 1, 3, 8)
    caret_rows = [ln for ln in out if "^" in ln]
    assert caret_rows == ["  | ^^^^^^"]  # end is on line 3 → only len("def f(") == 6


def test_full_render_missing_end_col_carets_to_end_of_line(tmp_path, capsys):
    out = _render_full(tmp_path, capsys, "abcd\n", 1, 2, None, None)
    caret_rows = [ln for ln in out if "^" in ln]
    assert caret_rows == ["  |  ^^^"]  # cols 2..4 of "abcd"


def test_full_format_separates_diagnostics_with_blank_line(tmp_path, capsys):
    f = tmp_path / "m.py"
    f.write_text("a = 1\nb = 2\n")
    diags = [(1, 1, 1, 2, "C1", "first"), (2, 1, 2, 2, "C2", "second")]
    nib.cli._emit_diags(str(f), diags, "full")
    out = capsys.readouterr().out
    assert "\n\nerror[C2] second" in out  # blank line between the two blocks


def test_full_render_out_of_range_lineno_falls_back(tmp_path, capsys):
    out = _render_full(tmp_path, capsys, "x = 1\n", 99, 1, 99, 2)
    assert out[0] == "error[C] msg"
    assert out[1].endswith("m.py:99:1")
    assert not any("|" in ln for ln in out)  # no snippet block


def test_check_uses_pyproject_tool_nib_plugins_without_flag(run_cli, tmp_path):
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
    result = run_cli("check", "bad.py", cwd=tmp_path)
    assert result.returncode == 1
    assert "error[FOO] no foo" in result.stdout


def test_no_plugins_exits_clean(run_cli, tmp_path):
    # nib ships zero rules — without plugins, every file is "clean."
    (tmp_path / "anything.py").write_text('eval("x")\n')
    result = run_cli("check", str(tmp_path), cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


def test_missing_path_fails_cleanly(run_cli):
    result = run_cli("check", "does/not/exist.py")
    assert result.returncode == 2
    assert "path does not exist" in result.stderr
    assert "does/not/exist.py" in result.stderr


def test_warns_on_visit_method_targeting_unknown_ast_class(run_cli, tmp_path):
    (tmp_path / "typoplugin.py").write_text(
        "from nib import Diagnostic, Rule\n"
        "class Typo(Rule):\n"
        '    code = "T1"\n'
        "    def visit_Cal(self, node):\n"  # typo of Call
        '        return [Diagnostic(node, "noop")]\n'
    )
    (tmp_path / "clean.py").write_text("x = 1\n")
    result = run_cli("check", "clean.py", "--plugins", "typoplugin", cwd=tmp_path)
    assert result.returncode == 0
    assert "Typo.visit_Cal" in result.stderr
    assert "'Cal'" in result.stderr


def test_warns_when_plugin_registers_no_rules(run_cli, tmp_path):
    (tmp_path / "emptyplugin.py").write_text(
        "X = 1  # imports fine, no Rule subclass\n"
    )
    (tmp_path / "clean.py").write_text("x = 1\n")
    result = run_cli("check", "clean.py", "--plugins", "emptyplugin", cwd=tmp_path)
    assert result.returncode == 0
    assert "emptyplugin" in result.stderr
    assert "registered no rules" in result.stderr


def test_plugin_with_syntax_error_emits_invalid_syntax_diagnostic(run_cli, tmp_path):
    (tmp_path / "badsyntax.py").write_text("def oops(:\n")
    (tmp_path / "clean.py").write_text("x = 1\n")
    result = run_cli("check", "clean.py", "--plugins", "badsyntax", cwd=tmp_path)
    assert result.returncode == 1
    assert "error[invalid-syntax]" in result.stdout
    assert "badsyntax.py" in result.stdout


def test_source_file_with_syntax_error_emits_invalid_syntax_diagnostic(
    run_cli, tmp_path
):
    three_rule_plugin(tmp_path)
    (tmp_path / "broken.py").write_text("def oops(:\n")
    result = run_cli("check", "broken.py", "--plugins", "multirule", cwd=tmp_path)
    assert result.returncode == 1
    assert "broken.py" in result.stdout
    assert "error[invalid-syntax]" in result.stdout


def test_unknown_plugin_module_fails_cleanly(run_cli):
    result = run_cli("check", "demo", "--plugins", "does_not_exist_xyz")
    assert result.returncode == 2
    assert "failed to import" in result.stderr
    assert "does_not_exist_xyz" in result.stderr


def test_select_filters_to_named_codes(run_cli, tmp_path):
    three_rule_plugin(tmp_path)
    result = run_cli(
        "check", "x.py", "--plugins", "multirule", "--select", "AAA001", cwd=tmp_path
    )
    assert reported_codes(result.stdout) == {"AAA001"}


def test_select_supports_group_match(run_cli, tmp_path):
    three_rule_plugin(tmp_path)
    result = run_cli(
        "check", "x.py", "--plugins", "multirule", "--select", "AAA", cwd=tmp_path
    )
    assert reported_codes(result.stdout) == {"AAA001", "AAA002"}


def test_ignore_drops_codes(run_cli, tmp_path):
    three_rule_plugin(tmp_path)
    result = run_cli(
        "check",
        "x.py",
        "--plugins",
        "multirule",
        "--ignore",
        "AAA001,BBB",
        cwd=tmp_path,
    )
    assert reported_codes(result.stdout) == {"AAA002"}


def test_unknown_token_matches_nothing(run_cli, tmp_path):
    # "ZZZ" is neither a registered code nor a group → silently no-op,
    # which combined with --select means *nothing* is selected.
    three_rule_plugin(tmp_path)
    result = run_cli(
        "check", "x.py", "--plugins", "multirule", "--select", "ZZZ", cwd=tmp_path
    )
    assert result.returncode == 0
    assert reported_codes(result.stdout) == set()


def test_errors_on_rule_without_code(run_cli, tmp_path):
    (tmp_path / "codeless.py").write_text(
        "from nib import Diagnostic, Rule\n"
        "class NoCode(Rule):\n"  # no `code` attribute set
        "    def visit_Name(self, node):\n"
        "        return [Diagnostic(node, 'noop')]\n"
    )
    (tmp_path / "clean.py").write_text("pass\n")
    result = run_cli("check", "clean.py", "--plugins", "codeless", cwd=tmp_path)
    assert result.returncode == 2
    assert "without a `code` attribute" in result.stderr
    assert "NoCode" in result.stderr


def test_code_group_collision_errors(run_cli, tmp_path):
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
    result = run_cli("check", "f.py", "--plugins", "collide", cwd=tmp_path)
    assert result.returncode == 2
    assert "both a code and a group" in result.stderr
    assert "'X'" in result.stderr


def test_duplicate_code_errors(run_cli, tmp_path):
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
    result = run_cli("check", "f.py", "--plugins", "dupes", cwd=tmp_path)
    assert result.returncode == 2
    assert "more than one rule" in result.stderr
    assert "'X001'" in result.stderr
    assert "R1" in result.stderr and "R2" in result.stderr


def test_ignore_wins_over_select(run_cli, tmp_path):
    three_rule_plugin(tmp_path)
    result = run_cli(
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
    assert reported_codes(result.stdout) == set()


def test_config_select_used_when_no_flag(run_cli, tmp_path):
    three_rule_plugin(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.nib]\nplugins = ["multirule"]\nselect = ["AAA001"]\n'
    )
    result = run_cli("check", "x.py", cwd=tmp_path)
    assert reported_codes(result.stdout) == {"AAA001"}


def test_cli_select_replaces_config_select(run_cli, tmp_path):
    three_rule_plugin(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.nib]\nplugins = ["multirule"]\nselect = ["AAA001"]\n'
    )
    result = run_cli("check", "x.py", "--select", "BBB001", cwd=tmp_path)
    assert reported_codes(result.stdout) == {"BBB001"}


def test_rules_subcommand_lists_groups_and_codes(run_cli, tmp_path):
    three_rule_plugin(tmp_path)
    result = run_cli("rules", "--plugins", "multirule", cwd=tmp_path)
    assert result.returncode == 0
    lines = result.stdout.splitlines()
    # Group headers + their rules appear in sorted order.
    assert "AAA" in lines
    assert "BBB" in lines
    assert any("AAA001" in line and "A1" in line for line in lines)
    assert any("AAA002" in line and "A2" in line for line in lines)
    assert any("BBB001" in line and "B1" in line for line in lines)


def test_rules_subcommand_groups_ungrouped_under_no_group(run_cli, tmp_path):
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
    result = run_cli("rules", "--plugins", "mixed", cwd=tmp_path)
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


def test_walk_prunes_default_excluded_dirs(run_cli, tmp_path):
    """A recursive `nib check .` should skip files inside .venv/, __pycache__/, etc."""
    (tmp_path / "pyproject.toml").write_text('[tool.nib]\nplugins = ["multirule"]\n')
    three_rule_plugin(tmp_path)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "trapped.py").write_text("a\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "trapped.py").write_text("a\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "trapped.py").write_text("a\n")
    result = run_cli("check", ".", cwd=tmp_path)
    assert result.returncode == 1
    # x.py at the root fires; nothing from the excluded dirs should appear.
    files = {line.split(":", 1)[0] for line in error_lines(result.stdout)}
    assert files == {"x.py"}


def test_explicit_path_bypasses_default_excludes(run_cli, tmp_path):
    """Default behavior: passing a file inside an excluded dir lints it anyway."""
    (tmp_path / "pyproject.toml").write_text('[tool.nib]\nplugins = ["multirule"]\n')
    three_rule_plugin(tmp_path)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "explicit.py").write_text("a\n")
    result = run_cli("check", ".venv/explicit.py", cwd=tmp_path)
    assert result.returncode == 1
    assert ".venv/explicit.py" in result.stdout


def test_force_exclude_applies_to_explicit_paths(run_cli, tmp_path):
    """`--force-exclude` makes explicit paths honor the exclude list too."""
    (tmp_path / "pyproject.toml").write_text('[tool.nib]\nplugins = ["multirule"]\n')
    three_rule_plugin(tmp_path)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "explicit.py").write_text("a\n")
    result = run_cli("check", ".venv/explicit.py", "--force-exclude", cwd=tmp_path)
    assert result.returncode == 0
    assert "error[" not in result.stdout


def test_check_accepts_multiple_explicit_paths(run_cli, tmp_path):
    # Pre-commit passes the staged files as separate args; check several at once.
    noqa_plugin(tmp_path)  # multirule via [tool.nib]; fires on a Name `a`
    (tmp_path / "one.py").write_text("a\n")
    (tmp_path / "two.py").write_text("a\n")
    result = run_cli("check", "one.py", "two.py", cwd=tmp_path)
    assert result.returncode == 1
    flagged = {line.split(":", 1)[0] for line in error_lines(result.stdout)}
    assert flagged == {"one.py", "two.py"}


def test_check_dedupes_repeated_paths(run_cli, tmp_path):
    # Overlapping/duplicate args (e.g. pre-commit listing a file twice) lint once.
    noqa_plugin(tmp_path)
    (tmp_path / "one.py").write_text("a\n")
    result = run_cli("check", "one.py", "one.py", cwd=tmp_path)
    # The three rules fire once each on the single `a` — not doubled.
    assert len(error_lines(result.stdout)) == 3


def test_extend_select_appends_to_config(run_cli, tmp_path):
    three_rule_plugin(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.nib]\nplugins = ["multirule"]\nselect = ["AAA001"]\n'
    )
    result = run_cli("check", "x.py", "--extend-select", "BBB001", cwd=tmp_path)
    assert reported_codes(result.stdout) == {"AAA001", "BBB001"}


def test_bare_noqa_blankets_its_line_and_does_not_leak(run_cli, tmp_path):
    # Line 1 has bare `# noqa` (blanket) — should silence all three rules.
    # Line 2 has no directive — should still fire all three.
    noqa_plugin(tmp_path)
    (tmp_path / "x.py").write_text("a  # noqa\na\n")
    result = run_cli("check", "x.py", cwd=tmp_path)
    assert result.returncode == 1  # line 2's unsuppressed diags
    assert all(":2:" in line for line in error_lines(result.stdout))
    assert reported_codes(result.stdout) == {"AAA001", "AAA002", "BBB001"}


def test_noqa_with_code_list_only_suppresses_listed(run_cli, tmp_path):
    noqa_plugin(tmp_path)
    (tmp_path / "x.py").write_text("a  # noqa: AAA001, BBB001\n")
    result = run_cli("check", "x.py", cwd=tmp_path)
    assert reported_codes(result.stdout) == {"AAA002"}


def test_noqa_keyword_is_case_insensitive(run_cli, tmp_path):
    # The `noqa` keyword matches case-insensitively; rule codes themselves
    # are matched literally.
    noqa_plugin(tmp_path)
    (tmp_path / "x.py").write_text("a  # NoQA: AAA001\n")
    result = run_cli("check", "x.py", cwd=tmp_path)
    assert reported_codes(result.stdout) == {"AAA002", "BBB001"}


def test_noqa_inside_string_is_a_known_false_positive(run_cli, tmp_path):
    # Known limitation of the regex-based scanner: it cannot tell a real
    # `# noqa` comment from the text "# noqa" sitting inside a string
    # literal. We accept this to skip a full tokenize pass on every file.
    # Real-world cases are probably rare.
    noqa_plugin(tmp_path)
    (tmp_path / "x.py").write_text('a = "# noqa"\na\n')
    result = run_cli("check", "x.py", cwd=tmp_path)
    # Line 1's diags get suppressed by the in-string false positive; line 2
    # still fires all three rules.
    assert all(":2:" in line for line in error_lines(result.stdout))
    assert reported_codes(result.stdout) == {"AAA001", "AAA002", "BBB001"}


def test_noqa_with_empty_colon_suppresses_nothing(run_cli, tmp_path):
    # `# noqa:` with no codes is considered malformed - not the blanket form.
    # Only bare `# noqa` (no colon) blankets the line.
    noqa_plugin(tmp_path)
    (tmp_path / "x.py").write_text("a  # noqa:\n")
    result = run_cli("check", "x.py", cwd=tmp_path)
    assert reported_codes(result.stdout) == {"AAA001", "AAA002", "BBB001"}


def test_rule_may_resolve_import_forms(run_cli, tmp_path):
    # DEMO010 (NoPickleLoads) uses `self.resolve`, so it should flag pickle's
    # unsafe loaders through every import form, and leave `pickle.dumps` alone.
    sample = tmp_path / "uses_pickle.py"
    sample.write_text(
        "import pickle\n"
        "import pickle as p\n"
        "from pickle import loads\n"
        "\n"
        "pickle.loads(b'')\n"  # line 5
        "p.loads(b'')\n"  # line 6  (aliased module)
        "loads(b'')\n"  # line 7  (from-import)
        "pickle.load(f)\n"  # line 8
        "pickle.dumps(x)\n"  # line 9  (safe - must not fire)
    )
    result = run_cli(
        "check", str(sample), "--plugins", "demo.rules", "--select", "DEMO010"
    )
    assert result.returncode == 1
    assert reported_codes(result.stdout) == {"DEMO010"}
    linenos = sorted(int(line.split(":")[1]) for line in error_lines(result.stdout))
    assert linenos == [5, 6, 7, 8]


def _setup_project(tmp_path, *, imported: bool):
    """A package whose plugin.py defines a `setup()` entry point; main.py imports
    that function by name only when `imported`."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "__init__.py").write_text("")
    (proj / "plugin.py").write_text("def setup():\n    pass\n")
    body = "from proj.plugin import setup\n" if imported else "pass\n"
    (proj / "main.py").write_text(f"def run():\n    {body}")
    return proj


def test_cross_file_rule_flags_unimported_setup(run_cli, tmp_path):
    # DEMO011 fires when `proj.plugin.setup` is imported by name nowhere.
    proj = _setup_project(tmp_path, imported=False)
    result = run_cli(
        "check", str(proj), "--plugins", "demo.rules", "--select", "DEMO011"
    )
    assert result.returncode == 1
    assert reported_codes(result.stdout) == {"DEMO011"}
    # The finding is on plugin.py (where setup is defined), not main.py.
    assert all("plugin.py" in line for line in error_lines(result.stdout))
    # A cross-file run is cacheable now (no decorator, no cache opt-out).
    assert list(Path(os.environ["NIB_CACHE_DIR"]).rglob("cache.*.pickle")) != []


def test_cross_file_rule_silent_when_setup_imported(run_cli, tmp_path):
    proj = _setup_project(tmp_path, imported=True)
    result = run_cli(
        "check", str(proj), "--plugins", "demo.rules", "--select", "DEMO011"
    )
    assert result.returncode == 0
    assert reported_codes(result.stdout) == set()


def test_cross_file_verdict_reresolved_over_warm_cache(run_cli, tmp_path):
    # The unimported setup fires, then a *second* file adds the import without
    # touching the function's file. On the warm cache plugin.py is a hit, yet its
    # verdict must now reverse to no finding; deferred findings are
    # re-resolved every run from the cached import targets, not replayed stale.
    proj = _setup_project(tmp_path, imported=False)
    cmd = ("check", str(proj), "--plugins", "demo.rules", "--select", "DEMO011")

    first = run_cli(*cmd)
    assert first.returncode == 1
    assert reported_codes(first.stdout) == {"DEMO011"}

    # main.py now imports setup; plugin.py is unchanged (warm hit).
    (proj / "main.py").write_text("def run():\n    from proj.plugin import setup\n")
    second = run_cli(*cmd)
    assert second.returncode == 0
    assert reported_codes(second.stdout) == set()


def test_cross_file_verdict_sees_imports_outside_invocation_cold(run_cli, tmp_path):
    # Linting only plugin.py — with no prior wide run to warm any cache — still
    # resolves DEMO011 against the whole package: nib scans the enclosing tree for
    # reachability, sees main.py's import of setup, and stays silent. (Without the
    # project scan this would falsely fire, since main.py isn't in the file set.)
    proj = _setup_project(tmp_path, imported=True)  # main.py imports proj.plugin.setup
    result = run_cli(
        "check",
        str(proj / "plugin.py"),
        "--plugins",
        "demo.rules",
        "--select",
        "DEMO011",
        "--no-cache",
    )
    assert reported_codes(result.stdout) == set()
    assert result.returncode == 0


def test_cross_file_verdict_fires_when_truly_unimported_narrow(run_cli, tmp_path):
    # Control for the scan above: when no project file imports setup, a narrow lint
    # of plugin.py must still flag it.
    proj = _setup_project(tmp_path, imported=False)  # main.py imports nothing
    result = run_cli(
        "check",
        str(proj / "plugin.py"),
        "--plugins",
        "demo.rules",
        "--select",
        "DEMO011",
        "--no-cache",
    )
    assert reported_codes(result.stdout) == {"DEMO011"}
    assert result.returncode == 1
