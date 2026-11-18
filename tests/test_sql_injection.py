"""Adversarial tests for SQL injection prevention via field/identifier validation."""

from enum import IntEnum

import pytest

from sqler import SQLerModel, SQLerQuery
from sqler.db.sqler_db import SQLerDB
from sqler.exceptions import (
    InvalidColumnDefError,
    InvalidFieldNameError,
    InvalidIdentifierError,
    InvalidTableNameError,
)
from sqler.fts import FTSIndex
from sqler.ops import checkpoint
from sqler.utils import (
    validate_check_expression,
    validate_column_def,
    validate_column_name,
    validate_field_name,
    validate_identifier,
    validate_table_name,
)

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

EVIL_COL_DEFS = [
    "TEXT; DROP TABLE items",
    "TEXT DEFAULT (SELECT 1)",
    "TEXT DEFAULT random()",
    "TEXT/* injected */",
    "TEXT -- comment",
    "VARCHAR(10)",
    "JSON",
    "TEXT NOT NULL DEFAULT 'ok'; DROP TABLE items",
    "",
    "   ",
]

VALID_COL_DEFS = [
    "TEXT",
    "INTEGER",
    "REAL",
    "BLOB",
    "NUMERIC",
    "NUMERIC(10,2)",
    "TEXT NOT NULL",
    "TEXT NOT NULL DEFAULT 'pending'",
    "INTEGER DEFAULT 0",
    "REAL DEFAULT 3.14",
    "TEXT DEFAULT NULL",
    "TEXT DEFAULT CURRENT_TIMESTAMP",
    "text not null default 'lower'",
]

EVIL_CHECK_EXPRS = [
    "1=1; DROP TABLE items",
    "status IN ('a') -- comment",
    "status IN ('a') /* comment */",
    "status IN ((SELECT name FROM sqlite_master))",  # subselect itself is fine, but…
    "EXISTS (SELECT 1 FROM users)",  # bare SELECT not in DDL/DML keywords — kept permissive
]

# Keyword-based evil: these MUST be rejected by the banned-keyword check.
# (Semicolons are rejected by a separate structural check — keep these semicolon-free
# to exercise the keyword path specifically.)
EVIL_CHECK_KEYWORD_EXPRS = [
    "status IN (SELECT 1) OR INSERT INTO t VALUES (1)",
    "status = 'x' OR DELETE FROM items",
    "1=1 AND PRAGMA writable_schema=ON",
    "age > 0 OR ALTER TABLE items ADD x TEXT",
    "score > 0 OR CREATE TABLE t (x TEXT)",
    "status = 'x' OR DROP TABLE items",
    "age > 0 OR ATTACH DATABASE 'evil.db' AS e",
]

VALID_CHECK_EXPRS = [
    "status IN ('pending','active','done')",
    "age >= 0",
    "score > 0 AND score <= 100",
    "length(name) > 0",
    "priority BETWEEN 1 AND 5",
]


class TestValidateColumnDef:
    @pytest.mark.parametrize("evil", EVIL_COL_DEFS)
    def test_rejects_evil(self, evil):
        with pytest.raises(
            InvalidColumnDefError,
            match="Invalid column definition|non-empty string",
        ):
            validate_column_def(evil)

    @pytest.mark.parametrize("evil", EVIL_COL_DEFS)
    def test_exception_carries_definition(self, evil):
        with pytest.raises(InvalidColumnDefError) as exc_info:
            validate_column_def(evil)
        assert exc_info.value.definition == evil

    @pytest.mark.parametrize("valid", VALID_COL_DEFS)
    def test_accepts_valid(self, valid):
        assert validate_column_def(valid) == valid

    def test_rejects_non_string(self):
        with pytest.raises(InvalidColumnDefError, match="non-empty string"):
            validate_column_def(None)  # type: ignore[arg-type]


class TestValidateCheckExpression:
    @pytest.mark.parametrize(
        "evil",
        [
            "1=1; DROP TABLE items",
            "status = 'a' -- trick",
            "status = 'a' /* trick */",
            "",
            "   ",
        ],
    )
    def test_rejects_structural_evil(self, evil):
        with pytest.raises(
            InvalidColumnDefError,
            match="CHECK expression|non-empty string",
        ):
            validate_check_expression(evil)

    @pytest.mark.parametrize("evil", EVIL_CHECK_KEYWORD_EXPRS)
    def test_rejects_banned_keywords(self, evil):
        with pytest.raises(InvalidColumnDefError, match="may not contain keyword"):
            validate_check_expression(evil)

    @pytest.mark.parametrize("valid", VALID_CHECK_EXPRS)
    def test_accepts_valid(self, valid):
        assert validate_check_expression(valid) == valid

    def test_rejects_non_string(self):
        with pytest.raises(InvalidColumnDefError, match="non-empty string"):
            validate_check_expression(None)  # type: ignore[arg-type]


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
# limit() and offset(): a whole number, 0 or more, checked at the call
# ---------------------------------------------------------------------------

UNION = "1 UNION SELECT 1, sqlite_version()"

