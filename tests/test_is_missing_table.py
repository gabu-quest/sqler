"""There is one ``is_missing_table``, in the query layer."""

import sqlite3

import pytest

from sqler.query import query


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
