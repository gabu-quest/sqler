"""Adversarial tests for SQL injection prevention (async adapter/query/db)."""

import pytest
import pytest_asyncio

from sqler.db.async_db import AsyncSQLerDB
from sqler.exceptions import InvalidFieldNameError, InvalidIdentifierError
from sqler.query.async_query import AsyncSQLerQuery

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
        with pytest.raises(InvalidFieldNameError):
            aquery.order_by(evil)


class TestAsyncAggregateRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    @pytest.mark.asyncio
    async def test_sum_rejects(self, aquery, evil):
        with pytest.raises(InvalidFieldNameError):
            await aquery.sum(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    @pytest.mark.asyncio
    async def test_avg_rejects(self, aquery, evil):
        with pytest.raises(InvalidFieldNameError):
            await aquery.avg(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    @pytest.mark.asyncio
    async def test_min_rejects(self, aquery, evil):
        with pytest.raises(InvalidFieldNameError):
            await aquery.min(evil)

    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    @pytest.mark.asyncio
    async def test_max_rejects(self, aquery, evil):
        with pytest.raises(InvalidFieldNameError):
            await aquery.max(evil)


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


class TestAsyncCreateIndexRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_FIELDS)
    @pytest.mark.asyncio
    async def test_field_injection(self, adb, evil):
        with pytest.raises((InvalidFieldNameError, InvalidIdentifierError)):
            await adb.create_index("items", evil)

    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    @pytest.mark.asyncio
    async def test_name_injection(self, adb, evil):
        with pytest.raises(InvalidIdentifierError):
            await adb.create_index("items", "name", name=evil)


class TestAsyncPromotedColumnRejectsInjection:
    @pytest.mark.parametrize("evil", EVIL_IDENTIFIERS)
    @pytest.mark.asyncio
    async def test_rejects(self, adb, evil):
        with pytest.raises(InvalidIdentifierError):
            await adb._ensure_table_with_promoted("items", {evil: "TEXT"})
