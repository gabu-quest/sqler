"""Tests for F-expression support in update()."""

import pytest
from sqler import SQLerDB, SQLerModel, SQLerSafeModel, F
from sqler.query.field import SQLerUpdateExpression


# ---- Expression building tests ----


class TestUpdateExpressionBuilding:
    def test_field_add(self):
        expr = F("count") + 1
        assert isinstance(expr, SQLerUpdateExpression)
        assert "json_extract(data, '$.count') + ?" in expr.sql_expr
        assert expr.params == [1]
        assert expr.field_name == "count"

    def test_field_sub(self):
        expr = F("count") - 1
        assert isinstance(expr, SQLerUpdateExpression)
        assert "- ?" in expr.sql_expr
        assert expr.params == [1]

    def test_field_mul(self):
        expr = F("delay") * 2
        assert isinstance(expr, SQLerUpdateExpression)
        assert "* ?" in expr.sql_expr
        assert expr.params == [2]

    def test_reverse_add(self):
        expr = 10 + F("count")
        assert isinstance(expr, SQLerUpdateExpression)
        assert "? +" in expr.sql_expr
        assert expr.params == [10]

    def test_chained_expression(self):
        expr = F("delay") * 2 + 10
        assert isinstance(expr, SQLerUpdateExpression)
        assert expr.params == [2, 10]

    def test_compound_sub_add(self):
        expr = F("score") - 5 + 10
        assert isinstance(expr, SQLerUpdateExpression)
        assert expr.params == [5, 10]


# ---- Execution tests (JSON fields) ----


class Counter(SQLerModel):
    count: int = 0
    name: str = ""


@pytest.fixture
def db_with_counters():
    db = SQLerDB.in_memory(shared=False)
    Counter.set_db(db, "counters")
    Counter(name="a", count=10).save()
    Counter(name="b", count=20).save()
    Counter(name="c", count=30).save()
    yield db
    db.close()


class TestFExpressionExecution:
    def test_increment(self, db_with_counters):
        Counter.query().filter(F("name") == "a").update(count=F("count") + 1)
        a = Counter.query().filter(F("name") == "a").first()
        assert a.count == 11

    def test_decrement(self, db_with_counters):
        Counter.query().filter(F("name") == "b").update(count=F("count") - 5)
        b = Counter.query().filter(F("name") == "b").first()
        assert b.count == 15

    def test_multiply(self, db_with_counters):
        Counter.query().filter(F("name") == "c").update(count=F("count") * 2)
        c = Counter.query().filter(F("name") == "c").first()
        assert c.count == 60

    def test_compound_expression(self, db_with_counters):
        Counter.query().filter(F("name") == "a").update(count=F("count") * 2 + 5)
        a = Counter.query().filter(F("name") == "a").first()
        assert a.count == 25  # 10 * 2 + 5

    def test_bulk_increment(self, db_with_counters):
        rows = Counter.query().update(count=F("count") + 100)
        assert rows == 3
        all_counters = Counter.query().order_by("name").all()
        assert [c.count for c in all_counters] == [110, 120, 130]

    def test_mixed_literal_and_fexpr(self, db_with_counters):
        Counter.query().filter(F("name") == "a").update(
            count=F("count") + 1,
            name="updated",
        )
        a = Counter.query().filter(F("name") == "updated").first()
        assert a is not None
        assert a.count == 11


# ---- Promoted column F-expression tests ----


class PromotedCounter(SQLerModel):
    __promoted__ = {"count": "INTEGER DEFAULT 0"}
    count: int = 0
    name: str = ""


@pytest.fixture
def db_with_promoted_counters():
    db = SQLerDB.in_memory(shared=False)
    PromotedCounter.set_db(db, "pcounters")
    PromotedCounter(name="a", count=10).save()
    PromotedCounter(name="b", count=20).save()
    yield db
    db.close()


class TestFExpressionPromoted:
    def test_increment_promoted(self, db_with_promoted_counters):
        PromotedCounter.query().filter(F("name") == "a").update(count=F("count") + 1)
        a = PromotedCounter.query().filter(F("name") == "a").first()
        assert a.count == 11

    def test_multiply_promoted(self, db_with_promoted_counters):
        PromotedCounter.query().filter(F("name") == "b").update(count=F("count") * 3)
        b = PromotedCounter.query().filter(F("name") == "b").first()
        assert b.count == 60

    def test_promoted_generates_direct_column_sql(self, db_with_promoted_counters):
        qs = PromotedCounter.query().filter(F("name") == "a")
        # The underlying query should rewrite promoted refs
        q = qs._query.filter(F("name") == "a")
        sql = q.sql
        # count should be direct column reference, not json_extract
        assert "count" in sql
