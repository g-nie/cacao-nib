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

    /// 1-based line number of the node's end position.
    fn end_lineno(&self) -> usize {
        self.node().end_position().row + 1
    }

    /// 0-based column offset of the node's end position.
    fn end_col_offset(&self) -> usize {
        self.node().end_position().column
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

    #[getter]
    fn lineno(&self) -> usize {
        self.inner.lineno()
    }

    #[getter]
    fn col_offset(&self) -> usize {
        self.inner.col_offset()
    }

    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }

    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
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

    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }

    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
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

    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }

    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
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

    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }

    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
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
                // Delegate to Python's int(txt, 0) — it handles every valid
                // Python int literal: decimal, 0x/0o/0b, underscore separators,
                // and arbitrary precision (Rust's i64 caps at 64 bits).
                let int_type = py.import("builtins")?.getattr("int")?;
                Ok(int_type.call1((txt, 0))?.unbind())
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

    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }

    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct Subscript {
    inner: NodeRef,
}

#[pymethods]
impl Subscript {
    /// The container being indexed — `arr` in `arr[0]`, `d` in `d['k']`.
    #[getter]
    fn value(&self, py: Python) -> PyResult<Py<PyAny>> {
        let node = self.inner.node();
        let value_node = node
            .child_by_field_name("value")
            .ok_or_else(|| pyo3::exceptions::PyAttributeError::new_err("subscript has no value"))?;
        wrap_node_or_err(py, &self.inner.child_with(value_node))
    }

    /// The index expression inside the brackets — matches CPython's
    /// `ast.Subscript.slice`. For `a[1:2]` this would be a `slice` node, which
    /// we don't yet have a wrapper for (returns None via the lenient wrap).
    #[getter]
    fn slice(&self, py: Python) -> PyResult<Option<Py<PyAny>>> {
        let node = self.inner.node();
        // Tree-sitter-python's subscript node uses field name "subscript" for
        // the bracketed index expression(s). For `a[0]` there's a single child.
        let Some(slice_node) = node.child_by_field_name("subscript") else {
            return Ok(None);
        };
        wrap_node(py, &self.inner.child_with(slice_node))
    }

    #[getter]
    fn lineno(&self) -> usize {
        self.inner.lineno()
    }

    #[getter]
    fn col_offset(&self) -> usize {
        self.inner.col_offset()
    }

    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }

    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct IfExp {
    inner: NodeRef,
}

#[pymethods]
impl IfExp {
    #[getter]
    fn lineno(&self) -> usize {
        self.inner.lineno()
    }
    #[getter]
    fn col_offset(&self) -> usize {
        self.inner.col_offset()
    }
    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }
    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct BoolOp {
    inner: NodeRef,
}

#[pymethods]
impl BoolOp {
    /// "and" or "or", matching the keyword used in the source.
    #[getter]
    fn op(&self) -> PyResult<String> {
        let node = self.inner.node();
        let op_node = node.child_by_field_name("operator").ok_or_else(|| {
            pyo3::exceptions::PyAttributeError::new_err("boolean_operator has no operator")
        })?;
        Ok(op_node.kind().to_string())
    }

    /// All operands of the same-op chain, flattened. Tree-sitter parses
    /// `a or b or c` as nested binary BoolOps; CPython's `ast.BoolOp` flattens
    /// these into a single n-ary node, so we match that. Different-op chains
    /// stay nested (`a or b and c` → BoolOp(or, [a, BoolOp(and, [b, c])])).
    #[getter]
    fn values(&self, py: Python) -> PyResult<Vec<Py<PyAny>>> {
        let node = self.inner.node();
        let op = node
            .child_by_field_name("operator")
            .map(|n| n.kind())
            .unwrap_or("");
        let mut out = Vec::new();
        self.collect_chain(py, node, op, &mut out)?;
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
    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }
    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
    }
}

