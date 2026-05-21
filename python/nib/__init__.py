from .nib import Diagnostic, ast, parse_module, run


class Rule:
    """Subclass and define `visit_<AstName>` methods that yield diagnostics."""


__all__ = ["Diagnostic", "Rule", "ast", "parse_module", "run"]
