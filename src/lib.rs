mod parser;
mod rules;
mod walk;

use pyo3::prelude::*;
use pyo3::types::PyModule as PyModuleType;

#[pymodule]
fn nib(m: &Bound<'_, PyModuleType>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parser::parse_module, m)?)?;
    m.add_function(wrap_pyfunction!(rules::run, m)?)?;
    m.add_function(wrap_pyfunction!(walk::collect_py_files, m)?)?;
    m.add_class::<rules::Diagnostic>()?;

    let py = m.py();
    let ast = PyModuleType::new(py, "ast")?;
    ast.add_class::<parser::Module>()?;
    ast.add_class::<parser::Call>()?;
    ast.add_class::<parser::Name>()?;
    ast.add_class::<parser::Attribute>()?;
    ast.add_class::<parser::Subscript>()?;
    ast.add_class::<parser::IfExp>()?;
    ast.add_class::<parser::BoolOp>()?;
    ast.add_class::<parser::Lambda>()?;
    ast.add_class::<parser::BinOp>()?;
    ast.add_class::<parser::Compare>()?;
    ast.add_class::<parser::List>()?;
    ast.add_class::<parser::Dict>()?;
    ast.add_class::<parser::Constant>()?;
    m.add_submodule(&ast)?;
    Ok(())
}
