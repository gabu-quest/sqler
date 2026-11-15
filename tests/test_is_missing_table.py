"""There is one ``is_missing_table``, in the query layer; the model layer's old name is it."""

import sqlite3

import pytest
from sqler.models import _table_names
from sqler.query import query


def test_model_layer_name_is_the_query_layer_function():
    assert _table_names._is_missing_table is query.is_missing_table


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("no such table: docs", True),
        ("no such table: main.docs", True),
        ("no such table: other", False),
        ("no such column: docs", False),
    ],
)
def test_is_missing_table_names_only_the_table_itself(message, expected):
    assert query.is_missing_table(sqlite3.OperationalError(message), "docs") is expected
