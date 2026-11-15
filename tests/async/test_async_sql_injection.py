"""Adversarial tests for SQL injection prevention (async adapter/query/db)."""

import pytest
import pytest_asyncio
from sqler.db.async_db import AsyncSQLerDB
from sqler.exceptions import (
    InvalidColumnDefError,
    InvalidFieldNameError,
    InvalidIdentifierError,
)

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

EVIL_UNDERSCORE_FIELDS = [
    "_; DROP TABLE items",
    "_ UNION SELECT",
    "_has space",
    "_semi;colon",
]


@pytest_asyncio.fixture
async def adb():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    await db.insert_document("items", {"name": "a", "score": 10})
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def aquery(adb):
    return await adb.query("items")


class TestAsyncOrderByRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    @pytest.mark.asyncio
    async def test_rejects(self, aquery, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            aquery.order_by(evil)


class TestAsyncAggregateRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    @pytest.mark.asyncio
    async def test_sum_rejects(self, aquery, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            await aquery.sum(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    @pytest.mark.asyncio
    async def test_avg_rejects(self, aquery, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            await aquery.avg(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    @pytest.mark.asyncio
    async def test_min_rejects(self, aquery, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            await aquery.min(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    @pytest.mark.asyncio
    async def test_max_rejects(self, aquery, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            await aquery.max(evil)


class TestAsyncDistinctValuesRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    @pytest.mark.asyncio
    async def test_rejects(self, aquery, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name|non-empty string"):
            await aquery.distinct_values(evil)


class TestAsyncUpdateRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS_KWARG)
    @pytest.mark.asyncio
    async def test_update_rejects(self, aquery, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name"):
            await aquery.update(**{evil: "val"})

    @pytest.mark.parametrize("evil", EVIL_FIELDS_KWARG)
    @pytest.mark.asyncio
    async def test_update_one_rejects(self, aquery, evil):
        with pytest.raises(InvalidFieldNameError, match="Invalid field name"):
            await aquery.update_one(**{evil: "val"})


class TestAsyncExecuteSqlRestriction:
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
    @pytest.mark.asyncio
    async def test_rejects_mutations(self, adb, sql):
        with pytest.raises(ValueError, match="read-only"):
            await adb.execute_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1; DROP TABLE items",
            "SELECT 1; DELETE FROM items",
            "SELECT 1; INSERT INTO items (data) VALUES ('{}')",
        ],
    )
    @pytest.mark.asyncio
    async def test_rejects_multi_statement(self, adb, sql):
        with pytest.raises(ValueError, match="multi-statement"):
            await adb.execute_sql(sql)

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
    @pytest.mark.asyncio
    async def test_allows_reads(self, adb, sql):
        await adb.execute_sql(sql)


class TestAsyncDropIndexRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    @pytest.mark.asyncio
    async def test_rejects(self, adb, evil):
        with pytest.raises(InvalidIdentifierError, match="Invalid identifier|non-empty string"):
            await adb.drop_index(evil)


class TestAsyncCreateIndexRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    @pytest.mark.asyncio
    async def test_field_injection(self, adb, evil):
        with pytest.raises(
            (InvalidFieldNameError, InvalidIdentifierError),
            match="Invalid field name|Invalid identifier|non-empty string",
        ):
            await adb.create_index("items", evil)

    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    @pytest.mark.asyncio
    async def test_name_injection(self, adb, evil):
        with pytest.raises(InvalidIdentifierError, match="Invalid identifier|non-empty string"):
            await adb.create_index("items", "name", name=evil)

    @pytest.mark.parametrize("evil", EVIL_UNDERSCORE_FIELDS)
    @pytest.mark.asyncio
    async def test_underscore_prefixed_field_injection(self, adb, evil):
        """Underscore-prefixed fields take the validate_identifier branch."""
        with pytest.raises(InvalidIdentifierError, match="Invalid identifier"):
            await adb.create_index("items", evil)


class TestAsyncPromotedColumnRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    @pytest.mark.asyncio
    async def test_rejects(self, adb, evil):
        with pytest.raises(InvalidIdentifierError, match="Invalid identifier|non-empty string"):
            await adb._ensure_table_with_promoted("items", {evil: "TEXT"})


class TestAsyncPromotedColumnDefRejectsInjection:
    """H-3 (async): _ensure_table_with_promoted must validate col_def strings."""

    @pytest.mark.parametrize(
        "evil_def",
        [
            "TEXT; DROP TABLE items",
            "TEXT DEFAULT (SELECT 1)",
            "TEXT/* injected */",
            "TEXT -- comment",
            "VARCHAR(10)",
            "",
        ],
    )
    @pytest.mark.asyncio
    async def test_rejects(self, adb, evil_def):
        with pytest.raises(
            InvalidColumnDefError,
            match="Invalid column definition|non-empty string",
        ):
            await adb._ensure_table_with_promoted("items", {"status": evil_def})


class TestAsyncPromotedChecksRejectInjection:
    @pytest.mark.asyncio
    async def test_rejects_semicolon(self, adb):
        with pytest.raises(InvalidColumnDefError, match="may not contain ';'"):
            await adb._ensure_table_with_promoted(
                "items",
                {"status": "TEXT"},
                checks={"evil": "status = 'x'; DROP TABLE items"},
            )

    @pytest.mark.asyncio
    async def test_rejects_keyword(self, adb):
        with pytest.raises(InvalidColumnDefError, match="may not contain keyword"):
            await adb._ensure_table_with_promoted(
                "items",
                {"status": "TEXT"},
                checks={"evil": "status = 'x' OR DROP TABLE items"},
            )


class TestAsyncCreateIndexWhereRejectsInjection:
    @pytest.mark.parametrize(
        "evil_where",
        [
            "1=1); DROP TABLE items; --",
            "1=1 -- trick",
            "1=1 /* trick */",
            "1=1 OR DROP TABLE items",
            "1=1 OR PRAGMA writable_schema=ON",
        ],
    )
    @pytest.mark.asyncio
    async def test_rejects_evil_where(self, adb, evil_where):
        with pytest.raises(
            InvalidColumnDefError,
            match="may not contain|non-empty string",
        ):
            await adb.create_index("items", "name", where=evil_where)

    @pytest.mark.asyncio
    async def test_accepts_legit_partial_where(self, adb):
        await adb.create_index(
            "items",
            "name",
            where="json_extract(data,'$.name') IS NOT NULL",
        )


# ---------------------------------------------------------------------------
# names stored in documents / used as JSON keys never change the statement
# ---------------------------------------------------------------------------

from typing import Union  # noqa: E402

from pydantic import Field as _PydField  # noqa: E402
from sqler import AsyncSQLerModel  # noqa: E402
from sqler.logging import query_logger  # noqa: E402
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


class AHolderUser(AsyncSQLerModel):
    __tablename__ = "hr_users"
    name: str = ""


# a typed position, so that what is refused here is refused by the walk, not by the rule
# that an untyped field is never followed
class AHolder(AsyncSQLerModel):
    payload: dict[str, Union[dict, AHolderUser]] = _PydField(default_factory=dict)


@pytest_asyncio.fixture
async def rdb():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    AHolderUser.set_db(db)
    AHolder.set_db(db)
    await db.insert_document("secrets", {"secret": "s3"})
    await db.insert_document("hr_users", {"name": "u1"})
    try:
        yield db
    finally:
        AHolder.set_db(None)
        await db.close()


class TestAsyncLoaderRejectsHostileRefs:
    # a reference the loader refuses leads to no row of the field's class: it is left out of
    # the dict, and no statement is sent for it: every statement names the holder's table

    @staticmethod
    async def _load(rdb, payload):
        rid = await rdb.insert_document(AHolder.__tablename__, {"payload": payload})
        with query_logger.capture() as sent:
            got = await AHolder.from_id(rid)
        return got.payload, sum(AHolder.__tablename__ not in q.sql for q in sent)

    @pytest.mark.parametrize("table", HOSTILE_TABLES)
    @pytest.mark.asyncio
    async def test_hostile_table_left_out(self, rdb, table):
        assert await self._load(rdb, {"k": {"_table": table, "_id": 1}}) == ({}, 0)

    @pytest.mark.parametrize("bad", BAD_IDS, ids=repr)
    @pytest.mark.asyncio
    async def test_bad_id_left_out(self, rdb, bad):
        assert await self._load(rdb, {"k": {"_table": "hr_users", "_id": bad}}) == ({}, 0)

    @pytest.mark.parametrize("edge", EDGE_IDS, ids=repr)
    @pytest.mark.asyncio
    async def test_edge_id_without_row_left_out(self, rdb, edge):
        assert (await self._load(rdb, {"k": {"_table": "hr_users", "_id": edge}}))[0] == {}

    @pytest.mark.parametrize("table", ["hr_users\n", "hr_users\r\n", "\nhr_users"], ids=repr)
    @pytest.mark.asyncio
    async def test_newline_table_left_out(self, rdb, table):
        assert await self._load(rdb, {"k": {"_table": table, "_id": 1}}) == ({}, 0)

    @pytest.mark.asyncio
    async def test_bad_ref_does_not_stop_good_ref(self, rdb):
        bad = {"_table": "hr_users; x", "_id": 1}
        good = {"_table": "hr_users", "_id": 1}
        payload, _ = await self._load(rdb, {"a": bad, "b": good})
        assert list(payload) == ["b"]
        assert payload["b"].name == "u1"

    @pytest.mark.asyncio
    async def test_end_to_end_all(self, rdb):
        ref = {"_table": "secrets WHERE 1 UNION SELECT _id, data FROM secrets", "_id": 1}
        await AHolder(payload={"k": ref}).save()
        got = await AHolder.query().all()
        assert got[0].payload == {}


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
    "two words",
]
UNMATCHABLE_NAMES = ['a\\"b', "a\\'b", "a\\b", "a\x00b", "a\nb", "a\tb", "a b", "a  b"]


@pytest_asyncio.fixture
async def jdb():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


async def _ids(db, expr):
    q = (await db.query("docs")).filter(expr)
    return sorted(r["_id"] for r in await q.all_dicts())


class TestAsyncJsonPathLiteralEscaping:
    @pytest.mark.parametrize("name", LITERAL_NAMES, ids=lambda s: repr(s[:20]))
    @pytest.mark.asyncio
    async def test_field_matches_key_literally(self, jdb, name):
        a = await jdb.insert_document("docs", {name: 1})
        await jdb.insert_document("docs", {"plain": 1})
        assert await _ids(jdb, F(name) == 1) == [a]

    @pytest.mark.parametrize("name", LITERAL_NAMES, ids=lambda s: repr(s[:20]))
    @pytest.mark.asyncio
    async def test_any_last_field(self, jdb, name):
        a = await jdb.insert_document("docs", {"arr": [{name: 1}]})
        await jdb.insert_document("docs", {"arr": [{"plain": 1}]})
        assert await _ids(jdb, F("arr").any()[name] == 1) == [a]

    @pytest.mark.parametrize("name", LITERAL_NAMES, ids=lambda s: repr(s[:20]))
    @pytest.mark.asyncio
    async def test_any_nested_array_key(self, jdb, name):
        a = await jdb.insert_document("docs", {"o": [{name: [{"v": 1}]}]})
        await jdb.insert_document("docs", {"o": [{"plain": [{"v": 1}]}]})
        assert await _ids(jdb, F("o").any()[name].any()["v"] == 1) == [a]

    @pytest.mark.parametrize("name", UNMATCHABLE_NAMES, ids=repr)
    @pytest.mark.asyncio
    async def test_unrepresentable_name_raises_sqler_error(self, jdb, name):
        await jdb.insert_document("docs", {"plain": 1})
        with pytest.raises(InvalidFieldNameError):
            await _ids(jdb, F(name) == 1)
        with pytest.raises(InvalidFieldNameError):
            await _ids(jdb, F("arr").any()[name] == 1)
        with pytest.raises(InvalidFieldNameError):
            await _ids(jdb, F(name).any()["v"] == 1)


class TestAsyncStatementText:
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


# ---------------------------------------------------------------------------
# Model fields and integrity checks (async database)
# ---------------------------------------------------------------------------

from sqler.exceptions import InvalidTableNameError as _InvalidTableNameError  # noqa: E402
from sqler.models import AsyncSQLerModel as _AsyncModel  # noqa: E402
from sqler.models import SQLerModelField as _MF  # noqa: E402
from sqler.models import async_integrity as _async_integrity  # noqa: E402

_ACARRIABLE_KEYS = ["a'b", 'a"b', "a.b", "a[0]", "x') OR 1=1 --"]
_AUNCARRIABLE_KEYS = ["a\n", "a\x00b", "", "a\\b"]


class _AMFOwner(_AsyncModel):
    name: str = ""


@pytest_asyncio.fixture
async def amfdb():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    _AMFOwner.set_db(db, table="mfowners")
    try:
        yield db
    finally:
        await db.close()


class TestAsyncModelFieldAndIntegrityNames:
    @pytest.mark.parametrize("key", _ACARRIABLE_KEYS, ids=repr)
    @pytest.mark.parametrize("position", ["last", "middle"])
    @pytest.mark.parametrize("any_", [False, True])
    @pytest.mark.asyncio
    async def test_model_field_matches_only_the_document_with_that_key(
        self, amfdb, key, position, any_
    ):
        if position == "last":
            hit, miss, rest = {key: 5}, {"zzz": 5}, [key]
        else:
            hit, miss, rest = {key: {"v": 5}}, {"zzz": {"v": 5}}, [key, "v"]
        a_hit = await amfdb.insert_document("addresses", hit)
        a_miss = await amfdb.insert_document("addresses", miss)
        decoy = None
        if "." in key:
            head, tail = key.split(".", 1)
            decoy = {head: {tail: 5}} if position == "last" else {head: {tail: {"v": 5}}}
        elif key == "a[0]":
            decoy = {"a": [5]} if position == "last" else {"a": [{"v": 5}]}
        a_decoy = await amfdb.insert_document("addresses", decoy) if decoy is not None else None
        wrap = (lambda i: [{"_table": "addresses", "_id": i}]) if any_ else (
            lambda i: {"_table": "addresses", "_id": i}
        )
        u_hit = await amfdb.insert_document("mfowners", {"addresses": wrap(a_hit)})
        await amfdb.insert_document("mfowners", {"addresses": wrap(a_miss)})
        if a_decoy is not None:
            await amfdb.insert_document("mfowners", {"addresses": wrap(a_decoy)})
        expr = _MF(_AMFOwner, ["addresses"] + rest, array_any=any_) == 5
        cur = await amfdb.adapter.execute(
            f"SELECT _id FROM mfowners WHERE {expr.sql} ORDER BY _id", list(expr.params)
        )
        assert [r[0] for r in await cur.fetchall()] == [u_hit]

    @pytest.mark.parametrize("key", _AUNCARRIABLE_KEYS, ids=repr)
    @pytest.mark.parametrize("position", ["last", "middle"])
    def test_model_field_refuses_uncarriable_key_before_sql(self, key, position):
        rest = [key] if position == "last" else [key, "v"]
        with pytest.raises(InvalidFieldNameError):
            (_MF(_AMFOwner, ["addresses"] + rest) == 1).sql  # noqa: B018

    @pytest.mark.parametrize("key", _ACARRIABLE_KEYS + _AUNCARRIABLE_KEYS, ids=repr)
    def test_model_field_first_segment_that_is_no_table_name_is_refused(self, key):
        with pytest.raises((InvalidFieldNameError, _InvalidTableNameError)):
            (_MF(_AMFOwner, [key, "v"]) == 1).sql  # noqa: B018

    def test_integrity_has_no_pattern_of_its_own(self):
        import inspect

        assert "a-zA-Z" not in inspect.getsource(_async_integrity)

    @pytest.mark.parametrize("bad", ["users\n", "users; --", "a b"], ids=repr)
    @pytest.mark.asyncio
    async def test_validate_references_reports_bad_table_name_as_broken(self, amfdb, bad):
        await amfdb.insert_document("users", {"name": "u"})
        await amfdb.insert_document("mfowners", {"r": {"_table": bad, "_id": 1}})
        broken = await _async_integrity.async_validate_references(amfdb)
        assert [(b.table, b.target_table) for b in broken] == [("mfowners", bad)]


# ---------------------------------------------------------------------------
# Review fixes: integrity helpers refuse referrer table names the owner refuses
# ---------------------------------------------------------------------------


class TestAsyncIntegrityReviewFixes:
    @pytest.mark.parametrize("fn", ["set_null", "cascade"])
    @pytest.mark.parametrize("bad", ["users\n", "users; --", "a b", ""], ids=repr)
    @pytest.mark.asyncio
    async def test_integrity_helpers_refuse_bad_referrer_table(self, amfdb, fn, bad):
        await amfdb.insert_document("users", {"name": "u"})
        referrers = [("users", 1, {}), (bad, 1, {})]
        with pytest.raises(_InvalidTableNameError):
            if fn == "set_null":
                await _async_integrity.async_set_null_referrers(amfdb, "users", 1, referrers)
            else:
                await _async_integrity.async_cascade_delete(amfdb, referrers, set())
        cur = await amfdb.adapter.execute("SELECT COUNT(*) FROM users")
        assert (await cur.fetchone())[0] == 1


# ---------------------------------------------------------------------------
# Database write methods with promoted columns check table and field names
# ---------------------------------------------------------------------------

from sqler.exceptions import InvalidIdentifierError as _InvalidIdentifierError  # noqa: E402

_ASYNC_PROMOTED_WRITES = {
    "upsert_document_promoted": lambda d, t, f: d.upsert_document_promoted(t, None, {"s": "a"}, f),
    "upsert_document_promoted_update": lambda d, t, f: d.upsert_document_promoted(t, 1, {"s": "a"}, f),
    "insert_many_promoted": lambda d, t, f: d.insert_many_promoted(t, [{"s": "a"}], f),
    "upsert_with_version_promoted": lambda d, t, f: d.upsert_with_version_promoted(t, None, {"s": "a"}, None, f),
    "upsert_with_version_promoted_update": lambda d, t, f: d.upsert_with_version_promoted(t, 1, {"s": "a"}, 0, f),
}


class TestAsyncPromotedWriteNames:
    @pytest_asyncio.fixture
    async def pdb(self):
        d = AsyncSQLerDB.in_memory(shared=False)
        await d.connect()
        await d.ensure_table("jobs", promoted={"s": "TEXT"}, versioned=True)
        await d.upsert_with_version_promoted("jobs", None, {"s": "first"}, None, ["s"])
        yield d
        await d.close()

    async def _state(self, d):
        out = []
        for sql in (
            "SELECT _id, s, _version FROM jobs",
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        ):
            cur = await d.adapter.execute(sql)
            out.append([tuple(r) for r in await cur.fetchall()])
            await cur.close()
        await d.adapter.auto_commit()
        return out

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", list(_ASYNC_PROMOTED_WRITES))
    async def test_table_ending_in_newline_is_refused(self, pdb, method):
        before = await self._state(pdb)
        with pytest.raises(_InvalidTableNameError):
            await _ASYNC_PROMOTED_WRITES[method](pdb, "jobs\n", ["s"])
        assert await self._state(pdb) == before == [[(1, "first", 0)], [("_sqler_promoted",), ("jobs",), ("sqlite_sequence",)]]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", list(_ASYNC_PROMOTED_WRITES))
    async def test_hostile_promoted_field_is_refused(self, pdb, method):
        before = await self._state(pdb)
        with pytest.raises(_InvalidIdentifierError):
            await _ASYNC_PROMOTED_WRITES[method](pdb, "jobs", ["s", 'x"; DROP TABLE t; --'])
        assert await self._state(pdb) == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", list(_ASYNC_PROMOTED_WRITES))
    async def test_valid_names_still_work(self, pdb, method):
        await _ASYNC_PROMOTED_WRITES[method](pdb, "jobs", ["s"])
        assert (await self._state(pdb))[0][-1][1] == "a"
