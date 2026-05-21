use pyo3::prelude::*;
use pyo3::types::PyModule as PyModuleType;
use std::sync::Arc;
use tree_sitter::{Node, Parser, Tree};

// A handle to a tree-sitter node, by byte range. Resolves to the live `Node`
// on each access by walking from the tree root. Both Arcs keep the underlying
// data alive for as long as any handle exists.
#[derive(Clone)]
struct NodeRef {
    tree: Arc<Tree>,
    source: Arc<[u8]>,
    start: usize,
    end: usize,
}

impl NodeRef {
    fn from_node(tree: Arc<Tree>, source: Arc<[u8]>, node: Node) -> Self {
        Self {
            tree,
            source,
            start: node.start_byte(),
            end: node.end_byte(),
        }
    }

    fn child_with(&self, node: Node) -> Self {
        Self::from_node(self.tree.clone(), self.source.clone(), node)
    }

    fn node(&self) -> Node<'_> {
        self.tree
            .root_node()
            .descendant_for_byte_range(self.start, self.end)
            .expect("node range fell outside the tree")
    }

    fn text(&self) -> &str {
        std::str::from_utf8(&self.source[self.start..self.end])
            .expect("node range is not valid UTF-8 (grammar bug or non-UTF-8 source)")
    }

    fn lineno(&self) -> usize {
        self.node().start_position().row + 1
    }

    fn col_offset(&self) -> usize {
        self.node().start_position().column
    }
}

#[pyclass(module = "nib.ast")]
struct Module {
    inner: NodeRef,
}

#[pymethods]
impl Module {
    #[getter]
    fn body(&self, py: Python) -> PyResult<Vec<Py<PyAny>>> {
        let root = self.inner.node();
        let mut cursor = root.walk();
        let mut out = Vec::new();
        for child in root.named_children(&mut cursor) {
            // Unwrap expression_statement -> its inner expression; skip other stmts for MVP.
            if child.kind() == "expression_statement"
                && let Some(expr) = child.named_child(0)
                && let Some(obj) = wrap_expr_opt(py, &self.inner.child_with(expr))?
            {
                out.push(obj);
            }
        }
        Ok(out)
    }
}

#[pyclass(module = "nib.ast")]
struct Call {
    inner: NodeRef,
}

#[pymethods]
impl Call {
    #[getter]
    fn func(&self, py: Python) -> PyResult<Py<PyAny>> {
        let node = self.inner.node();
        let func_node = node
            .child_by_field_name("function")
            .ok_or_else(|| pyo3::exceptions::PyAttributeError::new_err("call has no function"))?;
        wrap_expr(py, &self.inner.child_with(func_node))
    }

    #[getter]
    fn args(&self, py: Python) -> PyResult<Vec<Py<PyAny>>> {
        let node = self.inner.node();
        let Some(args_node) = node.child_by_field_name("arguments") else {
            return Ok(vec![]);
        };
        let mut cursor = args_node.walk();
        let mut out = Vec::new();
        for child in args_node.named_children(&mut cursor) {
            if child.kind() == "keyword_argument" {
                continue;
            }
            if let Some(obj) = wrap_expr_opt(py, &self.inner.child_with(child))? {
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
struct Name {
    inner: NodeRef,
}

#[pymethods]
impl Name {
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
struct Attribute {
    inner: NodeRef,
}

#[pymethods]
impl Attribute {
    #[getter]
    fn value(&self, py: Python) -> PyResult<Py<PyAny>> {
        let node = self.inner.node();
        let obj_node = node
            .child_by_field_name("object")
            .ok_or_else(|| pyo3::exceptions::PyAttributeError::new_err("attribute has no object"))?;
        wrap_expr(py, &self.inner.child_with(obj_node))
    }

    #[getter]
    fn attr(&self) -> PyResult<String> {
        let node = self.inner.node();
        let attr_node = node
            .child_by_field_name("attribute")
            .ok_or_else(|| pyo3::exceptions::PyAttributeError::new_err("attribute has no name"))?;
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
struct Constant {
    inner: NodeRef,
}

#[pymethods]
impl Constant {
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
                let stripped = txt
                    .trim_start_matches(['r', 'R', 'b', 'B', 'f', 'F', 'u', 'U'])
                    .trim_matches(['"', '\'']);
                Ok(stripped.into_pyobject(py)?.into_any().unbind())
            }
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

fn wrap_expr(py: Python, n: &NodeRef) -> PyResult<Py<PyAny>> {
    wrap_expr_opt(py, n)?.ok_or_else(|| {
        pyo3::exceptions::PyRuntimeError::new_err(format!(
            "unsupported node kind: {}",
            n.node().kind()
        ))
    })
}

fn wrap_expr_opt(py: Python, n: &NodeRef) -> PyResult<Option<Py<PyAny>>> {
    let obj: Py<PyAny> = match n.node().kind() {
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

#[pyfunction]
fn parse_module(source: String) -> PyResult<Module> {
    let mut parser = Parser::new();
    parser
        .set_language(&tree_sitter_python::LANGUAGE.into())
        .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
    let tree = parser
        .parse(&source, None)
        .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("parse failed"))?;
    let source_bytes: Arc<[u8]> = Arc::from(source.into_bytes().into_boxed_slice());
    let tree = Arc::new(tree);
    let root = tree.root_node();
    let inner = NodeRef::from_node(tree.clone(), source_bytes, root);
    Ok(Module { inner })
}

#[pymodule]
fn nib(m: &Bound<'_, PyModuleType>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_module, m)?)?;
    let py = m.py();
    let ast = PyModuleType::new(py, "ast")?;
    ast.add_class::<Module>()?;
    ast.add_class::<Call>()?;
    ast.add_class::<Name>()?;
    ast.add_class::<Attribute>()?;
    ast.add_class::<Constant>()?;
    m.add_submodule(&ast)?;
    Ok(())
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
        // module > expression_statement > call > function:identifier "eval"
        let call = n.node().named_child(0).unwrap().named_child(0).unwrap();
        let func = call.child_by_field_name("function").unwrap();
        let func_ref = n.child_with(func);
        assert_eq!(func_ref.node().kind(), "identifier");
        assert_eq!(func_ref.text(), "eval");
    }

    #[test]
    fn lineno_advances_with_source() {
        let n = parse("a\nb\nc\n");
        // third statement starts on line 3
        let third = n.node().named_child(2).unwrap();
        let third_ref = n.child_with(third);
        assert_eq!(third_ref.lineno(), 3);
        assert_eq!(third_ref.text(), "c");
    }
}
