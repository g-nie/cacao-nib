use crate::parser::{Module, NodeRef, wrap_node};
use pyo3::prelude::*;
use std::collections::HashMap;

/// A lint finding emitted by a rule.
///
/// `Diagnostic(node, message)` pulls the span from `node`'s `lineno`,
/// `col_offset`, `end_lineno`, `end_col_offset` attributes. The `code` is
/// filled in by the dispatcher after the rule returns it, from the owning rule's `.code`
/// class attribute.
#[pyclass(module = "nib")]
pub(crate) struct Diagnostic {
    #[pyo3(get, set)]
    code: String,
    #[pyo3(get, set)]
    message: String,
    #[pyo3(get)]
    lineno: usize,
    #[pyo3(get)]
    col_offset: usize,
    #[pyo3(get)]
    end_lineno: usize,
    #[pyo3(get)]
    end_col_offset: usize,
}

#[pymethods]
impl Diagnostic {
    #[new]
    fn new(node: &Bound<'_, PyAny>, message: String) -> PyResult<Self> {
        Ok(Self {
            code: String::new(),
            message,
            lineno: node.getattr("lineno")?.extract()?,
            col_offset: node.getattr("col_offset")?.extract()?,
            end_lineno: node.getattr("end_lineno")?.extract()?,
            end_col_offset: node.getattr("end_col_offset")?.extract()?,
        })
    }

    fn __repr__(&self) -> String {
        format!(
            "Diagnostic(code={:?}, message={:?}, line={}, col={})",
            self.code, self.message, self.lineno, self.col_offset
        )
    }
}

/// One bound `visit_*` method together with the `code` attribute of its rule.
struct DispatchEntry {
    method: Py<PyAny>,
    code: String,
}

/// Map a `visit_<AstName>` method to the tree-sitter kinds it should fire on.
/// Multiple kinds map to the same AST class (a `Constant` covers literals of
/// several types).
fn kinds_for_visit(ast_name: &str) -> &'static [&'static str] {
    match ast_name {
        "Module" => &["module"],
        "Call" => &["call"],
        "Name" => &["identifier"],
        "Attribute" => &["attribute"],
        "Subscript" => &["subscript"],
        "IfExp" => &["conditional_expression"],
        "BoolOp" => &["boolean_operator"],
        "Lambda" => &["lambda"],
        "BinOp" => &["binary_operator"],
        "List" => &["list"],
        "Dict" => &["dictionary"],
        "Constant" => &["integer", "float", "string", "true", "false", "none"],
        _ => &[],
    }
}

/// Walk a parsed `Module` once, dispatching to each rule's `visit_*` methods.
/// Returns the flat list of items returned by all rules. `Diagnostic` items
/// have their `code` filled in from the owning rule's `code` attribute.
#[pyfunction]
pub(crate) fn run(
    py: Python,
    module: PyRef<Module>,
    rules: Vec<Py<PyAny>>,
) -> PyResult<Vec<Py<PyAny>>> {
    let dispatch = build_dispatch(py, &rules)?;
    if dispatch.is_empty() {
        return Ok(Vec::new());
    }
    walk_and_collect(py, &module.inner, &dispatch)
}

/// Introspect each rule once up-front. Maps tree-sitter kind -> bound visit_*
/// methods so the hot walk loop is a single HashMap lookup per node. Records
/// each rule's `code` attribute alongside its methods so the dispatcher can
/// tag returned `Diagnostic`s.
fn build_dispatch(
    py: Python,
    rules: &[Py<PyAny>],
) -> PyResult<HashMap<&'static str, Vec<DispatchEntry>>> {
    let mut dispatch: HashMap<&'static str, Vec<DispatchEntry>> = HashMap::new();
    for rule in rules {
        let bound = rule.bind(py);
        // Rules without a `code` attribute get an empty code; non-Diagnostic
        // returns aren't tagged anyway.
        let code: String = bound
            .getattr("code")
            .ok()
            .and_then(|c| c.extract().ok())
            .unwrap_or_default();
        for attr in bound.dir()?.iter() {
            let name: String = attr.extract()?;
            // Skip anything that isn't a visit_* method.
            let Some(ast_name) = name.strip_prefix("visit_") else {
                continue;
            };
            // Skip visit_* methods for AST classes we don't know — keeps the
            // dispatcher tolerant of typos and forward-compat with later rules.
            let kinds = kinds_for_visit(ast_name);
            if kinds.is_empty() {
                continue;
            }
            // Register the same bound method under every tree-sitter kind it
            // covers (visit_Constant -> integer, float, string, ...).
            let method = bound.getattr(&name)?.unbind();
            for kind in kinds {
                dispatch.entry(kind).or_default().push(DispatchEntry {
                    method: method.clone_ref(py),
                    code: code.clone(),
                });
            }
        }
    }
    Ok(dispatch)
}

