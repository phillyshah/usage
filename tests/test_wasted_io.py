"""'I/O' (and 'io') marks a component wasted, alongside the prior W/wasted/yes/true."""
import pytest

from app.pipeline.assemble import _is_wasted


@pytest.mark.parametrize("vline", [
    {"wasted": "I/O"},
    {"wasted": "io"},
    {"wasted": {"value": "I/O", "confidence": "high"}},
    # Regression: the prior truthy spellings must still mark wasted.
    {"wasted": "W"},
    {"wasted": "wasted"},
    {"wasted": True},
    {"wasted": "yes"},
])
def test_wasted_true(vline):
    assert _is_wasted(vline) is True


@pytest.mark.parametrize("vline", [
    {"wasted": "x"},
    {"wasted": None},
    {},
])
def test_wasted_false(vline):
    assert _is_wasted(vline) is False