# (value, error, message): every value limit() and offset() refuse, and how.
BAD_COUNTS = [
    (UNION, TypeError, "takes a whole number, not str"),
    ("5", TypeError, "takes a whole number, not str"),
    ("1; DROP TABLE x", TypeError, "takes a whole number, not str"),
    (5.0, TypeError, "takes a whole number, not float"),
    (2.5, TypeError, "takes a whole number, not float"),
    (True, TypeError, "takes a whole number, not bool"),
    (None, TypeError, "takes a whole number, not NoneType"),
    (-1, ValueError, "must be >= 0"),
]
BAD_IDS = ["union", "digit-str", "drop", "float-whole", "float", "bool", "none", "negative"]


class Three(IntEnum):
    THREE = 3


@pytest.fixture
def scored():
    """Five items, ids 1 to 5, ``score`` 1 to 5."""
    d = SQLerDB.in_memory(shared=False)
    for s in range(1, 6):
        d.insert_document("items", {"score": s})
    yield d
    d.close()


def _scores(db, q) -> list[int]:
    return [d["score"] for d in q.all_dicts()]


class _Item(SQLerModel):
    score: int = 0
    __tablename__ = "items"


class TestLimitAndOffsetTakeOnlyWholeNumbers:
    @pytest.mark.parametrize("method", ["limit", "offset"])
    @pytest.mark.parametrize("value,error,message", BAD_COUNTS, ids=BAD_IDS)
    def test_a_query_refuses_at_the_call(self, query, method, value, error, message):
        with pytest.raises(error, match=rf"^{method}(\(\) | ){message}$"):
            getattr(query, method)(value)

    @pytest.mark.parametrize("method", ["limit", "offset"])
    @pytest.mark.parametrize("value,error,message", BAD_COUNTS, ids=BAD_IDS)
    def test_a_model_queryset_refuses_at_the_call(self, scored, method, value, error, message):
        with pytest.raises(error, match=rf"^{method}(\(\) | ){message}$"):
            getattr(_Item.using(scored), method)(value)

    @pytest.mark.parametrize("arg", ["limit", "offset"])
    def test_a_query_made_with_a_limit_or_an_offset_refuses_a_string(self, db, arg):
        with pytest.raises(TypeError, match=rf"^{arg}\(\) takes a whole number, not str$"):
            SQLerQuery("items", adapter=db.adapter, **{arg: UNION})

    @pytest.mark.parametrize(
        "write",
        [
            lambda q: q.limit("1; DROP TABLE x").delete(),
            lambda q: q.limit("1; DROP TABLE x").update(score=0),
            lambda q: q.offset("1; DROP TABLE x").delete(),
            lambda q: q.limit(2.5).delete(),
            lambda q: q.limit(-1).update_n(2, score=0),
        ],
        ids=["limit-str-delete", "limit-str-update", "offset-str-delete", "limit-float-delete", "limit-negative-update-n"],
    )
    def test_a_write_is_refused_before_any_row_is_written(self, scored, write):
        with pytest.raises((TypeError, ValueError), match=r"^(limit|offset)(\(\) takes a whole number| must be >= 0)"):
            write(_Item.using(scored))
        assert _scores(scored, scored.query("items").order_by("_id")) == [1, 2, 3, 4, 5]

    def test_limit_zero_returns_no_row(self, scored):
        assert _scores(scored, scored.query("items").limit(0)) == []

    def test_offset_zero_skips_no_row(self, scored):
        assert _scores(scored, scored.query("items").offset(0)) == [1, 2, 3, 4, 5]

    def test_an_int_like_is_taken_as_its_int(self, scored):
        q = scored.query("items").limit(Three.THREE).offset(Three.THREE)
        assert (type(q._limit), type(q._offset), _scores(scored, scored.query("items").limit(Three.THREE))) == (int, int, [1, 2, 3])
        assert q.debug() == scored.query("items").limit(3).offset(3).debug()

    def test_the_sql_of_a_valid_limit_and_offset(self, query):
        assert query.limit(5).offset(2).debug() == ("SELECT data FROM items LIMIT 5 OFFSET 2", [])


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


class TestPromotedColumnDefRejectsInjection:
    """H-3: _ensure_table_with_promoted must validate col_def strings."""

    @pytest.mark.parametrize("evil_def", EVIL_COL_DEFS)
    def test_rejects_evil_col_def(self, db, evil_def):
        with pytest.raises(
            InvalidColumnDefError,
            match="Invalid column definition|non-empty string",
        ):
            db._ensure_table_with_promoted("items", {"status": evil_def})

    @pytest.mark.parametrize("valid_def", ["TEXT", "INTEGER", "TEXT NOT NULL DEFAULT 'ok'"])
    def test_accepts_valid_col_def(self, db, valid_def):
        # Should not raise
        db._ensure_table_with_promoted("items_ok", {"status": valid_def})


class TestPromotedChecksRejectInjection:
    """H-3: _ensure_table_with_promoted must validate CHECK expressions."""

    def test_rejects_semicolon_in_check(self, db):
        with pytest.raises(InvalidColumnDefError, match="may not contain ';'"):
            db._ensure_table_with_promoted(
                "items",
                {"status": "TEXT"},
                checks={"evil": "status = 'x'; DROP TABLE items"},
            )

    def test_rejects_ddl_keyword_in_check(self, db):
        with pytest.raises(InvalidColumnDefError, match="may not contain keyword"):
            db._ensure_table_with_promoted(
                "items",
                {"status": "TEXT"},
                checks={"evil": "status = 'x' OR DROP TABLE items"},
            )

    def test_accepts_valid_check(self, db):
        # Should not raise
        db._ensure_table_with_promoted(
            "items_ok",
            {"status": "TEXT"},
            checks={"valid": "status IN ('a','b','c')"},
        )


