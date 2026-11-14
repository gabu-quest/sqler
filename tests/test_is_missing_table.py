"""There is one ``is_missing_table``, in the database layer; the model layer's old name is it."""

import sqlite3

import pytest
from sqler.db import _missing_table
from sqler.models import _table_names


def test_model_layer_name_is_the_database_layer_function():
    assert _table_names._is_missing_table is _missing_table.is_missing_table


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
    assert _missing_table.is_missing_table(sqlite3.OperationalError(message), "docs") is expected
