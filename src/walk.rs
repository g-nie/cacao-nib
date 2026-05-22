use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;
use std::path::Path;
use walkdir::WalkDir;

/// Collect `.py` files under `root`. If `root` is a file, returns just that
/// path (regardless of extension — the CLI was told explicitly to lint it).
/// Results are sorted for deterministic output.
#[pyfunction]
pub(crate) fn collect_py_files(root: &str) -> PyResult<Vec<String>> {
    let path = Path::new(root);
    if path.is_file() {
        return Ok(vec![root.to_string()]);
    }

    let mut out = Vec::new();
    for entry in WalkDir::new(path).sort_by_file_name() {
        let entry = entry.map_err(|e| PyOSError::new_err(e.to_string()))?;
        // Walkdir yields dirs too; we want only regular files ending in .py.
        if entry.file_type().is_file()
            && entry.path().extension().is_some_and(|ext| ext == "py")
        {
            out.push(entry.path().to_string_lossy().into_owned());
        }
    }
    Ok(out)
}