class TestCreateIndexWhereRejectsInjection:
    """H-1: create_index(where=...) must reject DDL-injection payloads."""

    @pytest.mark.parametrize(
        "evil_where",
        [
            "1=1); DROP TABLE items; --",
            "1=1 -- trick",
            "1=1 /* trick */",
            "1=1 OR DROP TABLE items",
            "1=1 OR PRAGMA writable_schema=ON",
            "1=1 OR ALTER TABLE items ADD x TEXT",
        ],
    )
    def test_rejects_evil_where(self, db, evil_where):
        with pytest.raises(
            InvalidColumnDefError,
            match="may not contain|non-empty string",
        ):
            db.create_index("items", "name", where=evil_where)

    def test_accepts_legit_partial_where(self, db):
        # The documented use case from README.ja.md / docs/API.md must keep working
        db.create_index(
            "items",
            "name",
            where="json_extract(data,'$.name') IS NOT NULL",
        )


class TestSetDbRejectsInjection:
    """H-2: set_db() must validate table names on Pydantic and Lite backends."""

    @pytest.mark.parametrize(
        "evil_table",
        [
            "users; DROP TABLE x",
            "users--comment",
            "has space",
            "'quoted'",
        ],
    )
    def test_pydantic_set_db_rejects(self, evil_table):
        import warnings as _w

        from sqler import SQLerDB, SQLerModel
        from sqler.exceptions import InvalidTableNameError

        class _User(SQLerModel):
            name: str = ""

        d = SQLerDB.in_memory(shared=False)
        try:
            with _w.catch_warnings():
                _w.simplefilter("ignore", DeprecationWarning)
                with pytest.raises(
                    InvalidTableNameError,
                    match="Invalid table name|non-empty string",
                ):
                    _User.set_db(d, table=evil_table)
        finally:
            d.close()

    @pytest.mark.parametrize(
        "evil_table",
        [
            "users; DROP TABLE x",
            "users--comment",
            "has space",
            "'quoted'",
        ],
    )
    def test_lite_set_db_rejects(self, evil_table):
        import warnings as _w
        from dataclasses import dataclass

        from sqler import SQLerDB, SQLerLiteModel
        from sqler.exceptions import InvalidTableNameError

        @dataclass
        class _LiteUser(SQLerLiteModel):
            name: str = ""

        d = SQLerDB.in_memory(shared=False)
        try:
            with _w.catch_warnings():
                _w.simplefilter("ignore", DeprecationWarning)
                with pytest.raises(
                    InvalidTableNameError,
                    match="Invalid table name|non-empty string",
                ):
                    _LiteUser.set_db(d, table=evil_table)
        finally:
            d.close()


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


# ---------------------------------------------------------------------------
# Reserved-word protection for promoted column names
# ---------------------------------------------------------------------------

RESERVED_COLUMN_NAMES = [
    "index", "order", "group", "table", "select", "from", "where",
    "values", "set", "key", "primary", "column", "create", "drop",
    "insert", "update", "delete", "join", "on", "having", "limit",
    "offset", "union", "check", "default", "foreign", "references",
    "constraint", "unique", "case", "when", "then", "else", "end",
    "between", "like", "in", "is", "not", "null", "and", "or", "as",
]


class TestReservedWordProtection:
    """Reserved words must be rejected as promoted column names."""

    @pytest.mark.parametrize("word", RESERVED_COLUMN_NAMES)
    def test_validate_column_name_rejects_reserved(self, word):
        with pytest.raises(InvalidIdentifierError, match="reserved word"):
            validate_column_name(word)

    @pytest.mark.parametrize("word", RESERVED_COLUMN_NAMES)
    def test_case_insensitive_rejection(self, word):
        with pytest.raises(InvalidIdentifierError, match="reserved word"):
            validate_column_name(word.upper())

    @pytest.mark.parametrize("name", ["status", "name", "email", "age", "score", "is_active"])
    def test_accepts_non_reserved_names(self, name):
        assert validate_column_name(name) == name

    def test_promoted_column_rejects_reserved_at_table_creation(self, db):
        with pytest.raises(InvalidIdentifierError, match="reserved word"):
            db._ensure_table_with_promoted("items", {"index": "TEXT"})

    def test_promoted_column_rejects_order(self, db):
        with pytest.raises(InvalidIdentifierError, match="reserved word"):
            db._ensure_table_with_promoted("items", {"order": "INTEGER"})

    def test_promoted_column_accepts_valid_names(self, db):
        db._ensure_table_with_promoted("items", {"status": "TEXT", "priority": "INTEGER"})
        # Verify table was created with the columns
        cursor = db.adapter.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='items';"
        )
        row = cursor.fetchone()
        assert row is not None
        ddl = row[0]
        assert "status" in ddl
        assert "priority" in ddl


# ---------------------------------------------------------------------------
# names stored in documents / used as JSON keys never change the statement
# ---------------------------------------------------------------------------

from typing import Union  # noqa: E402

from pydantic import Field as _PydField  # noqa: E402

from sqler import SQLerDB as _SQLerDB  # noqa: E402
from sqler.logging import query_logger  # noqa: E402
from sqler.models import SQLerModel  # noqa: E402
from sqler.query import SQLerField as F  # noqa: E402