/// Iterative depth-first pre-order walk; fires matched rules on each node.
fn walk_and_collect(
    py: Python,
    root_ref: &NodeRef,
    dispatch: &HashMap<&'static str, Vec<DispatchEntry>>,
) -> PyResult<Vec<Py<PyAny>>> {
    let mut results = Vec::new();
    let root = root_ref.node();
    // The cursor is tree-sitter's stateful walker — avoids allocating during
    // traversal. We drive it manually instead of using an iterator.
    let mut cursor = root.walk();

    'outer: loop {
        // 1. Visit the current node: dispatch matching rules, collect returns.
        let node = cursor.node();
        if let Some(entries) = dispatch.get(node.kind()) {
            let n_ref = NodeRef::from_node(root_ref.tree.clone(), root_ref.source.clone(), node);
            if let Some(wrapped) = wrap_node(py, &n_ref)? {
                fire_methods(py, entries, &wrapped, &mut results)?;
            }
        }

        // 2. Move to the next node in pre-order.
        if cursor.goto_first_child() {
            continue;
        }
        // Leaf node: walk back up until we find a sibling we haven't visited,
        // or we've returned past the root (walk is done).
        loop {
            if cursor.goto_next_sibling() {
                continue 'outer;
            }
            if !cursor.goto_parent() {
                break 'outer;
            }
        }
    }

    Ok(results)
}

/// Call every matched visit_* method on the wrapped node and collect results.
///
/// A visit_* method should return either:
/// - `None` (or fall off the end) — no diagnostics
/// - an iterable of items — typically `list[Diagnostic]`
///
/// `Diagnostic` items get their `code` filled in from the rule's `code`
/// attribute. Other item types pass through untouched.
fn fire_methods(
    py: Python,
    entries: &[DispatchEntry],
    wrapped: &Py<PyAny>,
    out: &mut Vec<Py<PyAny>>,
) -> PyResult<()> {
    for entry in entries {
        let ret = entry.method.bind(py).call1((wrapped.clone_ref(py),))?;
        // A visit_* that returns None means "no diagnostics" — skip rather
        // than fail in try_iter (None isn't iterable).
        if ret.is_none() {
            continue;
        }
        for item in ret.try_iter()? {
            let item = item?;
            // Tag Diagnostic instances with the rule's code. Cheap cast;
            // non-Diagnostic items (e.g. raw values from tests) pass through.
            if let Ok(diag) = item.cast::<Diagnostic>() {
                diag.borrow_mut().code = entry.code.clone();
            }
            out.push(item.unbind());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_ast_names_map_to_kinds() {
        assert_eq!(kinds_for_visit("Module"), &["module"]);
        assert_eq!(kinds_for_visit("Call"), &["call"]);
        assert_eq!(kinds_for_visit("Name"), &["identifier"]);
        assert_eq!(kinds_for_visit("Attribute"), &["attribute"]);
    }

    #[test]
    fn constant_covers_all_literal_kinds() {
        let kinds = kinds_for_visit("Constant");
        for expected in ["integer", "float", "string", "true", "false", "none"] {
            assert!(kinds.contains(&expected), "missing kind: {expected}");
        }
    }

    #[test]
    fn unknown_ast_name_returns_empty() {
        assert!(kinds_for_visit("ListComp").is_empty());
        assert!(kinds_for_visit("").is_empty());
        assert!(kinds_for_visit("call").is_empty()); // case-sensitive
    }
}