impl BoolOp {
    /// Recurse into `left`/`right` while they're boolean_operators with the
    /// same operator; collect everything else as a single value.
    fn collect_chain(
        &self,
        py: Python,
        node: Node,
        op: &str,
        out: &mut Vec<Py<PyAny>>,
    ) -> PyResult<()> {
        for field in ["left", "right"] {
            let Some(child) = node.child_by_field_name(field) else {
                continue;
            };
            let same_op = child.kind() == "boolean_operator"
                && child
                    .child_by_field_name("operator")
                    .map(|n| n.kind() == op)
                    .unwrap_or(false);
            if same_op {
                self.collect_chain(py, child, op, out)?;
            } else if let Some(wrapped) = wrap_node(py, &self.inner.child_with(child))? {
                out.push(wrapped);
            }
        }
        Ok(())
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct Lambda {
    inner: NodeRef,
}

#[pymethods]
impl Lambda {
    /// Parameter names. Bare params (`x`), default params (`y=1`), and
    /// `*args`/`**kwargs` all contribute their identifier. Returns
    /// `list[Name]` so rules can inspect names or just count via `len()`.
    #[getter]
    fn args(&self, py: Python) -> PyResult<Vec<Py<PyAny>>> {
        let node = self.inner.node();
        let Some(params) = node.child_by_field_name("parameters") else {
            return Ok(vec![]);
        };
        let mut cursor = params.walk();
        let mut out = Vec::new();
        for child in params.named_children(&mut cursor) {
            // Pull the identifier out of each parameter shape. The `_ => None`
            // branch drops syntactic markers like `/` (positional_separator)
            // and `*` (keyword_separator) — they're tokens, not names.
            let id_node = match child.kind() {
                "identifier" => Some(child),
                "default_parameter" | "typed_parameter" | "typed_default_parameter" => {
                    child.child_by_field_name("name")
                }
                "list_splat_pattern" | "dictionary_splat_pattern" => child.named_child(0),
                _ => None,
            };
            if let Some(id) = id_node
                && id.kind() == "identifier"
                && let Some(wrapped) = wrap_node(py, &self.inner.child_with(id))?
            {
                out.push(wrapped);
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
    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }
    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct BinOp {
    inner: NodeRef,
}

#[pymethods]
impl BinOp {
    #[getter]
    fn left(&self, py: Python) -> PyResult<Py<PyAny>> {
        let node = self.inner.node();
        let left_node = node
            .child_by_field_name("left")
            .ok_or_else(|| pyo3::exceptions::PyAttributeError::new_err("binop has no left"))?;
        wrap_node_or_err(py, &self.inner.child_with(left_node))
    }

    #[getter]
    fn right(&self, py: Python) -> PyResult<Py<PyAny>> {
        let node = self.inner.node();
        let right_node = node
            .child_by_field_name("right")
            .ok_or_else(|| pyo3::exceptions::PyAttributeError::new_err("binop has no right"))?;
        wrap_node_or_err(py, &self.inner.child_with(right_node))
    }

    /// Operator as the literal source text — "+", "-", "*", "/", "%", etc.
    /// Simpler than CPython's `ast.Add`/`ast.Sub`/... classes; if a rule needs
    /// CPython-style operator classes later, build them on top of this.
    #[getter]
    fn op(&self) -> PyResult<String> {
        let node = self.inner.node();
        let op_node = node.child_by_field_name("operator").ok_or_else(|| {
            pyo3::exceptions::PyAttributeError::new_err("binop has no operator")
        })?;
        Ok(op_node.kind().to_string())
    }

    #[getter]
    fn lineno(&self) -> usize {
        self.inner.lineno()
    }
    #[getter]
    fn col_offset(&self) -> usize {
        self.inner.col_offset()
    }
    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }
    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct Compare {
    inner: NodeRef,
}

#[pymethods]
impl Compare {
    /// First operand. For `a == b`, this is `a`; for `1 < x < 10`, `1`.
    #[getter]
    fn left(&self, py: Python) -> PyResult<Py<PyAny>> {
        let node = self.inner.node();
        let first = node
            .named_child(0)
            .ok_or_else(|| pyo3::exceptions::PyAttributeError::new_err("compare has no left"))?;
        wrap_node_or_err(py, &self.inner.child_with(first))
    }

    /// Operator strings, in source order. For `a == b`, `["=="]`; for the
    /// chained `1 < x < 10`, `["<", "<"]`. Two-token operators (`is not`,
    /// `not in`) are returned as a single space-joined string.
    #[getter]
    fn ops(&self) -> Vec<String> {
        let (ops, _) = self.split_ops_and_comparators();
        ops
    }

    /// Right-hand operands, one per `ops` entry. For `a == b`, `[b]`; for
    /// `1 < x < 10`, `[x, 10]`.
    #[getter]
    fn comparators(&self, py: Python) -> PyResult<Vec<Py<PyAny>>> {
        let (_, comparator_nodes) = self.split_ops_and_comparators();
        let mut out = Vec::with_capacity(comparator_nodes.len());
        for child in comparator_nodes {
            out.push(wrap_node_or_err(py, &self.inner.child_with(child))?);
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
    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }
    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
    }
}

impl Compare {
    /// Walk every child in source order, splitting operator tokens (anonymous
    /// children like `==`, `is`, `not`) from operand expressions (named
    /// children). Adjacent anonymous tokens are stitched into one op string
    /// to handle `is not` / `not in`. The first named child is `left` and is
    /// excluded from the returned comparators.
    fn split_ops_and_comparators(&self) -> (Vec<String>, Vec<Node<'_>>) {
        let node = self.inner.node();
        let mut ops = Vec::new();
        let mut comparators = Vec::new();
        let mut pending_op = String::new();
        let mut saw_left = false;
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.is_named() {
                if !saw_left {
                    saw_left = true;
                } else {
                    ops.push(std::mem::take(&mut pending_op));
                    comparators.push(child);
                }
            } else {
                let tok = child.kind();
                if pending_op.is_empty() {
                    pending_op.push_str(tok);
                } else {
                    pending_op.push(' ');
                    pending_op.push_str(tok);
                }
            }
        }
        (ops, comparators)
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct Tuple {
    inner: NodeRef,
}

#[pymethods]
impl Tuple {
    #[getter]
    fn lineno(&self) -> usize {
        self.inner.lineno()
    }
    #[getter]
    fn col_offset(&self) -> usize {
        self.inner.col_offset()
    }
    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }
    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct List {
    inner: NodeRef,
}

#[pymethods]
impl List {
    #[getter]
    fn lineno(&self) -> usize {
        self.inner.lineno()
    }
    #[getter]
    fn col_offset(&self) -> usize {
        self.inner.col_offset()
    }
    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }
    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
    }
}

#[pyclass(module = "nib.ast")]
pub(crate) struct Dict {
    inner: NodeRef,
}

#[pymethods]
impl Dict {
    #[getter]
    fn lineno(&self) -> usize {
        self.inner.lineno()
    }
    #[getter]
    fn col_offset(&self) -> usize {
        self.inner.col_offset()
    }
    #[getter]
    fn end_lineno(&self) -> usize {
        self.inner.end_lineno()
    }
    #[getter]
    fn end_col_offset(&self) -> usize {
        self.inner.end_col_offset()
    }
}

/// Wrap a tree-sitter node as the matching Python AST class. `None` for kinds
/// we don't have a wrapper for (comprehensions, comments, ...).
pub(crate) fn wrap_node(py: Python, n: &NodeRef) -> PyResult<Option<Py<PyAny>>> {
    let kind = n.node().kind();
    // CPython's `ast` has no Paren node — `(expr)` is just `expr`. Tree-sitter
    // preserves the parens as a wrapping node, so we transparently descend.
    if kind == "parenthesized_expression"
        && let Some(inner) = n.node().named_child(0)
    {
        return wrap_node(py, &n.child_with(inner));
    }
    let obj: Py<PyAny> = match kind {
        "module" => Py::new(py, Module { inner: n.clone() })?.into_any(),
        "call" => Py::new(py, Call { inner: n.clone() })?.into_any(),
        "identifier" => Py::new(py, Name { inner: n.clone() })?.into_any(),
        "attribute" => Py::new(py, Attribute { inner: n.clone() })?.into_any(),
        "subscript" => Py::new(py, Subscript { inner: n.clone() })?.into_any(),
        "conditional_expression" => Py::new(py, IfExp { inner: n.clone() })?.into_any(),
        "boolean_operator" => Py::new(py, BoolOp { inner: n.clone() })?.into_any(),
        "lambda" => Py::new(py, Lambda { inner: n.clone() })?.into_any(),
        "binary_operator" => Py::new(py, BinOp { inner: n.clone() })?.into_any(),
        "comparison_operator" => Py::new(py, Compare { inner: n.clone() })?.into_any(),
        "list" => Py::new(py, List { inner: n.clone() })?.into_any(),
        "tuple" => Py::new(py, Tuple { inner: n.clone() })?.into_any(),
        "dictionary" => Py::new(py, Dict { inner: n.clone() })?.into_any(),
        "integer" | "float" | "string" | "true" | "false" | "none" => {
            Py::new(py, Constant { inner: n.clone() })?.into_any()
        }
        _ => return Ok(None),
    };
    Ok(Some(obj))
}

/// Strict variant of `wrap_node`: errors if the node isn't wrappable.
/// Use in positions where a missing wrapper is a bug (e.g., `Call.func`).
/// The error reports the *deepest* unwrappable kind after transparent unwraps
/// (e.g., a parenthesized conditional reports "conditional_expression", not
/// "parenthesized_expression").
fn wrap_node_or_err(py: Python, n: &NodeRef) -> PyResult<Py<PyAny>> {
    wrap_node(py, n)?.ok_or_else(|| {
        pyo3::exceptions::PyRuntimeError::new_err(format!(
            "unsupported node kind: {}",
            deepest_unwrappable_kind(n),
        ))
    })
}

/// Walk through transparent wrappers (parenthesized_expression) to find the
/// kind that actually has no wrapper — what the error message should report.
fn deepest_unwrappable_kind(n: &NodeRef) -> String {
    let mut node = n.node();
    while node.kind() == "parenthesized_expression"
        && let Some(inner) = node.named_child(0)
    {
        node = inner;
    }
    node.kind().to_string()
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