HOSTILE_TABLES = [
    "'; DROP TABLE users; --",
    "users WHERE 1=1 UNION SELECT _id, data FROM secrets --",
    "secrets WHERE 1 UNION SELECT _id, data FROM secrets",
    "users where",
    "",
    "a b",
    "a\nb",
    "t\x00",
    "名前",
    "x" * 10000 + "'",
]

BAD_IDS = ["1 OR 1=1", None, 1.5, [1], True, {"a": 1}, "1", 2**63, -(2**63) - 1, 10**20]
EDGE_IDS = [2**63 - 1, -(2**63)]
TRAILING_NEWLINE_NAMES = ["users\n", "users\r\n", "\nusers", "a\n", "_\n"]


class HolderUser(SQLerModel):
    __tablename__ = "hr_users"
    name: str = ""


# a typed position, so that what is refused here is refused by the walk, not by the rule
# that an untyped field is never followed
class Holder(SQLerModel):
    payload: dict[str, Union[dict, HolderUser]] = _PydField(default_factory=dict)


@pytest.fixture
def rdb():
    d = _SQLerDB.in_memory(shared=False)
    HolderUser.set_db(d)
    Holder.set_db(d)
    d.insert_document("secrets", {"secret": "s3"})
    d.insert_document("hr_users", {"name": "u1"})
    yield d
    d.close()


class TestLoaderRejectsHostileRefs:
    # a reference the loader refuses leads to no row of the field's class: it is left out of
    # the dict, and no statement is sent for it: every statement names the holder's table

    @staticmethod
    def _load(rdb, payload):
        rid = rdb.insert_document(Holder.__tablename__, {"payload": payload})
        with query_logger.capture() as sent:
            got = Holder.from_id(rid)
        return got.payload, sum(Holder.__tablename__ not in q.sql for q in sent)

    @pytest.mark.parametrize("table", HOSTILE_TABLES)
    def test_hostile_table_left_out(self, rdb, table):
        assert self._load(rdb, {"k": {"_table": table, "_id": 1}}) == ({}, 0)

    @pytest.mark.parametrize("bad", BAD_IDS, ids=repr)
    def test_bad_id_left_out(self, rdb, bad):
        # an ``_id`` that is no int (a bool neither) makes the dict data, kept as it is; an
        # int no row can have makes a reference to no row, left out
        held = {"_table": "hr_users", "_id": bad}
        kept = {} if type(bad) is int else {"k": held}
        assert self._load(rdb, {"k": held}) == (kept, 0)

    @pytest.mark.parametrize("edge", EDGE_IDS, ids=repr)
    def test_edge_id_without_row_left_out(self, rdb, edge):
        assert self._load(rdb, {"k": {"_table": "hr_users", "_id": edge}})[0] == {}

    @pytest.mark.parametrize("table", ["hr_users\n", "hr_users\r\n", "\nhr_users"], ids=repr)
    def test_newline_table_left_out(self, rdb, table):
        assert self._load(rdb, {"k": {"_table": table, "_id": 1}}) == ({}, 0)

    def test_bad_ref_does_not_stop_good_ref(self, rdb):
        bad = {"_table": "hr_users; x", "_id": 1}
        good = {"_table": "hr_users", "_id": 1}
        payload, _ = self._load(rdb, {"a": bad, "b": good})
        assert list(payload) == ["b"]
        assert payload["b"].name == "u1"

    def test_end_to_end_all(self, rdb):
        ref = {"_table": "secrets WHERE 1 UNION SELECT _id, data FROM secrets", "_id": 1}
        Holder(payload={"k": ref}).save()
        got = Holder.query().all()
        assert got[0].payload == {}


# --- JSON path literals -----------------------------------------------------

LITERAL_NAMES = [
    "x') OR 1=1 --",
    "it's",
    'a"b',
    "'; DROP TABLE users; --",
    "a..b",
    "a.",
    ".a",
    "n" * 10000,
    "名前",
    "two words"
]
UNMATCHABLE_NAMES = ['a\\"b', "a\\'b", "a\\b", "a\x00b", "a\nb", "a\tb", "a\u00a0b", "a  b"]


@pytest.fixture
def jdb():
    d = _SQLerDB.in_memory(shared=False)
    yield d
    d.close()


def _ids(db, expr):
    return sorted(r["_id"] for r in db.query("docs").filter(expr).all_dicts())


