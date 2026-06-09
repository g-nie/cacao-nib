from helpers import reported_codes, three_rule_plugin

# three_rule_plugin's rules all fire on a bare name `a`.
DIRTY = "a\n"
PLUGIN_CODES = {"AAA001", "AAA002", "BBB001"}


def _project(root):
    """Scaffold a project root: the multirule plugin + a `[tool.nib]` table
    pointing at it."""
    three_rule_plugin(root)  # writes multirule.py (and an x.py we ignore here)
    (root / "pyproject.toml").write_text('[tool.nib]\nplugins = ["multirule"]\n')


def test_subdir_uses_ancestor_config(run_cli, tmp_path):
    # Running from a subdirectory with no pyproject of its own must still load
    # the project's plugins from the ancestor root (and import them from there).
    _project(tmp_path)
    sub = tmp_path / "src" / "pkg"
    sub.mkdir(parents=True)
    (sub / "code.py").write_text(DIRTY)

    result = run_cli("check", ".", cwd=sub)
    assert result.returncode == 1
    assert reported_codes(result.stdout) == PLUGIN_CODES


def test_ancestor_without_nib_table_is_skipped(run_cli, tmp_path):
    # A nearer pyproject.toml lacking [tool.nib] must not shadow the real config:
    # discovery skips it and keeps walking up.
    _project(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "pyproject.toml").write_text('[project]\nname = "x"\n')
    (sub / "code.py").write_text(DIRTY)

    result = run_cli("check", ".", cwd=sub)
    assert result.returncode == 1
    assert reported_codes(result.stdout) == PLUGIN_CODES


def test_unreadable_ancestor_pyproject_is_skipped(run_cli, tmp_path):
    # A malformed pyproject.toml is skipped like one without the table, rather
    # than aborting the run.
    _project(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "pyproject.toml").write_text("not = valid toml [[[")
    (sub / "code.py").write_text(DIRTY)

    result = run_cli("check", ".", cwd=sub)
    assert result.returncode == 1
    assert reported_codes(result.stdout) == PLUGIN_CODES


def test_no_config_anywhere_loads_no_rules(run_cli, tmp_path):
    # With no [tool.nib] up the tree, nib has no plugins/rules: every file is
    # "clean" and the run exits 0 without diagnostics.
    (tmp_path / "code.py").write_text(DIRTY)

    result = run_cli("check", ".", cwd=tmp_path)
    assert result.returncode == 0
    assert reported_codes(result.stdout) == set()
