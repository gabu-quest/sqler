import json

import pytest

from sqler.query import SQLerExpression, SQLerQuery


@pytest.fixture
def query_obj(dummy_adapter):
    # now it takes an adapter too to run queries
    q = SQLerQuery(table="oligos", adapter=dummy_adapter)
    return q, dummy_adapter


def test_can_build_queries():
    """can we combine expresions into queries?"""

    # make a query obj
    # needs to know the tablename?
    q = SQLerQuery(table="oligos")

    # some expressions
    expression1 = SQLerExpression("length > ?", [20])
    expression2 = SQLerExpression("sequence = ?", ["ACGT"])

    # does it have the correct initial sql?
    assert q.sql == "SELECT data FROM oligos"
    assert q.params == []

    # can we construct sql?
    q = q.filter(expression1)
    assert q.sql == "SELECT data FROM oligos WHERE length > ?"
    assert q.params == [20]

    # should return another query obj that we can chain
    q = q.filter(expression2)
    assert q.sql == "SELECT data FROM oligos WHERE (length > ?) AND (sequence = ?)"
    assert q.params == [20, "ACGT"]


def test_limit_builds_sql(query_obj):
    """can we add a limit?"""
    q, _ = query_obj
    expr = SQLerExpression("length > ?", [10])
    q2 = q.filter(expr).limit(5)
    assert q2.sql == "SELECT data FROM oligos WHERE length > ? LIMIT 5"
    assert q2.params == [10]


def test_order_by_builds_sql(query_obj):
    """can we order by?"""
    q, _ = query_obj
    expr = SQLerExpression("sequence = ?", ["ATGC"])
    q2 = q.filter(expr).order_by("sequence")
    assert (
        q2.sql
        == "SELECT data FROM oligos WHERE sequence = ? ORDER BY json_extract(data, '$.sequence')"
    )
    assert q2.params == ["ATGC"]
    q3 = q2.order_by("length", desc=True)
    assert "DESC" in q3.sql


def test_exclude_builds_sql(query_obj):
    """can we exclude something?"""
    q, _ = query_obj
    expr = SQLerExpression("length = ?", [12])
    q2 = q.exclude(expr)
    assert "NOT (" in q2.sql


def test_count_runs_adapter(query_obj):
    """can we count instead of SELECT?"""
    q, adapter = query_obj
    expr = SQLerExpression("sequence = ?", ["ACGT"])
    q = q.filter(expr)
    adapter.count = 42
    count = q.count()
    assert count == 42
    assert "count(*)" in adapter.executed[-1][0]


def test_all_runs_adapter(query_obj):
    """can we query .all()?"""
    q, adapter = query_obj
    expr = SQLerExpression("length > ?", [5])
    q = q.filter(expr)
    adapter.return_value = [{"sequence": "ACGTACGT", "length": 8}]
    _ = q.all()
    # result is what DummyAdapter returned
    assert adapter.executed[-1][0].startswith("SELECT data")


def test_first_returns_first_result(dummy_adapter):
    q = SQLerQuery(table="oligos", adapter=dummy_adapter)
    expr = SQLerExpression("length > ?", [4])
    q = q.filter(expr)

    # dummy data: two matching oligos
    dummy_adapter.return_value = [
        {"sequence": "ACGTAC", "length": 6, "_id": 1},
        {"sequence": "TTGGCCA", "length": 7, "_id": 2},
    ]

    result = json.loads(q.first())
    assert result == {"sequence": "ACGTAC", "length": 6, "_id": 1}
    # should generate limit 1 in sql
    assert "LIMIT 1" in q.limit(1).sql


def test_first_returns_none_if_empty(dummy_adapter):
    q = SQLerQuery(table="oligos", adapter=dummy_adapter)
    expr = SQLerExpression("length > ?", [20])
    q = q.filter(expr)
    dummy_adapter.return_value = []
    assert q.first() is None