class TestJsonPathLiteralEscaping:
    @pytest.mark.parametrize("name", LITERAL_NAMES, ids=lambda s: repr(s[:20]))
    def test_field_matches_key_literally(self, jdb, name):
        a = jdb.insert_document("docs", {name: 1})
        jdb.insert_document("docs", {"plain": 1})
        assert _ids(jdb, F(name) == 1) == [a]

    @pytest.mark.parametrize("name", LITERAL_NAMES, ids=lambda s: repr(s[:20]))
    def test_nested_key_matches_literally(self, jdb, name):
        a = jdb.insert_document("docs", {"o": {name: 1}})
        jdb.insert_document("docs", {"o": {"plain": 1}})
        assert _ids(jdb, F("o")[name] == 1) == [a]

    @pytest.mark.parametrize("name", UNMATCHABLE_NAMES, ids=repr)
    def test_unrepresentable_name_raises_sqler_error(self, jdb, name):
        jdb.insert_document("docs", {"plain": 1})
        with pytest.raises(InvalidFieldNameError):
            _ids(jdb, F(name) == 1)

    @pytest.mark.parametrize("name", LITERAL_NAMES, ids=lambda s: repr(s[:20]))
    def test_any_last_field(self, jdb, name):
        a = jdb.insert_document("docs", {"arr": [{name: 1}]})
        jdb.insert_document("docs", {"arr": [{"plain": 1}]})
        assert _ids(jdb, F("arr").any()[name] == 1) == [a]

    @pytest.mark.parametrize("name", LITERAL_NAMES, ids=lambda s: repr(s[:20]))
    def test_any_array_key(self, jdb, name):
        a = jdb.insert_document("docs", {name: [{"v": 1}]})
        jdb.insert_document("docs", {"plain": [{"v": 1}]})
        assert _ids(jdb, F(name).any()["v"] == 1) == [a]

    @pytest.mark.parametrize("name", LITERAL_NAMES, ids=lambda s: repr(s[:20]))
    def test_any_nested_array_key(self, jdb, name):
        a = jdb.insert_document("docs", {"o": [{name: [{"v": 1}]}]})
        jdb.insert_document("docs", {"o": [{"plain": [{"v": 1}]}]})
        assert _ids(jdb, F("o").any()[name].any()["v"] == 1) == [a]

    @pytest.mark.parametrize("name", LITERAL_NAMES, ids=lambda s: repr(s[:20]))
    def test_any_context_where_nested_key(self, jdb, name):
        a = jdb.insert_document("docs", {"o": [{name: 1}]})
        jdb.insert_document("docs", {"o": [{"plain": 1}]})
        expr = F("o").any().where(F(name) == 1)
        assert _ids(jdb, expr.to_expression()) == [a]

    @pytest.mark.parametrize("name", UNMATCHABLE_NAMES, ids=repr)
    def test_any_unrepresentable_raises(self, jdb, name):
        with pytest.raises(InvalidFieldNameError):
            _ids(jdb, F("arr").any()[name] == 1)
        with pytest.raises(InvalidFieldNameError):
            _ids(jdb, F(name).any()["v"] == 1)

    @pytest.mark.parametrize("name", UNMATCHABLE_NAMES, ids=repr)
    def test_statement_refused_for_unmatchable_name(self, name):
        with pytest.raises(InvalidFieldNameError):
            (F(name) == 1).sql  # noqa: B018

    @pytest.mark.parametrize("name", LITERAL_NAMES, ids=lambda s: repr(s[:20]))
    def test_statement_text_has_no_bare_quote(self, name):
        sql = (F(name) == 1).sql
        literal = sql.split("JSON_EXTRACT(data, ", 1)[1].rsplit(") = ?", 1)[0]
        assert literal.startswith("'") and literal.endswith("'")
        assert "'" not in literal[1:-1].replace("''", "")

    def test_ordinary_names_keep_their_sql(self):
        assert (F("a") == 1).sql == "JSON_EXTRACT(data, '$.a') = ?"
        assert (F(["a", "b", 1, "c"]) == 1).sql == "JSON_EXTRACT(data, '$.a.b[1].c') = ?"
        assert (F("a-b") == 1).sql == "JSON_EXTRACT(data, '$.\"a-b\"') = ?"
        assert (F("a b") == 1).sql == "JSON_EXTRACT(data, '$.\"a b\"') = ?"
        assert (F("a.b") == 1).sql == "JSON_EXTRACT(data, '$.\"a.b\"') = ?"
        assert (F("arr").any()["val"] == 1).sql == (
            "EXISTS (SELECT 1 FROM json_each(data, '$.arr') AS a "
            "WHERE json_extract(a.value, '$.val') = ?)"
        )
        assert (F("l1").any()["arr2"].any()["f"] > 1).sql == (
            "EXISTS (SELECT 1 FROM json_each(data, '$.l1') AS a "
            "JOIN json_each(a.value, '$.arr2') AS b "
            "WHERE json_extract(b.value, '$.f') > ?)"
        )

    def test_expression_index_still_used(self, jdb):
        jdb.insert_document("docs", {"a-b": 1})
        jdb.adapter.execute("CREATE INDEX ix ON docs (JSON_EXTRACT(data, '$.\"a-b\"'))")
        plan = jdb.query("docs").filter(F("a-b") == 1).explain_query_plan(jdb.adapter)
        assert any("ix" in str(tuple(r)) for r in plan)


class TestValidatorsRefuseTrailingNewline:
    @pytest.mark.parametrize("name", TRAILING_NEWLINE_NAMES, ids=repr)
    def test_table_name(self, name):
        with pytest.raises(InvalidTableNameError):
            validate_table_name(name)

    @pytest.mark.parametrize("name", TRAILING_NEWLINE_NAMES, ids=repr)
    def test_identifier(self, name):
        with pytest.raises(InvalidIdentifierError):
            validate_identifier(name)

    @pytest.mark.parametrize("name", TRAILING_NEWLINE_NAMES, ids=repr)
    def test_column_name(self, name):
        with pytest.raises(InvalidIdentifierError):
            validate_column_name(name)

    @pytest.mark.parametrize("name", TRAILING_NEWLINE_NAMES + ["a.b\n", "a\n.b"], ids=repr)
    def test_field_name(self, name):
        with pytest.raises(InvalidFieldNameError):
            validate_field_name(name)

    @pytest.mark.parametrize("name", ["users", "_a1", "A"])
    def test_valid_names_stay_valid(self, name):
        assert validate_table_name(name) == name
        assert validate_identifier(name) == name
        assert validate_field_name(name + ".b_2") == name + ".b_2"


