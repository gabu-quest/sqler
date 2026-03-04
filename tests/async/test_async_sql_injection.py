"""Adversarial tests for SQL injection prevention (async adapter/query/db)."""

import pytest
import pytest_asyncio
from sqler.db.async_db import AsyncSQLerDB
from sqler.exceptions import InvalidFieldNameError, InvalidIdentifierError

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
