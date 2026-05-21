use crate::parser::{Module, NodeRef, wrap_node};
use pyo3::prelude::*;
use std::collections::HashMap;

/// Map a `visit_<AstName>` method to the tree-sitter kinds it should fire on.
/// Multiple kinds map to the same AST class (a `Constant` covers literals of
/// several types).
fn kinds_for_visit(ast_name: &str) -> &'static [&'static str] {
    match ast_name {
        "Module" => &["module"],
        "Call" => &["call"],
        "Name" => &["identifier"],
        "Attribute" => &["attribute"],
        "Constant" => &["integer", "float", "string", "true", "false", "none"],
        _ => &[],
    }
}

/// Walk a parsed `Module` once, dispatching to each rule's `visit_*` methods.
/// Returns the flat list of items yielded by all rules (raw Python objects for
/// now; will become typed `Diagnostic`s later on).
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
/// methods so the hot walk loop is a single HashMap lookup per node.
fn build_dispatch(
    py: Python,
    rules: &[Py<PyAny>],
) -> PyResult<HashMap<&'static str, Vec<Py<PyAny>>>> {
    let mut dispatch: HashMap<&'static str, Vec<Py<PyAny>>> = HashMap::new();
    for rule in rules {
        let bound = rule.bind(py);
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
                dispatch.entry(kind).or_default().push(method.clone_ref(py));
            }
        }
    }
    Ok(dispatch)
}

/// Iterative depth-first pre-order walk; fires matched rules on each node.
fn walk_and_collect(
    py: Python,
    root_ref: &NodeRef,
    dispatch: &HashMap<&'static str, Vec<Py<PyAny>>>,
) -> PyResult<Vec<Py<PyAny>>> {
    let mut results = Vec::new();
    let root = root_ref.node();
    // The cursor is tree-sitter's stateful walker — avoids allocating during
    // traversal. We drive it manually instead of using an iterator.
    let mut cursor = root.walk();

    'outer: loop {
        // 1. Visit the current node: dispatch matching rules, collect yields.
        let node = cursor.node();
        if let Some(methods) = dispatch.get(node.kind()) {
            let n_ref = NodeRef::from_node(root_ref.tree.clone(), root_ref.source.clone(), node);
            if let Some(wrapped) = wrap_node(py, &n_ref)? {
                fire_methods(py, methods, &wrapped, &mut results)?;
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

/// Call every matched visit_* method on the wrapped node, draining any yielded
/// items into `out`. A visit_* without `yield` returns None — skip it.
fn fire_methods(
    py: Python,
    methods: &[Py<PyAny>],
    wrapped: &Py<PyAny>,
    out: &mut Vec<Py<PyAny>>,
) -> PyResult<()> {
    for method in methods {
        let ret = method.bind(py).call1((wrapped.clone_ref(py),))?;
        if ret.is_none() {
            continue;
        }
        for item in ret.try_iter()? {
            out.push(item?.unbind());
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
        assert!(kinds_for_visit("Subscript").is_empty());
        assert!(kinds_for_visit("").is_empty());
        assert!(kinds_for_visit("call").is_empty()); // case-sensitive
    }
}
