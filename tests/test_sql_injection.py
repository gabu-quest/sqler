"""Adversarial tests for SQL injection prevention via field/identifier validation."""

import re
from unittest.mock import MagicMock

import pytest

from sqler.db.sqler_db import SQLerDB
from sqler.exceptions import InvalidFieldNameError, InvalidIdentifierError
from sqler.fts import FTSIndex
from sqler.ops import checkpoint
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

# Subset safe for use as **kwargs keys (empty string excluded).
EVIL_FIELDS_KWARG = [f for f in EVIL_FIELDS if f != ""]

EVIL_IDENTIFIERS = [
    "idx; DROP TABLE t",
    "idx--comment",
    "my index",
    "idx'name",
    "",
]

# Underscore-prefixed payloads that should hit the validate_identifier branch.
EVIL_UNDERSCORE_FIELDS = [
    "_; DROP TABLE items",
    "_ UNION SELECT",
    "_has space",
    "_semi;colon",
]


# ---------------------------------------------------------------------------
# Validator unit tests
# ---------------------------------------------------------------------------


class TestValidateFieldName:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_rejects_evil_field(self, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            validate_field_name(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_exception_carries_field_attr(self, evil):
        with pytest.raises(InvalidFieldNameError) as exc_info:
            validate_field_name(evil)
        assert exc_info.value.field == evil

    @pytest.mark.parametrize(
        "valid",
        ["name", "meta_level", "meta.level", "_private", "camelCase123", "a.b.c"],
    )
    def test_accepts_valid_field(self, valid):
        assert validate_field_name(valid) == valid


class TestValidateIdentifier:
    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    def test_rejects_evil_identifier(self, evil):
        with pytest.raises(InvalidIdentifierError, match="Invalid identifier|non-empty string"):
            validate_identifier(evil)

    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    def test_exception_carries_identifier_attr(self, evil):
        with pytest.raises(InvalidIdentifierError) as exc_info:
            validate_identifier(evil)
        assert exc_info.value.identifier == evil

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
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            query.order_by(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS_KWARG)
    def test_order_by_desc_prefix_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name"):
            query.order_by(f"-{evil}")


class TestAggregateRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_sum_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            query.sum(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_avg_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            query.avg(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_min_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            query.min(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_max_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            query.max(evil)


class TestDistinctValuesRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            query.distinct_values(evil)


class TestUpdateRejectsFieldInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS_KWARG)
    def test_update_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name"):
            query.update(**{evil: "val"})

    @pytest.mark.parametrize("evil", EVIL_FIELDS_KWARG)
    def test_update_one_rejects(self, query, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name"):
            query.update_one(**{evil: "val"})


# ---------------------------------------------------------------------------
# DB-level injection tests
# ---------------------------------------------------------------------------


class TestCreateIndexRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_field_injection(self, db, evil):
        with pytest.raises(
            (InvalidFieldNameError, InvalidIdentifierError),
            match="Invalid field name|Invalid identifier|non-empty string",
        ):
            db.create_index("items", evil)

    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    def test_name_injection(self, db, evil):
        with pytest.raises(InvalidIdentifierError, match="Invalid identifier|non-empty string"):
            db.create_index("items", "name", name=evil)

    @pytest.mark.parametrize("evil", EVIL_UNDERSCORE_FIELDS)
    def test_underscore_prefixed_field_injection(self, db, evil):
        """Underscore-prefixed fields take the validate_identifier branch."""
        with pytest.raises(InvalidIdentifierError, match="Invalid identifier"):
            db.create_index("items", evil)


class TestDropIndexRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    def test_rejects(self, db, evil):
        with pytest.raises(InvalidIdentifierError, match="Invalid identifier|non-empty string"):
            db.drop_index(evil)


class TestPromotedColumnRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    def test_rejects(self, db, evil):
        with pytest.raises(InvalidIdentifierError, match="Invalid identifier|non-empty string"):
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


# ---------------------------------------------------------------------------
# FTSIndex injection tests
# ---------------------------------------------------------------------------


class _FakeModel:
    """Minimal fake model for FTSIndex tests (no real DB needed)."""

    _table = "articles"
    _db = None


class TestFTSIndexRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    def test_field_injection(self, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            FTSIndex(_FakeModel, fields=[evil])

    def test_mixed_fields_one_evil(self):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name"):
            FTSIndex(_FakeModel, fields=["title", "x'; DROP TABLE t; --"])

    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    def test_index_name_injection(self, evil):
        with pytest.raises(InvalidIdentifierError, match="Invalid identifier|non-empty string"):
            FTSIndex(_FakeModel, fields=["title"], index_name=evil)

    @pytest.mark.parametrize(
        "evil_tokenizer",
        [
            "porter'); DROP TABLE t; --",
            "unicode61'; DELETE FROM t; --",
            "x\"); DROP TABLE t",
            "",
        ],
    )
    def test_tokenizer_injection(self, evil_tokenizer):
        with pytest.raises(ValueError, match="Invalid tokenizer"):
            FTSIndex(_FakeModel, fields=["title"], tokenizer=evil_tokenizer)

    @pytest.mark.parametrize(
        "valid",
        ["porter unicode61", "unicode61", "ascii", "trigram", "porter"],
    )
    def test_accepts_valid_tokenizer(self, valid):
        fts = FTSIndex(_FakeModel, fields=["title"], tokenizer=valid)
        assert fts.tokenizer == valid

    def test_accepts_valid_fields(self):
        fts = FTSIndex(_FakeModel, fields=["title", "content", "meta_tags"])
        assert fts.fields == ["title", "content", "meta_tags"]

    def test_accepts_valid_index_name(self):
        fts = FTSIndex(_FakeModel, fields=["title"], index_name="articles_search")
        assert fts._index_table == "articles_search"


# ---------------------------------------------------------------------------
# Checkpoint mode validation tests
# ---------------------------------------------------------------------------


class TestCheckpointModeValidation:
    @pytest.mark.parametrize(
        "evil_mode",
        [
            "PASSIVE); DROP TABLE items; --",
            "'; DELETE FROM sqlite_master; --",
            "FULL; PRAGMA writable_schema=ON",
            "not_a_mode",
        ],
    )
    def test_rejects_evil_mode(self, db, evil_mode):
        with pytest.raises(ValueError, match="Invalid checkpoint mode"):
            checkpoint(db, mode=evil_mode)

    @pytest.mark.parametrize("valid", ["PASSIVE", "FULL", "RESTART", "TRUNCATE"])
    def test_accepts_valid_mode(self, db, valid):
        # In-memory DBs use journal mode=memory, so checkpoint is a no-op
        # but it should not raise ValueError
        result = checkpoint(db, mode=valid)
        assert isinstance(result, dict)

    def test_case_insensitive(self, db):
        result = checkpoint(db, mode="passive")
        assert isinstance(result, dict)
