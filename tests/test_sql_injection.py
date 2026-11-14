"""Adversarial tests for SQL injection prevention via field/identifier validation."""


import pytest
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
# WS-Q: names stored in documents / used as JSON keys never change the statement
# ---------------------------------------------------------------------------

from pydantic import Field as _PydField  # noqa: E402
from sqler import SQLerDB as _SQLerDB  # noqa: E402
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


class Holder(SQLerModel):
    payload: dict = _PydField(default_factory=dict)


@pytest.fixture
def rdb():
    d = _SQLerDB.in_memory(shared=False)
    Holder.set_db(d)
    d.insert_document("secrets", {"secret": "s3"})
    d.insert_document("users", {"name": "u1"})
    yield d
    d.close()


class TestBatchResolveRejectsHostileRefs:
    @pytest.mark.parametrize("table", HOSTILE_TABLES)
    def test_hostile_table_left_unchanged(self, rdb, table):
        ref = {"_table": table, "_id": 1}
        out = Holder.query()._batch_resolve([{"payload": {"k": ref}, "_id": 1}])
        assert out == [{"payload": {"k": ref}, "_id": 1}]

    @pytest.mark.parametrize("bad", BAD_IDS, ids=repr)
    def test_bad_id_left_unchanged(self, rdb, bad):
        ref = {"_table": "users", "_id": bad}
        out = Holder.query()._batch_resolve([{"payload": {"k": ref}, "_id": 1}])
        assert out == [{"payload": {"k": ref}, "_id": 1}]

    @pytest.mark.parametrize("edge", EDGE_IDS, ids=repr)
    def test_edge_id_without_row_left_unchanged(self, rdb, edge):
        ref = {"_table": "users", "_id": edge}
        out = Holder.query()._batch_resolve([{"payload": {"k": ref}, "_id": 1}])
        assert out == [{"payload": {"k": ref}, "_id": 1}]

    @pytest.mark.parametrize("table", ["users\n", "users\r\n", "\nusers"], ids=repr)
    def test_newline_table_left_unchanged(self, rdb, table):
        ref = {"_table": table, "_id": 1}
        out = Holder.query()._batch_resolve([{"payload": {"k": ref}, "_id": 1}])
        assert out == [{"payload": {"k": ref}, "_id": 1}]

    def test_bad_ref_does_not_stop_good_ref(self, rdb):
        bad = {"_table": "users; x", "_id": 1}
        good = {"_table": "users", "_id": 1}
        out = Holder.query()._batch_resolve([{"payload": {"a": bad, "b": good}}])
        assert out[0]["payload"]["a"] == bad
        assert out[0]["payload"]["b"]["name"] == "u1"

    def test_end_to_end_all(self, rdb):
        ref = {"_table": "secrets WHERE 1 UNION SELECT _id, data FROM secrets", "_id": 1}
        Holder(payload={"k": ref}).save()
        got = Holder.query().all()
        assert got[0].payload == {"k": ref}


# --- JSON path literals -----------------------------------------------------

LITERAL_NAMES = [
    "x') OR 1=1 --",
    "it's",
    'a"b',
    "'; DROP TABLE users; --",
    "",
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
