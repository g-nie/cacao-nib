use pyo3::prelude::*;
use std::sync::Arc;
use tree_sitter::{Node, Parser, Tree};

// A handle to a tree-sitter node, by byte range. Resolves to the live `Node`
// on each access by walking from the tree root. Both Arcs keep the underlying
// data alive for as long as any handle exists.
#[derive(Clone)]
pub(crate) struct NodeRef {
    pub(crate) tree: Arc<Tree>,
    pub(crate) source: Arc<[u8]>,
    pub(crate) start: usize,
    pub(crate) end: usize,
}

impl NodeRef {
    /// Build a handle from a live tree-sitter `Node`. Only its byte range is
    /// kept — the `Node` itself goes out of scope here.
    pub(crate) fn from_node(tree: Arc<Tree>, source: Arc<[u8]>, node: Node) -> Self {
        Self {
            tree,
            source,
            start: node.start_byte(),
            end: node.end_byte(),
        }
    }

    /// Make a sibling handle pointing at a different node in the same tree.
    /// Bumps both Arc refcounts; the new handle shares the underlying buffers.
    fn child_with(&self, node: Node) -> Self {
        Self::from_node(self.tree.clone(), self.source.clone(), node)
    }

    /// Resolve the live `Node` for our stored range by walking from the root.
    /// O(depth). The returned borrow is tied to `&self`.
    pub(crate) fn node(&self) -> Node<'_> {
        self.tree
            .root_node()
            .descendant_for_byte_range(self.start, self.end)
            .expect("node range fell outside the tree")
    }

    /// The raw source text spanned by this node. Borrows from `self.source`.
    fn text(&self) -> &str {
        std::str::from_utf8(&self.source[self.start..self.end])
            .expect("node range is not valid UTF-8 (grammar bug or non-UTF-8 source)")
    }

    /// 1-based line number, matching CPython's `ast.lineno`.
    fn lineno(&self) -> usize {
        self.node().start_position().row + 1
    }

    /// 0-based column offset, matching CPython's `ast.col_offset`.
    fn col_offset(&self) -> usize {
        self.node().start_position().column
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct Module {
    pub(crate) inner: NodeRef,
}

#[pymethods]
impl Module {
    /// The module's top-level statements. For MVP, only expression statements
    /// are unwrapped to their inner expression — imports, defs, etc. are
    /// silently skipped until we add wrappers for them.
    #[getter]
    fn body(&self, py: Python) -> PyResult<Vec<Py<PyAny>>> {
        let root = self.inner.node();
        // Cursor is required by tree-sitter's iteration API — it's a reusable
        // walker rather than per-call allocations.
        let mut cursor = root.walk();
        let mut out = Vec::new();
        for child in root.named_children(&mut cursor) {
            // Tree-sitter wraps bare expressions in an `expression_statement`
            // node. CPython's `ast` skips that layer, so we do too.
            if child.kind() == "expression_statement"
                && let Some(expr) = child.named_child(0)
                && let Some(obj) = wrap_node(py, &self.inner.child_with(expr))?
            {
                out.push(obj);
            }
        }
        Ok(out)
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct Call {
    inner: NodeRef,
}

#[pymethods]
impl Call {
    /// The callee. Always present on a well-formed call. Strict wrapper —
    /// errors if the callee node kind isn't one we know how to wrap.
    #[getter]
    fn func(&self, py: Python) -> PyResult<Py<PyAny>> {
        let node = self.inner.node();
        let func_node = node
            .child_by_field_name("function")
            .ok_or_else(|| pyo3::exceptions::PyAttributeError::new_err("call has no function"))?;
        wrap_node_or_err(py, &self.inner.child_with(func_node))
    }

    /// Positional arguments. Keyword arguments (`x=1`) are filtered out — for
    /// MVP we only expose the positional list; kwargs will be a separate
    /// `keywords` getter later, mirroring CPython.
    #[getter]
    fn args(&self, py: Python) -> PyResult<Vec<Py<PyAny>>> {
        let node = self.inner.node();
        // A bare `foo()` call has no `arguments` field at all.
        let Some(args_node) = node.child_by_field_name("arguments") else {
            return Ok(vec![]);
        };
        let mut cursor = args_node.walk();
        let mut out = Vec::new();
        for child in args_node.named_children(&mut cursor) {
            if child.kind() == "keyword_argument" {
                continue;
            }
            // Lenient: skip arg kinds we don't wrap yet (e.g. binary_operator).
            if let Some(obj) = wrap_node(py, &self.inner.child_with(child))? {
                out.push(obj);
            }
        }
        Ok(out)
    }

    #[getter]
    fn lineno(&self) -> usize {
        self.inner.lineno()
    }

    #[getter]
    fn col_offset(&self) -> usize {
        self.inner.col_offset()
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct Name {
    inner: NodeRef,
}

#[pymethods]
impl Name {
    /// The identifier text (e.g., "eval" for the name in `eval(x)`).
    /// Owned String because we cross the FFI boundary — the borrow into
    /// `self.inner.source` can't survive past this call.
    #[getter]
    fn id(&self) -> String {
        self.inner.text().to_string()
    }

    #[getter]
    fn lineno(&self) -> usize {
        self.inner.lineno()
    }

    #[getter]
    fn col_offset(&self) -> usize {
        self.inner.col_offset()
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct Attribute {
    inner: NodeRef,
}

#[pymethods]
impl Attribute {
    /// The left side of the dot — `os.path` for `os.path.join`. Can be a
    /// Name, another Attribute, a Call, etc.
    #[getter]
    fn value(&self, py: Python) -> PyResult<Py<PyAny>> {
        let node = self.inner.node();
        let obj_node = node
            .child_by_field_name("object")
            .ok_or_else(|| pyo3::exceptions::PyAttributeError::new_err("attribute has no object"))?;
        wrap_node_or_err(py, &self.inner.child_with(obj_node))
    }

    /// The right side of the dot — "join" for `os.path.join`. Returned as a
    /// bare string, matching CPython's `ast.Attribute.attr` (not a Name node).
    #[getter]
    fn attr(&self) -> PyResult<String> {
        let node = self.inner.node();
        let attr_node = node
            .child_by_field_name("attribute")
            .ok_or_else(|| pyo3::exceptions::PyAttributeError::new_err("attribute has no name"))?;
        // Slice the source for the attribute identifier specifically — we
        // want only its range, not the whole Attribute node's range.
        let s = std::str::from_utf8(&self.inner.source[attr_node.byte_range()])
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        Ok(s.to_string())
    }

    #[getter]
    fn lineno(&self) -> usize {
        self.inner.lineno()
    }

    #[getter]
    fn col_offset(&self) -> usize {
        self.inner.col_offset()
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct Constant {
    inner: NodeRef,
}

#[pymethods]
impl Constant {
    /// The actual Python value of the literal — `int`, `float`, `str`, `bool`,
    /// or `None`. Matches `ast.Constant.value` in CPython, so rules can write
    /// `node.value == 42` or `node.value is None` directly.
    #[getter]
    fn value(&self, py: Python) -> PyResult<Py<PyAny>> {
        let txt = self.inner.text();
        match self.inner.node().kind() {
            "integer" => {
                let v: i64 = txt.parse().map_err(|e: std::num::ParseIntError| {
                    pyo3::exceptions::PyValueError::new_err(e.to_string())
                })?;
                Ok(v.into_pyobject(py)?.into_any().unbind())
            }
            "float" => {
                let v: f64 = txt.parse().map_err(|e: std::num::ParseFloatError| {
                    pyo3::exceptions::PyValueError::new_err(e.to_string())
                })?;
                Ok(v.into_pyobject(py)?.into_any().unbind())
            }
            "string" => {
                // Naive string handling: strip any leading prefix (`r`, `b`,
                // `f`, `u`, case-insensitive) then trim quote chars. Wrong for
                // triple-quoted strings and escapes — good enough for MVP.
                let stripped = txt
                    .trim_start_matches(['r', 'R', 'b', 'B', 'f', 'F', 'u', 'U'])
                    .trim_matches(['"', '\'']);
                Ok(stripped.into_pyobject(py)?.into_any().unbind())
            }
            // True/False are singletons in Python (only one of each ever
            // exists). `into_pyobject` returns a Borrowed; to_owned bumps the
            // refcount so we get an owned handle.
            "true" => Ok(true.into_pyobject(py)?.to_owned().into_any().unbind()),
            "false" => Ok(false.into_pyobject(py)?.to_owned().into_any().unbind()),
            "none" => Ok(py.None()),
            other => Err(pyo3::exceptions::PyRuntimeError::new_err(format!(
                "unsupported constant kind: {other}"
            ))),
        }
    }

    #[getter]
    fn lineno(&self) -> usize {
        self.inner.lineno()
    }

    #[getter]
    fn col_offset(&self) -> usize {
        self.inner.col_offset()
    }
}

/// Wrap a tree-sitter node as the matching Python AST class. `None` for kinds
/// we don't have a wrapper for (binary ops, comprehensions, comments, ...).
pub(crate) fn wrap_node(py: Python, n: &NodeRef) -> PyResult<Option<Py<PyAny>>> {
    let obj: Py<PyAny> = match n.node().kind() {
        "module" => Py::new(py, Module { inner: n.clone() })?.into_any(),
        "call" => Py::new(py, Call { inner: n.clone() })?.into_any(),
        "identifier" => Py::new(py, Name { inner: n.clone() })?.into_any(),
        "attribute" => Py::new(py, Attribute { inner: n.clone() })?.into_any(),
        "integer" | "float" | "string" | "true" | "false" | "none" => {
            Py::new(py, Constant { inner: n.clone() })?.into_any()
        }
        _ => return Ok(None),
    };
    Ok(Some(obj))
}

/// Strict variant of `wrap_node`: errors if the node isn't wrappable.
/// Use in positions where a missing wrapper is a bug (e.g., `Call.func`).
fn wrap_node_or_err(py: Python, n: &NodeRef) -> PyResult<Py<PyAny>> {
    wrap_node(py, n)?.ok_or_else(|| {
        pyo3::exceptions::PyRuntimeError::new_err(format!(
            "unsupported node kind: {}",
            n.node().kind()
        ))
    })
}

/// Entry point exposed to Python: parse a Python source string into a Module.
/// TODO: surface parse errors instead of returning a Module with ERROR nodes
/// (see MVP follow-ups). Currently tree-sitter parses everything it can and
/// silently includes ERROR nodes for what it couldn't.
#[pyfunction]
pub(crate) fn parse_module(source: String) -> PyResult<Module> {
    let mut parser = Parser::new();
    parser
        .set_language(&tree_sitter_python::LANGUAGE.into())
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
    let tree = parser
        .parse(&source, None)
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("parse failed"))?;
    // Move the source into an Arc<[u8]> so every wrapper can share it cheaply.
    let source_bytes: Arc<[u8]> = Arc::from(source.into_bytes().into_boxed_slice());
    let tree = Arc::new(tree);
    let root = tree.root_node();
    // Clone the Arc for the NodeRef; we'd want to keep `tree` available if we
    // needed it again, but here we just hand the clone over and drop the local.
    let inner = NodeRef::from_node(tree.clone(), source_bytes, root);
    Ok(Module { inner })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(source: &str) -> NodeRef {
        let mut parser = Parser::new();
        parser
            .set_language(&tree_sitter_python::LANGUAGE.into())
            .unwrap();
        let tree = parser.parse(source, None).unwrap();
        let source_bytes: Arc<[u8]> = Arc::from(source.as_bytes().to_vec().into_boxed_slice());
        let tree = Arc::new(tree);
        let root = tree.root_node();
        NodeRef::from_node(tree.clone(), source_bytes, root)
    }

    #[test]
    fn root_node_text_and_position() {
        let n = parse("x = 1\n");
        assert_eq!(n.text(), "x = 1\n");
        assert_eq!(n.lineno(), 1);
        assert_eq!(n.col_offset(), 0);
        assert_eq!(n.node().kind(), "module");
    }

    #[test]
    fn descend_to_identifier() {
        let n = parse("eval('hi')\n");
        let call = n.node().named_child(0).unwrap().named_child(0).unwrap();
        let func = call.child_by_field_name("function").unwrap();
        let func_ref = n.child_with(func);
        assert_eq!(func_ref.node().kind(), "identifier");
        assert_eq!(func_ref.text(), "eval");
    }

    #[test]
    fn lineno_advances_with_source() {
        let n = parse("a\nb\nc\n");
        let third = n.node().named_child(2).unwrap();
        let third_ref = n.child_with(third);
        assert_eq!(third_ref.lineno(), 3);
        assert_eq!(third_ref.text(), "c");
    }
}
