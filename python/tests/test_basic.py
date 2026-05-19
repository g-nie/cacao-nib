import pytest

import cacao_nib


def test_add():
    assert cacao_nib.add(2, 3) == 5
    assert cacao_nib.add(-4, 4) == 0
    assert cacao_nib.add(0, 0) == 0


def test_multiply():
    assert cacao_nib.multiply(3, 4) == 12
    assert cacao_nib.multiply(-2, 5) == -10
    assert cacao_nib.multiply(0, 999) == 0


def test_add_rejects_non_int():
    with pytest.raises(TypeError):
        cacao_nib.add("a", "b")


def test_multiply_rejects_float():
    with pytest.raises(TypeError):
        cacao_nib.multiply(1.5, 2)