# ---------------------------------------------------------------------------
# Model fields, search index names and integrity checks
# ---------------------------------------------------------------------------

from sqler.exceptions import InvalidTableNameError as _InvalidTableNameError  # noqa: E402
from sqler.models import SQLerModel as _SQLerModel  # noqa: E402
from sqler.models import SQLerModelField as _MF  # noqa: E402
from sqler.models import integrity as _integrity  # noqa: E402

_CARRIABLE_KEYS = ["a'b", 'a"b', "a.b", "a[0]", "x') OR 1=1 --"]
_UNCARRIABLE_MF_KEYS = ["a\n", "a\x00b", "", "a\\b"]


class _MFOwner(_SQLerModel):
    name: str = ""


@pytest.fixture
def mfdb():
    d = SQLerDB.in_memory(shared=False)
    _MFOwner.set_db(d, table="mfowners")
    yield d
    d.close()


def _mf_owners(db, expr):
    cur = db.adapter.execute(
        f"SELECT _id FROM mfowners WHERE {expr.sql} ORDER BY _id", list(expr.params)
    )
    return [r[0] for r in cur.fetchall()]


class TestModelFieldSearchAndIntegrityNames:
    @pytest.mark.parametrize("key", _CARRIABLE_KEYS, ids=repr)
    @pytest.mark.parametrize("position", ["last", "middle"])
    @pytest.mark.parametrize("any_", [False, True])
    def test_model_field_matches_only_the_document_with_that_key(
        self, mfdb, key, position, any_
    ):
        if position == "last":
            hit, miss, rest = {key: 5}, {"zzz": 5}, [key]
        else:
            hit, miss, rest = {key: {"v": 5}}, {"zzz": {"v": 5}}, [key, "v"]
        a_hit = mfdb.insert_document("addresses", hit)
        a_miss = mfdb.insert_document("addresses", miss)
        # a dotted or bracketed key must not match the nested / array document either
        decoy = None
        if "." in key:
            head, tail = key.split(".", 1)
            decoy = {head: {tail: 5}} if position == "last" else {head: {tail: {"v": 5}}}
        elif key == "a[0]":
            decoy = {"a": [5]} if position == "last" else {"a": [{"v": 5}]}
        a_decoy = mfdb.insert_document("addresses", decoy) if decoy is not None else None
        ref = lambda i: {"_table": "addresses", "_id": i}  # noqa: E731
        wrap = (lambda i: [ref(i)]) if any_ else ref
        u_hit = mfdb.insert_document("mfowners", {"addresses": wrap(a_hit)})
        mfdb.insert_document("mfowners", {"addresses": wrap(a_miss)})
        mfdb.insert_document("mfowners", {"addresses": wrap(a_miss + 1)})
        if a_decoy is not None:
            mfdb.insert_document("mfowners", {"addresses": wrap(a_decoy)})
        field = _MF(_MFOwner, ["addresses"] + rest, array_any=any_)
        assert _mf_owners(mfdb, field == 5) == [u_hit]

    @pytest.mark.parametrize("key", _UNCARRIABLE_MF_KEYS, ids=repr)
    @pytest.mark.parametrize("position", ["last", "middle"])
    def test_model_field_refuses_uncarriable_key_before_sql(self, mfdb, key, position):
        rest = [key] if position == "last" else [key, "v"]
        with pytest.raises(InvalidFieldNameError):
            (_MF(_MFOwner, ["addresses"] + rest) == 1).sql  # noqa: B018

    @pytest.mark.parametrize("key", _CARRIABLE_KEYS + _UNCARRIABLE_MF_KEYS, ids=repr)
    @pytest.mark.parametrize("any_", [False, True])
    def test_model_field_first_segment_that_is_no_table_name_is_refused(self, mfdb, key, any_):
        with pytest.raises((InvalidFieldNameError, _InvalidTableNameError)):
            (_MF(_MFOwner, [key, "v"], array_any=any_) == 1).sql  # noqa: B018

    @pytest.mark.parametrize("any_", [False, True])
    def test_model_field_statement_has_no_bare_quote(self, mfdb, any_):
        sql = (_MF(_MFOwner, ["addresses", "a'b", "c'd"], array_any=any_) == 1).sql
        assert "a'b" not in sql and "c'd" not in sql

    def test_model_field_plain_names_still_work(self, mfdb):
        a = mfdb.insert_document("addresses", {"city": "Kyoto", "geo": {"lat": 3}})
        u = mfdb.insert_document("mfowners", {"addresses": {"_table": "addresses", "_id": a}})
        assert _mf_owners(mfdb, _MF(_MFOwner, ["addresses", "city"]) == "Kyoto") == [u]
        assert _mf_owners(mfdb, _MF(_MFOwner, ["addresses", "geo", "lat"]) == 3) == [u]
        assert _mf_owners(mfdb, _MF(_MFOwner, ["addresses", "_id"]) == a) == [u]

    # -- search index ------------------------------------------------------

    @pytest.mark.parametrize("name", ["x; DROP TABLE users; --", "docs_fts\n", "a b", ""], ids=repr)
    def test_index_table_set_after_construction_is_checked(self, name):
        class _Doc(_SQLerModel):
            body: str = ""

        idx = FTSIndex(_Doc, ["body"], index_name="ok_fts")
        with pytest.raises(InvalidIdentifierError):
            idx.index_table = name
        assert idx.index_table == "ok_fts"

    def test_index_table_can_still_be_reset_to_none_and_valid_names(self):
        class _Doc2(_SQLerModel):
            body: str = ""

        idx = FTSIndex(_Doc2, ["body"], index_name="ok_fts")
        idx.index_table = "other_fts"
        assert idx.index_table == "other_fts"
        idx.index_table = None
        assert idx.index_table.endswith("_fts")

    @pytest.mark.parametrize("name", ["x; DROP TABLE users; --", "docs_fts\n"], ids=repr)
    def test_no_fts_method_runs_sql_for_a_bad_index_table(self, name):
        class _Doc3(_SQLerModel):
            body: str = ""

        d = SQLerDB.in_memory(shared=False)
        _Doc3.set_db(d, table="doc3s")
        d.insert_document("users", {"name": "u"})
        idx = FTSIndex(_Doc3, ["body"])
        with pytest.raises(InvalidIdentifierError):
            idx.index_table = name
        idx.create(d)
        assert d.adapter.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        d.close()

    @pytest.mark.parametrize("tok", ["porter\n", "porter; --", "porter\n\n", "x'y"], ids=repr)
    def test_tokenizer_with_trailing_newline_or_junk_is_refused(self, tok):
        class _Doc4(_SQLerModel):
            body: str = ""

        with pytest.raises(ValueError):
            FTSIndex(_Doc4, ["body"], tokenizer=tok)

    # -- integrity ---------------------------------------------------------

    def test_integrity_has_no_pattern_of_its_own(self):
        import inspect

        assert "a-zA-Z" not in inspect.getsource(_integrity)

    @pytest.mark.parametrize("bad", ["users\n", "users; --", "a b"], ids=repr)
    def test_validate_references_reports_bad_table_name_as_broken(self, bad):
        d = SQLerDB.in_memory(shared=False)
        _MFOwner.set_db(d, table="mfowners")
        d.insert_document("mfowners", {"r": {"_table": bad, "_id": 1}})
        broken = _integrity.validate_references(d)
        assert [(b.table, b.target_table) for b in broken] == [("mfowners", bad)]
        d.close()

    def test_validate_references_trailing_newline_name_is_not_sent_to_sql(self):
        d = SQLerDB.in_memory(shared=False)
        _MFOwner.set_db(d, table="mfowners")
        d.insert_document("users", {"name": "u"})
        d.insert_document("mfowners", {"r": {"_table": "users\n", "_id": 1}})
        broken = _integrity.validate_references(d)
        assert [b.target_table for b in broken] == ["users\n"]
        d.close()


