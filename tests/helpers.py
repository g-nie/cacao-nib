"""Shared test helpers for the CLI/cache test modules: plugin scaffolding and
output parsing, imported where needed."""


def three_rule_plugin(tmp_path):
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


def error_lines(stdout: str) -> list[str]:
    """Diagnostic lines only — drops the trailing `Found N issues.` summary."""
    return [line for line in stdout.splitlines() if "error[" in line]


def reported_codes(stdout: str) -> set[str]:
    return {line.split("error[")[1].split("]")[0] for line in error_lines(stdout)}


def noqa_plugin(tmp_path):
    three_rule_plugin(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[tool.nib]\nplugins = ["multirule"]\n')
