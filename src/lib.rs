use pyo3::prelude::*;

#[pymodule]
pub mod cacao_nib {
    use pyo3::prelude::*;

    /// Add two integers in Rust.
    #[pyfunction]
    pub fn add(a: i64, b: i64) -> i64 {
        a + b
    }

    /// Multiply two integers in Rust.
    #[pyfunction]
    pub fn multiply(a: i64, b: i64) -> i64 {
        a * b
    }
}

#[cfg(test)]
mod tests {
    use super::cacao_nib::{add, multiply};

    #[test]
    fn add_works() {
        assert_eq!(add(2, 3), 5);
        assert_eq!(add(-4, 4), 0);
    }

    #[test]
    fn multiply_works() {
        assert_eq!(multiply(3, 4), 12);
        assert_eq!(multiply(-2, 5), -10);
    }
}