class TestFtsFieldsSetAfterConstruction:
    @pytest.mark.parametrize("name", ["x'); DROP TABLE users; --", "body\n", ""], ids=repr)
    def test_fields_set_after_construction_are_checked(self, name):
        class _Doc5(_SQLerModel):
            body: str = ""

        idx = FTSIndex(_Doc5, ["body"])
        with pytest.raises(InvalidFieldNameError):
            idx.fields = ["body", name]
        assert idx.fields == ["body"]


# ---------------------------------------------------------------------------
# Review fixes: values that still reach SQL in the search index and integrity
# ---------------------------------------------------------------------------

_EVIL_FIELD = "x'); DROP TABLE users; --"
_EVIL_TOKENIZER = "x'); DROP TABLE users; --"


def _fts_fixture(table="rv_docs"):
    class _RvDoc(_SQLerModel):
        body: str = ""

    d = SQLerDB.in_memory(shared=False)
    _RvDoc.set_db(d, table=table)
    d.insert_document("users", {"name": "u"})
    return _RvDoc, d


def _users_intact(d):
    return d.adapter.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


class TestFtsAndIntegrityReviewFixes:
    def test_fields_returned_list_cannot_change_the_index(self):
        doc, d = _fts_fixture()
        idx = FTSIndex(doc, ["body"])
        idx.fields.append(_EVIL_FIELD)
        assert idx.fields == ["body"]
        idx.create(d)
        idx.rebuild()
        assert _users_intact(d)
        d.close()

    def test_constructor_list_mutation_cannot_change_the_index(self):
        doc, d = _fts_fixture()
        given = ["body"]
        idx = FTSIndex(doc, given)
        given.append(_EVIL_FIELD)
        assert idx.fields == ["body"]
        idx.create(d)
        idx.rebuild()
        assert idx.stats().fields == ["body"]
        assert _users_intact(d)
        d.close()

    @pytest.mark.parametrize("tok", [_EVIL_TOKENIZER, "porter\n", "porter; --"], ids=repr)
    def test_tokenizer_set_after_construction_is_checked(self, tok):
        doc, d = _fts_fixture()
        idx = FTSIndex(doc, ["body"])
        with pytest.raises(ValueError, match="Invalid tokenizer"):
            idx.tokenizer = tok
        assert idx.tokenizer == "porter unicode61"
        d.close()

    @pytest.mark.parametrize("bad", [True, 1.5, "3", "1) FROM users; --"], ids=repr)
    def test_snippet_max_tokens_must_be_an_integer(self, bad):
        doc, d = _fts_fixture()
        idx = FTSIndex(doc, ["body"])
        idx.create(d)
        m = doc(body="hello world")
        m.save()
        with pytest.raises(TypeError, match="max_tokens"):
            idx.snippet("hello", m, max_tokens=bad)
        assert idx.snippet("hello", m, max_tokens=5) == "<b>hello</b> world"
        assert _users_intact(d)
        d.close()

    @pytest.mark.parametrize("name", ["x; DROP TABLE users; --", "docs\n", "a b"], ids=repr)
    def test_model_table_set_by_hand_is_checked(self, name):
        class _Hand(_SQLerModel):
            body: str = ""

        _Hand._table = name
        idx = FTSIndex(_Hand, ["body"])
        with pytest.raises(InvalidTableNameError):
            idx.table  # noqa: B018
        with pytest.raises(InvalidTableNameError):
            idx.index_table  # noqa: B018

    @pytest.mark.parametrize("fn", ["set_null", "cascade"])
    @pytest.mark.parametrize("bad", ["users\n", "users; --", "a b", ""], ids=repr)
    def test_integrity_helpers_refuse_bad_referrer_table(self, fn, bad):
        d = SQLerDB.in_memory(shared=False)
        d.insert_document("users", {"name": "u"})
        referrers = [("users", 1, {}), (bad, 1, {})]
        with pytest.raises(_InvalidTableNameError):
            if fn == "set_null":
                _integrity.set_null_referrers(d, "users", 1, referrers)
            else:
                _integrity.cascade_delete(d, referrers, set())
        # refused before any SQL ran: the valid first entry was not touched either
        assert d.adapter.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        d.close()


