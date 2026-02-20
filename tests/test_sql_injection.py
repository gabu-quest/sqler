"""Adversarial tests for SQL injection prevention via field/identifier validation."""

import pytest

from sqler.db.sqler_db import SQLerDB
from sqler.exceptions import InvalidFieldNameError, InvalidIdentifierError
from sqler.query.query import SQLerQuery
from sqler.utils import validate_field_name, validate_identifier

# ---------------------------------------------------------------------------
# Evil payloads
# ---------------------------------------------------------------------------

EVIL_FIELDS = [
    "x') FROM users--",
    "x'; DROP TABLE t; --",
    "1 OR 1=1",
    "' UNION SELECT * FROM sqlite_master --",
    "",
    "a..b",
    ".leading",
    "trailing.",
    "has space",
    "semi;colon",
]

EVIL_IDENTIFIERS = [
    "idx; DROP TABLE t",
    "idx--comment",
    "my index",
    "idx'name",
    "",
]


# ---------------------------------------------------------------------------
# Validator unit tests
# ---------------------------------------------------------------------------


class TestValidateFieldName:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_rejects_evil_field(self, evil):
        with pytest.raises(InvalidFieldNameError):
            validate_field_name(evil)

    @pytest.mark.parametrize(
        "valid",
        ["name", "meta_level", "meta.level", "_private", "camelCase123", "a.b.c"],
    )
    def test_accepts_valid_field(self, valid):
        assert validate_field_name(valid) == valid


class TestValidateIdentifier:
    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    def test_rejects_evil_identifier(self, evil):
        with pytest.raises(InvalidIdentifierError):
            validate_identifier(evil)

    @pytest.mark.parametrize(
        "valid",
        ["idx_users_name", "my_index", "_col", "A1"],
    )
    def test_accepts_valid_identifier(self, valid):
        assert validate_identifier(valid) == valid


# ---------------------------------------------------------------------------
# Query-level injection tests (sync)
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    d = SQLerDB.in_memory(shared=False)
    d.insert_document("items", {"name": "a", "score": 10})
    yield d
    d.close()


@pytest.fixture
def query(db):
    return db.query("items")


class TestOrderByRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_order_by_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError):
            query.order_by(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_order_by_desc_prefix_rejects(self, query, evil):
        if evil == "":
            pytest.skip("empty string has no desc prefix form")
        with pytest.raises(InvalidFieldNameError):
            query.order_by(f"-{evil}")


class TestAggregateRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_sum_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError):
            query.sum(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_avg_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError):
            query.avg(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_min_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError):
            query.min(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_max_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError):
            query.max(evil)


class TestDistinctValuesRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError):
            query.distinct_values(evil)


class TestUpdateRejectsFieldInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_update_rejects(self, query, evil):
        if evil == "":
            pytest.skip("empty string is not a valid kwarg key")
        with pytest.raises(InvalidFieldNameError):
            query.update(**{evil: "val"})

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_update_one_rejects(self, query, evil):
        if evil == "":
            pytest.skip("empty string is not a valid kwarg key")
        with pytest.raises(InvalidFieldNameError):
            query.update_one(**{evil: "val"})


# ---------------------------------------------------------------------------
# DB-level injection tests
# ---------------------------------------------------------------------------


class TestCreateIndexRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_field_injection(self, db, evil):
        with pytest.raises((InvalidFieldNameError, InvalidIdentifierError)):
            db.create_index("items", evil)

    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    def test_name_injection(self, db, evil):
        with pytest.raises(InvalidIdentifierError):
            db.create_index("items", "name", name=evil)


class TestDropIndexRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    def test_rejects(self, db, evil):
        with pytest.raises(InvalidIdentifierError):
            db.drop_index(evil)


class TestPromotedColumnRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    def test_rejects(self, db, evil):
        with pytest.raises(InvalidIdentifierError):
            db._ensure_table_with_promoted("items", {evil: "TEXT"})


# ---------------------------------------------------------------------------
# execute_sql restriction tests
# ---------------------------------------------------------------------------


class TestExecuteSqlRestriction:
    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE items",
            "DELETE FROM items WHERE 1=1",
            "INSERT INTO items (data) VALUES ('{}')",
            "UPDATE items SET data = '{}'",
            "ALTER TABLE items ADD COLUMN x TEXT",
        ],
    )
    def test_rejects_mutations(self, db, sql):
        with pytest.raises(ValueError, match="read-only"):
            db.execute_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1; DROP TABLE items",
            "SELECT 1; DELETE FROM items",
            "SELECT 1; INSERT INTO items (data) VALUES ('{}')",
        ],
    )
    def test_rejects_multi_statement(self, db, sql):
        with pytest.raises(ValueError, match="multi-statement"):
            db.execute_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "SELECT 1;",
            "EXPLAIN SELECT 1",
            "PRAGMA table_info('items')",
            "WITH cte AS (SELECT 1) SELECT * FROM cte",
        ],
    )
    def test_allows_reads(self, db, sql):
        # Should not raise
        db.execute_sql(sql)
