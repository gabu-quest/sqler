"""is_ref_dict: what counts as a stored reference."""

import pytest

from sqler.utils import is_ref_dict


class TestReferenceUtilities:
    """Tests for reference-related utilities."""

    def test_is_ref_dict(self):
        """Test reference dictionary detection."""
        assert is_ref_dict({"_table": "users", "_id": 1})
        assert not is_ref_dict({"name": "Alice"})
        assert not is_ref_dict({"_table": "users"})  # missing _id
        assert not is_ref_dict([])
        assert not is_ref_dict("string")

    @pytest.mark.parametrize(
        "value, is_ref",
        [
            ({"_table": "t", "_id": 1}, True),
            ({"_table": "t", "_id": True}, False),
            ({"_table": "t", "_id": "1"}, False),
            ({"_table": 1, "_id": 1}, False),
            ({"_table": "t", "_id": 1, "label": "kept"}, False),
            ({"_id": 1}, False),
        ],
        ids=repr,
    )
    def test_a_reference_is_exactly_a_str_table_and_an_int_id(self, value, is_ref):
        assert is_ref_dict(value) is is_ref
