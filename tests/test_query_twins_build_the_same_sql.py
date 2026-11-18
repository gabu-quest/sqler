"""The sync and async query classes build the same SQL text and parameters for the same query.

Each shape is made with the same arguments on both classes; every builder (and ``sql``,
``params`` and ``debug()``) is compared pair by pair. Three shapes are also asserted
literally, so the test fails when both classes are wrong together.
"""

from typing import Any, Callable

import pytest

from sqler.query import F, SQLerQuery
from sqler.query.async_query import AsyncSQLerQuery

AGGREGATES = ["COUNT", "SUM", "AVG", "MIN", "MAX"]
AGGREGATE_FIELDS = [None, "price", "_id", "meta.score"]

SHAPES: dict[str, Callable[[Any], Any]] = {
    "empty": lambda q: q("t"),
    "filter": lambda q: q("t").filter(F("age") > 30),
    "exclude": lambda q: q("t").exclude(F("name") == "bob"),
    "any_over_objects": lambda q: q("t").filter(F("items").any()["sku"] == "a1"),
    "and_or": lambda q: q("t").filter(F("a") == 1).filter(F("b") == 2).or_filter(F("c") == 3),
    "order_one": lambda q: q("t").order_by("name"),
    "order_one_desc": lambda q: q("t").order_by("name", desc=True),
    "order_many": lambda q: q("t").order_by("-priority", "eta", "-ulid"),
    "order_legacy_constructor": lambda q: q("t", order="name", desc=True),
    "limit": lambda q: q("t").limit(5),
    "offset_only": lambda q: q("t").offset(7),
    "limit_offset": lambda q: q("t").limit(5).offset(10),
    "distinct": lambda q: q("t").distinct(),
    "select_fields": lambda q: q("t").select("name", "age"),
    "versioned": lambda q: q("t").filter(F("a") == 1).with_version(),
    "promoted": lambda q: q("t", promoted_fields=["status", "rank"]).filter(F("status") == "on"),
    "promoted_versioned": lambda q: q(
        "t", promoted_fields=["status"], include_version=True
    ).order_by("status"),
    "meta_columns": lambda q: q("t").filter(F("_id") > 3).order_by("_id"),
    "grouped": lambda q: q("t").group_by("city"),
    "grouped_two": lambda q: q("t").filter(F("a") == 1).group_by("city", "_version"),
    "grouped_promoted": lambda q: q("t", promoted_fields=["city", "price"]).group_by("city"),
    "grouped_having": lambda q: q("t").filter(F("a") == 1).group_by("city").having(F("_count") > 2),
    "grouped_having_sum": lambda q: q("t").group_by("city").having(F("_sum") > 100),
    "grouped_having_all": lambda q: q("t")
    .group_by("city")
    .having((F("_min") < 1) | (F("_max") > 9) | (F("_avg") > 4)),
}

GROUPED = [name for name in SHAPES if name.startswith("grouped")]


def both(name: str) -> tuple[Any, Any]:
    return SHAPES[name](SQLerQuery), SHAPES[name](AsyncSQLerQuery)


@pytest.mark.parametrize("name", list(SHAPES))
def test_select_is_the_same(name):
    sync, twin = both(name)
    for include_id in (False, True):
        assert twin._build_query(include_id=include_id) == sync._build_query(include_id=include_id)
    assert twin.sql == sync.sql
    assert twin.params == sync.params
    assert twin.debug() == sync.debug()


@pytest.mark.parametrize("name", list(SHAPES))
def test_aggregates_are_the_same(name):
    sync, twin = both(name)
    for func in AGGREGATES:
        for field in AGGREGATE_FIELDS:
            assert twin._build_aggregate_query(func, field) == sync._build_aggregate_query(
                func, field
            )


@pytest.mark.parametrize("name", GROUPED)
def test_grouped_aggregates_are_the_same(name):
    sync, twin = both(name)
    for func in AGGREGATES:
        for field in AGGREGATE_FIELDS:
            assert twin._build_grouped_aggregate_query(
                func, field
            ) == sync._build_grouped_aggregate_query(func, field)


@pytest.mark.parametrize("field", ["_id", "_version", "city", "meta.score"])
def test_field_expression_is_the_same(field):
    sync = SQLerQuery("t", promoted_fields=["city"])
    twin = AsyncSQLerQuery("t", promoted_fields=["city"])
    assert twin._field_expr(field) == sync._field_expr(field)


@pytest.mark.parametrize("cls", [SQLerQuery, AsyncSQLerQuery])
def test_literal_filter_order_limit(cls):
    q = cls("t").filter(F("age") > 30).order_by("-name", "id").limit(5).offset(10)
    assert q.sql == (
        "SELECT data FROM t WHERE JSON_EXTRACT(data, '$.age') > ? "
        "ORDER BY json_extract(data, '$.name') DESC, json_extract(data, '$.id') LIMIT 5 OFFSET 10"
    )
    assert q.params == [30]


@pytest.mark.parametrize("cls", [SQLerQuery, AsyncSQLerQuery])
def test_literal_promoted_versioned_with_id(cls):
    q = (
        cls("t", promoted_fields=["status"], include_version=True)
        .filter(F("status") == "on")
        .distinct()
    )
    assert q._build_query(include_id=True) == (
        "SELECT DISTINCT _id, data, _version, status FROM t WHERE status = ?",
        ["on"],
    )


@pytest.mark.parametrize("cls", [SQLerQuery, AsyncSQLerQuery])
def test_literal_grouped_having(cls):
    q = cls("t").filter(F("a") == 1).group_by("city").having(F("_count") > 2)
    assert q._build_grouped_aggregate_query("SUM", "price") == (
        "SELECT json_extract(data, '$.city'), SUM(json_extract(data, '$.price')) FROM t "
        "WHERE JSON_EXTRACT(data, '$.a') = ? GROUP BY json_extract(data, '$.city') HAVING COUNT(*) > ?",
        [1, 2],
    )