# ---------------------------------------------------------------------------
# Database write methods with promoted columns check table and field names
# ---------------------------------------------------------------------------

from sqler.exceptions import InvalidIdentifierError as _InvalidIdentifierError  # noqa: E402

_PROMOTED_WRITES = {
    "upsert_document_promoted": lambda d, t, f: d.upsert_document_promoted(t, None, {"s": "a"}, f),
    "upsert_document_promoted_update": lambda d, t, f: d.upsert_document_promoted(t, 1, {"s": "a"}, f),
    "insert_many_promoted": lambda d, t, f: d.insert_many_promoted(t, [{"s": "a"}], f),
    "upsert_with_version_promoted": lambda d, t, f: d.upsert_with_version_promoted(t, None, {"s": "a"}, None, f),
    "upsert_with_version_promoted_update": lambda d, t, f: d.upsert_with_version_promoted(t, 1, {"s": "a"}, 0, f),
}


class TestPromotedWriteNames:
    @pytest.fixture
    def pdb(self):
        d = SQLerDB.in_memory(shared=False)
        d.ensure_table("jobs", promoted={"s": "TEXT"}, versioned=True)
        d.upsert_with_version_promoted("jobs", None, {"s": "first"}, None, ["s"])
        yield d
        d.close()

    def _untouched(self, d):
        rows = d.adapter.execute("SELECT _id, s, _version FROM jobs").fetchall()
        tables = d.adapter.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return [tuple(r) for r in rows], [r[0] for r in tables]

    @pytest.mark.parametrize("method", list(_PROMOTED_WRITES))
    def test_table_ending_in_newline_is_refused(self, pdb, method):
        before = self._untouched(pdb)
        with pytest.raises(InvalidTableNameError):
            _PROMOTED_WRITES[method](pdb, "jobs\n", ["s"])
        assert self._untouched(pdb) == before == ([(1, "first", 0)], ["_sqler_promoted", "jobs", "sqlite_sequence"])

    @pytest.mark.parametrize("method", list(_PROMOTED_WRITES))
    def test_hostile_promoted_field_is_refused(self, pdb, method):
        before = self._untouched(pdb)
        with pytest.raises(_InvalidIdentifierError):
            _PROMOTED_WRITES[method](pdb, "jobs", ["s", 'x"; DROP TABLE t; --'])
        assert self._untouched(pdb) == before

    @pytest.mark.parametrize("method", list(_PROMOTED_WRITES))
    def test_valid_names_still_work(self, pdb, method):
        _PROMOTED_WRITES[method](pdb, "jobs", ["s"])
        assert pdb.adapter.execute("SELECT s FROM jobs WHERE s = 'a'").fetchone()[0] == "a"


class TestTableNameValidation:
    """Tests for shared table name validation utility."""

    def test_valid_table_names(self):
        """Test that valid table names pass validation."""
        assert validate_table_name("users") == "users"
        assert validate_table_name("User_data") == "User_data"
        assert validate_table_name("_private") == "_private"
        assert validate_table_name("table123") == "table123"

    def test_invalid_table_names(self):
        """Test that invalid table names raise InvalidTableNameError."""
        with pytest.raises(InvalidTableNameError):
            validate_table_name("123starts_with_number")

        with pytest.raises(InvalidTableNameError):
            validate_table_name("has-dashes")

        with pytest.raises(InvalidTableNameError):
            validate_table_name("has spaces")

        with pytest.raises(InvalidTableNameError):
            validate_table_name("")
