"""The query cache: ``QueryCache``, ``cached_query`` / ``async_cached_query`` and
``CacheAwareModel``.

A cached result of table T is never kept from rows older than the last write of T: every way
of writing a row of a class with the cache mixin tells the cache that T was written; while a
transaction of this process that wrote T is open the cache neither serves nor stores entries
of T, and at its end (commit or rollback) they are dropped; and a result is stored only if T
was not written while its function ran.
"""

import asyncio
import contextlib
import itertools
import threading
import time
import types
from dataclasses import dataclass
from typing import Optional

import pydantic
import pytest
import pytest_asyncio

from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerLiteSafeModel,
    AsyncSQLerModel,
    AsyncSQLerMsgspecModel,
    AsyncSQLerSafeModel,
    QueryCache,
    SQLerDB,
    SQLerLiteModel,
    SQLerLiteSafeModel,
    SQLerModel,
    SQLerMsgspecModel,
    SQLerSafeModel,
    cached_query,
)
from sqler import cache as cache_module
from sqler.cache import CacheAwareModel, async_cached_query, configure_cache, get_cache
from sqler.exceptions import TransactionStateError
from sqler.logging import query_logger
from sqler.models.async_queryset import AsyncSQLerQuerySet
from sqler.models.mixins import AsyncAuditLogMixin, AuditLogMixin
from sqler.models.queryset import SQLerQuerySet
from sqler.query import F, SQLerQuery
from sqler.tracking import (
    AsyncPartialUpdateMixin,
    AsyncTrackedModel,
    PartialUpdateMixin,
    TrackedModel,
)

# =============================================================================
# QueryCache on its own
# =============================================================================


class TestQueryCache:
    """Tests for QueryCache functionality."""

    def test_cache_set_and_get(self):
        """Basic set and get works."""
        cache = QueryCache(max_size=100)

        cache.set("key1", {"data": "value"})
        result = cache.get("key1")

        assert result == {"data": "value"}

    def test_cache_ttl_expiration(self):
        """TTL expiration works."""
        cache = QueryCache(max_size=100, default_ttl_seconds=0.1)

        cache.set("key1", "value")
        assert cache.has("key1")

        time.sleep(0.15)

        assert not cache.has("key1")
        assert cache.get("key1") is None

    def test_cache_lru_eviction(self):
        """LRU eviction works when max size reached."""
        cache = QueryCache(max_size=3)

        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)

        # Access 'a' to make it recently used
        cache.get("a")

        # Add new item, should evict 'b' (least recently used)
        cache.set("d", 4)

        assert cache.has("a")
        assert not cache.has("b")
        assert cache.has("c")
        assert cache.has("d")

    def test_cache_invalidate_key(self):
        """Invalidate specific key works."""
        cache = QueryCache(max_size=100)

        cache.set("key1", "value1")
        cache.set("key2", "value2")

        cache.invalidate("key1")

        assert not cache.has("key1")
        assert cache.has("key2")

    def test_cache_invalidate_pattern(self):
        """Pattern invalidation works."""
        cache = QueryCache(max_size=100)

        cache.set("users:1", "user1")
        cache.set("users:2", "user2")
        cache.set("products:1", "product1")

        count = cache.invalidate_pattern("users:*")

        assert count == 2
        assert not cache.has("users:1")
        assert not cache.has("users:2")
        assert cache.has("products:1")

    def test_cache_invalidate_table(self):
        """Table invalidation works."""
        cache = QueryCache(max_size=100)

        cache.set("key1", "value1", table="users")
        cache.set("key2", "value2", table="users")
        cache.set("key3", "value3", table="products")

        count = cache.invalidate_table("users")

        assert count == 2
        assert not cache.has("key1")
        assert cache.has("key3")

    def test_cache_stats(self):
        """Cache statistics are tracked."""
        cache = QueryCache(max_size=100)

        cache.set("key1", "value1")
        cache.get("key1")  # Hit
        cache.get("key2")  # Miss

        stats = cache.stats

        assert stats.hits == 1
        assert stats.misses == 1
        assert stats.hit_rate == 0.5

    def test_cache_clear(self):
        """Clear all entries works."""
        cache = QueryCache(max_size=100)

        cache.set("a", 1)
        cache.set("b", 2)

        count = cache.clear()

        assert count == 2
        assert len(cache) == 0


class TestCachedQueryDecorator:
    """Tests for cached_query decorator."""

    @pytest.mark.parametrize("flavour", ["sync", "async"])
    def test_a_decorated_function_runs_once_and_its_cache_counts_the_miss_and_the_hit(self, flavour):
        cache = QueryCache()
        calls = 0

        if flavour == "sync":

            @cached_query(ttl_seconds=60, cache=cache)
            def expensive():
                nonlocal calls
                calls += 1
                return {"result": "data"}

            results = [expensive(), expensive()]
        else:

            @async_cached_query(ttl_seconds=60, cache=cache)
            async def expensive():
                nonlocal calls
                calls += 1
                return {"result": "data"}

            async def twice():
                return [await expensive(), await expensive()]

            results = asyncio.run(twice())

        assert (calls, results) == (1, [{"result": "data"}, {"result": "data"}])
        stats = cache.stats
        assert (stats.hits, stats.misses, stats.hit_rate) == (1, 1, 0.5)


# =============================================================================
# a cached function and the writes of its table
# =============================================================================

SYNC_BASES = {
    "pydantic": SQLerModel,
    "pydantic_safe": SQLerSafeModel,
    "lite": SQLerLiteModel,
    "lite_safe": SQLerLiteSafeModel,
    "msgspec": SQLerMsgspecModel,
}
ASYNC_BASES = {
    "pydantic": AsyncSQLerModel,
    "pydantic_safe": AsyncSQLerSafeModel,
    "lite": AsyncSQLerLiteModel,
    "lite_safe": AsyncSQLerLiteSafeModel,
    "msgspec": AsyncSQLerMsgspecModel,
}

_tables = itertools.count()


LITE = (SQLerLiteModel, SQLerLiteSafeModel, AsyncSQLerLiteModel, AsyncSQLerLiteSafeModel)


def make(base, *mixins, table=None, alias=None):
    """A class of ``base`` with the cache mixin, the ``mixins`` and a ``name`` field, on a
    table of its own; ``alias``: its ``Meta.cache_table``."""
    table = table or f"qc_{base.__name__.lower()}_{next(_tables)}"
    ns = {"__module__": __name__, "__tablename__": table, "name": "", "__annotations__": {"name": str}}
    kwds = {"tag": False} if base in (SQLerMsgspecModel, AsyncSQLerMsgspecModel) else {}
    cls = types.new_class(f"Item_{table}", (CacheAwareModel, *mixins, base), kwds, lambda body: body.update(ns))
    if alias is not None:
        cls.Meta = type("Meta", (), {"cache_table": alias})
    return dataclass(cls) if base in LITE else cls


class Counted:
    """A cached function of the names in ``cls``'s rows on ``db``, tagged with ``tag`` (the
    class by default), that counts its runs."""

    def __init__(self, cls, db, tag="class", *, is_async=False):
        self.runs = 0
        self.key = f"names:{cls.__tablename__}"
        table = cls if tag == "class" else tag
        if is_async:

            @async_cached_query(table=table, key=self.key)
            async def names():
                self.runs += 1
                return sorted(m.name for m in await cls.using(db).all())

        else:

            @cached_query(table=table, key=self.key)
            def names():
                self.runs += 1
                return sorted(m.name for m in cls.using(db).all())

        self.names = names

    def stored(self) -> bool:
        return get_cache().get_entry(self.key) is not None


def table_names(db, table) -> list:
    """The names in ``table``, read with SQL of the test's own."""
    cur = db.adapter.execute(f"SELECT json_extract(data, '$.name') FROM {table}")
    return sorted(r[0] for r in cur.fetchall())


def _shape(sent, table):
    return [e.sql.replace(table, "T") for e in sent]


def _plain(base, *mixins):
    """A class of ``base`` with the ``mixins`` and no cache mixin."""
    table = f"qc_plain_{next(_tables)}"
    ns = {"__tablename__": table, "name": "", "__annotations__": {"name": str}}
    kwds = {"tag": False} if base in (SQLerMsgspecModel, AsyncSQLerMsgspecModel) else {}
    cls = types.new_class(f"Plain_{table}", (*mixins, base), kwds, lambda body: body.update(ns))
    return dataclass(cls) if base in LITE else cls


async def atable_names(db, table) -> list:
    cur = await db.adapter.execute(f"SELECT json_extract(data, '$.name') FROM {table}")
    rows = await cur.fetchall()
    await cur.close()
    return sorted(r[0] for r in rows)


@pytest.fixture(autouse=True)
def fresh_cache():
    configure_cache()
    yield
    configure_cache()


@pytest.fixture
def db():
    d = SQLerDB.in_memory(shared=False)
    yield d
    d.close()


@pytest.fixture
def disk_db(tmp_path):
    d = SQLerDB.on_disk(str(tmp_path / "cache.db"))
    yield d
    d.close()


# ----- a reader between the writer's save and its commit ---------------------------------


def test_a_result_read_before_a_commit_is_not_served_after_it(disk_db):
    Item = make(SQLerModel)
    Item(name="old").save(db=disk_db)
    f = Counted(Item, disk_db)
    assert f.names() == ["old"]
    saved, read = threading.Event(), threading.Event()
    seen = []

    def writer():
        with disk_db.transaction():
            Item(name="new").save(db=disk_db)
            saved.set()
            read.wait(5)

    def reader():
        saved.wait(5)
        seen.append(f.names())
        read.set()

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen == [["old"]]
    assert f.names() == ["new", "old"]
    assert table_names(disk_db, Item.__tablename__) == ["new", "old"]


@pytest.mark.asyncio
async def test_a_result_read_before_a_commit_is_not_served_after_it_async(tmp_path):
    adb = AsyncSQLerDB.on_disk(str(tmp_path / "acache.db"))
    await adb.connect()
    try:
        Item = make(AsyncSQLerModel)
        await Item(name="old").save(db=adb)
        f = Counted(Item, adb, is_async=True)
        assert await f.names() == ["old"]
        saved, read = asyncio.Event(), asyncio.Event()
        seen = []

        async def writer():
            async with adb.transaction():
                await Item(name="new").save(db=adb)
                saved.set()
                await read.wait()

        async def reader():
            await saved.wait()
            seen.append(await f.names())
            read.set()

        await asyncio.gather(writer(), reader())

        assert seen == [["old"]]
        assert await f.names() == ["new", "old"]
        assert await atable_names(adb, Item.__tablename__) == ["new", "old"]
    finally:
        await adb.close()


# ----- every way of writing, on every base ---------------------------------------------

def _first(q, name):
    return [m for m in q.all() if m.name == name][0]


SYNC_WAYS = {
    "save_new": (lambda cls, db: cls(name="new").save(db=db), ["a", "b", "new"]),
    "save_changed": (lambda cls, db: _renamed(_first(cls.using(db), "a")).save(db=db), ["b", "new"]),
    "save_partial": (lambda cls, db: _renamed(_first(cls.using(db), "a")).save_partial(db=db), ["b", "new"]),
    "delete": (lambda cls, db: _first(cls.using(db), "a").delete(db=db), ["b"]),
    "delete_with_policy": (
        lambda cls, db: _first(cls.using(db), "a").delete_with_policy(db=db, on_delete="cascade"),
        ["b"],
    ),
    "save_many": (lambda cls, db: cls.save_many([cls(name="new"), cls(name="new2")], db=db), ["a", "b", "new", "new2"]),
    "update": (lambda cls, db: cls.using(db).update(name="new"), ["new", "new"]),
    "update_one": (lambda cls, db: cls.using(db).filter(F("name") == "a").update_one(name="new"), ["b", "new"]),
    "update_n": (lambda cls, db: cls.using(db).filter(F("name") == "a").update_n(1, name="new"), ["b", "new"]),
    "delete_all": (lambda cls, db: cls.using(db).filter(F("name") == "a").delete_all(), ["b"]),
}
SET_WAYS = {"update", "update_one", "update_n", "delete_all"}


def _renamed(m):
    m.name = "new"
    return m


def _sync_cells():
    for way in SYNC_WAYS:
        for kind, base in SYNC_BASES.items():
            yield pytest.param(way, kind, id=f"{way}-{kind}")


@pytest.mark.parametrize("way,kind", list(_sync_cells()))
def test_every_way_of_writing_makes_the_cached_result_fresh(db, way, kind):
    mixins = (PartialUpdateMixin, TrackedModel) if way == "save_partial" else ()
    Item = make(SYNC_BASES[kind], *mixins)
    Item.save_many([Item(name="a"), Item(name="b")], db=db)
    f = Counted(Item, db)
    assert f.names() == ["a", "b"]
    write, expected = SYNC_WAYS[way]

    write(Item, db)

    assert f.names() == expected
    assert table_names(db, Item.__tablename__) == expected


def test_a_queryset_built_by_hand_without_a_database_makes_the_cached_result_fresh(db):
    Item = make(SQLerModel)
    Item.save_many([Item(name="a"), Item(name="b")], db=db)
    f = Counted(Item, db)
    assert f.names() == ["a", "b"]

    SQLerQuerySet(Item, SQLerQuery(table=Item.__tablename__, adapter=db.adapter)).update(name="new")

    assert f.names() == ["new", "new"]
    assert table_names(db, Item.__tablename__) == ["new", "new"]


async def _afirst(q, name):
    return [m for m in await q.all() if m.name == name][0]


async def _asave_changed(cls, db):
    return await _renamed(await _afirst(cls.using(db), "a")).save(db=db)


async def _asave_partial(cls, db):
    return await _renamed(await _afirst(cls.using(db), "a")).save_partial(db=db)


async def _adelete(cls, db):
    return await (await _afirst(cls.using(db), "a")).delete(db=db)


async def _adelete_with_policy(cls, db):
    return await (await _afirst(cls.using(db), "a")).delete_with_policy(db=db, on_delete="cascade")


ASYNC_WAYS = {
    "save_new": (lambda cls, db: cls(name="new").save(db=db), ["a", "b", "new"]),
    "save_changed": (_asave_changed, ["b", "new"]),
    "save_partial": (_asave_partial, ["b", "new"]),
    "delete": (_adelete, ["b"]),
    "delete_with_policy": (_adelete_with_policy, ["b"]),
    "save_many": (lambda cls, db: cls.save_many([cls(name="new"), cls(name="new2")], db=db), ["a", "b", "new", "new2"]),
    "update": (lambda cls, db: cls.using(db).update(name="new"), ["new", "new"]),
    "update_one": (lambda cls, db: cls.using(db).filter(F("name") == "a").update_one(name="new"), ["b", "new"]),
    "update_n": (lambda cls, db: cls.using(db).filter(F("name") == "a").update_n(1, name="new"), ["b", "new"]),
    "delete_all": (lambda cls, db: cls.using(db).filter(F("name") == "a").delete_all(), ["b"]),
}


def _async_cells():
    for way in ASYNC_WAYS:
        for kind in ASYNC_BASES:
            yield pytest.param(way, kind, id=f"{way}-{kind}")


@pytest_asyncio.fixture
async def adb():
    d = AsyncSQLerDB.in_memory(shared=False)
    await d.connect()
    yield d
    await d.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("way,kind", list(_async_cells()))
async def test_every_way_of_writing_makes_the_cached_result_fresh_async(adb, way, kind):
    mixins = (AsyncPartialUpdateMixin, AsyncTrackedModel) if way == "save_partial" else ()
    Item = make(ASYNC_BASES[kind], *mixins)
    await Item.save_many([Item(name="a"), Item(name="b")], db=adb)
    f = Counted(Item, adb, is_async=True)
    assert await f.names() == ["a", "b"]
    write, expected = ASYNC_WAYS[way]

    await write(Item, adb)

    assert await f.names() == expected
    assert await atable_names(adb, Item.__tablename__) == expected


PYDANTIC_NAMES = set(dir(pydantic.BaseModel))


@pytest.mark.parametrize("way", sorted(SET_WAYS))
def test_a_set_based_write_of_a_class_with_an_audit_log_makes_the_cached_result_fresh(db, way):
    Item = make(SQLerModel, AuditLogMixin)
    Item.save_many([Item(name="a"), Item(name="b")], db=db)
    f = Counted(Item, db)
    assert f.names() == ["a", "b"]
    write, expected = SYNC_WAYS[way]

    write(Item, db)

    assert f.names() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("way", sorted(SET_WAYS))
async def test_a_set_based_write_of_a_class_with_an_audit_log_makes_the_cached_result_fresh_async(adb, way):
    Item = make(AsyncSQLerModel, AsyncAuditLogMixin)
    await Item.save_many([Item(name="a"), Item(name="b")], db=adb)
    f = Counted(Item, adb, is_async=True)
    assert await f.names() == ["a", "b"]
    write, expected = ASYNC_WAYS[way]

    await write(Item, adb)

    assert await f.names() == expected


class QcPlace(CacheAwareModel, SQLerModel):
    __tablename__ = "qc_places"
    name: str = ""


class QcVisit(SQLerModel):
    __tablename__ = "qc_visits"
    place: Optional[QcPlace] = None


def test_a_delete_all_of_rows_another_class_can_refer_to_makes_the_cached_result_fresh(db):
    QcPlace.save_many([QcPlace(name="s"), QcPlace(name="t")], db=db)
    f = Counted(QcPlace, db)
    assert f.names() == ["s", "t"]

    QcPlace.using(db).filter(F("name") == "s").delete_all()

    assert f.names() == ["t"]


WRITE_VERBS = ("save", "delete", "update", "upsert", "insert", "bulk", "replace", "remove")


def _write_ways(*classes) -> set:
    """The public ways of writing rows the classes have: their methods named for a write."""
    return {
        name
        for cls in classes
        for name in dir(cls)
        if name.startswith(WRITE_VERBS) and name not in PYDANTIC_NAMES
    }


def test_the_table_of_ways_names_every_way_of_writing():
    """A way of writing added to a base or a queryset that the tables above do not name fails
    here; ``save_partial`` is the change tracker's, the policy rows are below."""
    sync = _write_ways(*SYNC_BASES.values(), SQLerQuerySet, PartialUpdateMixin)
    asynchronous = _write_ways(*ASYNC_BASES.values(), AsyncSQLerQuerySet, AsyncPartialUpdateMixin)

    assert sync == set(SYNC_WAYS) - {"save_new", "save_changed"} | {"save"}
    assert asynchronous == set(ASYNC_WAYS) - {"save_new", "save_changed"} | {"save"}


# ----- the rows a delete's policy changes ----------------------------------------------


class QcShelf(SQLerModel):
    __tablename__ = "qc_shelves"
    name: str = ""


class QcBook(CacheAwareModel, SQLerModel):
    __tablename__ = "qc_books"
    name: str = ""
    shelf: Optional[QcShelf] = None


class AQcShelf(AsyncSQLerModel):
    __tablename__ = "aqc_shelves"
    name: str = ""


class AQcBook(CacheAwareModel, AsyncSQLerModel):
    __tablename__ = "aqc_books"
    name: str = ""
    shelf: Optional[AQcShelf] = None


POLICIES = {"set_null": [("a", False), ("b", False)], "cascade": []}


@pytest.mark.parametrize("policy", list(POLICIES))
def test_the_rows_a_policy_changes_make_their_cached_result_fresh(db, policy):
    shelf = QcShelf(name="s").save(db=db)
    QcBook.save_many([QcBook(name="a", shelf=shelf), QcBook(name="b", shelf=shelf)], db=db)

    @cached_query(table=QcBook, key="books")
    def books():
        return sorted((b.name, b.shelf is not None) for b in QcBook.using(db).all())

    assert books() == [("a", True), ("b", True)]

    shelf.delete_with_policy(db=db, on_delete=policy)

    assert books() == POLICIES[policy]


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", list(POLICIES))
async def test_the_rows_a_policy_changes_make_their_cached_result_fresh_async(adb, policy):
    shelf = await AQcShelf(name="s").save(db=adb)
    await AQcBook.save_many([AQcBook(name="a", shelf=shelf), AQcBook(name="b", shelf=shelf)], db=adb)

    @async_cached_query(table=AQcBook, key="abooks")
    async def books():
        return sorted((b.name, b.shelf is not None) for b in await AQcBook.using(adb).resolve(False).all())

    assert await books() == [("a", True), ("b", True)]

    await shelf.delete_with_policy(db=adb, on_delete=policy)

    assert await books() == POLICIES[policy]


# ----- inside a transaction ------------------------------------------------------------


def test_the_writers_own_cached_call_reads_its_uncommitted_rows_and_stores_nothing(disk_db):
    Item = make(SQLerModel)
    Item.save_many([Item(name="a"), Item(name="b")], db=disk_db)
    f = Counted(Item, disk_db)
    assert f.names() == ["a", "b"]

    with disk_db.transaction():
        Item(name="new").save(db=disk_db)
        assert f.names() == ["a", "b", "new"]
        assert f.names() == ["a", "b", "new"]
        assert (f.runs, f.stored()) == (3, False)

    assert f.names() == ["a", "b", "new"]
    assert f.names() == ["a", "b", "new"]
    assert (f.runs, f.stored()) == (4, True)


def test_another_threads_result_during_a_transaction_does_not_survive_its_commit(disk_db):
    Item = make(SQLerModel)
    Item.save_many([Item(name="a"), Item(name="b")], db=disk_db)
    f = Counted(Item, disk_db)
    seen = []

    with disk_db.transaction():
        Item(name="new").save(db=disk_db)
        t = threading.Thread(target=lambda: seen.append(f.names()))
        t.start()
        t.join()

    assert seen == [["a", "b"]]
    assert f.names() == ["a", "b", "new"]


@pytest.mark.asyncio
async def test_inside_a_transaction_the_writers_task_reads_through_and_the_end_drops_the_rest(tmp_path):
    adb = AsyncSQLerDB.on_disk(str(tmp_path / "t.db"))
    await adb.connect()
    try:
        Item = make(AsyncSQLerModel)
        await Item.save_many([Item(name="a"), Item(name="b")], db=adb)
        f = Counted(Item, adb, is_async=True)
        assert await f.names() == ["a", "b"]

        with pytest.raises(KeyError):
            async with adb.transaction():
                await Item(name="new").save(db=adb)
                assert await f.names() == ["a", "b", "new"]
                assert await asyncio.create_task(f.names()) == ["a", "b", "new"]
                assert f.stored() is False
                raise KeyError("undo")
        assert await f.names() == ["a", "b"]

        async with adb.transaction():
            await Item(name="new").save(db=adb)
        assert await f.names() == ["a", "b", "new"]
        assert await f.names() == ["a", "b", "new"]
        assert (f.runs, f.stored()) == (5, True)
    finally:
        await adb.close()


# ----- the name of a table's results ---------------------------------------------------


@pytest.mark.parametrize("tag", ["class", "table", "alias", "TABLE"])
def test_an_entry_tagged_by_the_class_its_table_or_its_alias_is_dropped_by_a_write(db, tag):
    Item = make(SQLerModel, alias="qc_alias_items")
    table = Item.__tablename__
    by = {"class": Item, "table": table, "alias": "qc_alias_items", "TABLE": table.upper()}[tag]
    Item(name="a").save(db=db)
    get_cache().set("tagged", "stale", table=by if isinstance(by, str) else table)
    f = Counted(Item, db, by)
    assert f.names() == ["a"]

    Item(name="b").save(db=db)

    assert get_cache().get_entry("tagged") is None
    assert f.names() == ["a", "b"]


def test_an_entry_tagged_with_the_class_name_in_lower_case_is_not_the_classs(db):
    Item = make(SQLerModel)
    get_cache().set("by_name", "kept", table=Item.__name__.lower())

    Item(name="a").save(db=db)

    assert get_cache().get("by_name") == "kept"


# ----- async: the write runs when it is awaited ----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["save", "delete"])
async def test_an_async_write_tells_the_cache_when_it_has_run_not_when_it_is_made(adb, way):
    Item = make(AsyncSQLerModel, table="qc_made_" + way, alias="qc_made_" + way)
    m = await Item(name="a").save(db=adb)
    get_cache().set("tagged", "stale", table=Item.__tablename__)

    pending = m.save(db=adb) if way == "save" else m.delete(db=adb)
    assert get_cache().get("tagged") == "stale"
    await pending

    assert get_cache().get_entry("tagged") is None


# ----- hostile inputs ------------------------------------------------------------------


def test_a_cached_function_that_raises_stores_nothing_and_the_next_call_reads_fresh(db):
    Item = make(SQLerModel)
    Item(name="a").save(db=db)
    runs = []

    @cached_query(table=Item, key="raising")
    def names():
        runs.append(1)
        found = sorted(m.name for m in Item.using(db).all())
        if len(runs) == 1:
            raise LookupError("first call fails")
        return found

    with pytest.raises(LookupError):
        names()
    assert get_cache().get_entry("raising") is None
    Item(name="b").save(db=db)

    assert names() == ["a", "b"]
    assert names() == ["a", "b"]
    assert len(runs) == 2


def test_a_cached_function_that_writes_its_table_stores_nothing(db):
    Item = make(SQLerModel)
    Item(name="a").save(db=db)
    runs = []

    @cached_query(table=Item, key="writing")
    def names():
        runs.append(1)
        Item(name=f"w{len(runs)}").save(db=db)
        return sorted(m.name for m in Item.using(db).all())

    assert names() == ["a", "w1"]
    assert get_cache().get_entry("writing") is None
    assert names() == ["a", "w1", "w2"]


def test_only_the_table_a_transaction_wrote_is_read_through(disk_db):
    Written, Other = make(SQLerModel), make(SQLerModel)
    Written(name="a").save(db=disk_db)
    Other(name="o").save(db=disk_db)
    written, other = Counted(Written, disk_db), Counted(Other, disk_db)
    assert (written.names(), other.names()) == (["a"], ["o"])

    with disk_db.transaction():
        Written(name="b").save(db=disk_db)
        assert (written.names(), other.names()) == (["a", "b"], ["o"])
        assert (written.runs, other.runs) == (2, 1)
        assert (written.stored(), other.stored()) == (False, True)


def test_an_inner_rollback_under_an_outer_commit_leaves_the_outer_rows_fresh(disk_db):
    Item = make(SQLerModel)
    Item(name="a").save(db=disk_db)
    f = Counted(Item, disk_db)
    assert f.names() == ["a"]

    with disk_db.transaction():
        Item(name="outer").save(db=disk_db)
        with pytest.raises(KeyError):
            with disk_db.transaction():
                Item(name="inner").save(db=disk_db)
                raise KeyError("undo the inner level")
        assert f.names() == ["a", "outer"]
        assert f.stored() is False

    assert f.names() == ["a", "outer"]
    assert f.names() == ["a", "outer"]
    assert (f.runs, f.stored()) == (3, True)


def test_a_table_written_in_an_inner_level_that_commits_is_held_until_the_outer_level_ends(disk_db):
    Item = make(SQLerModel)
    Item(name="a").save(db=disk_db)
    f = Counted(Item, disk_db)
    seen = []

    with disk_db.transaction():
        with disk_db.transaction():
            Item(name="inner").save(db=disk_db)
        t = threading.Thread(target=lambda: seen.append(f.names()))
        t.start()
        t.join()
        assert f.stored() is False

    assert seen == [["a"]]
    assert f.names() == ["a", "inner"]
    assert f.names() == ["a", "inner"]
    assert (f.runs, f.stored()) == (2, True)


@pytest.mark.asyncio
async def test_a_task_that_writes_after_its_transaction_ended_does_not_hold_the_table(tmp_path):
    adb = AsyncSQLerDB.on_disk(str(tmp_path / "late.db"))
    await adb.connect()
    try:
        Item = make(AsyncSQLerModel)
        await Item(name="a").save(db=adb)
        f = Counted(Item, adb, is_async=True)
        go = asyncio.Event()

        async def late():
            await go.wait()
            await Item(name="late").save(db=adb)

        async with adb.transaction():
            task = asyncio.create_task(late())
        go.set()
        await task

        assert await f.names() == ["a", "late"]
        assert await f.names() == ["a", "late"]
        assert (f.runs, f.stored()) == (1, True)
    finally:
        await adb.close()


def test_a_transaction_left_by_an_exception_lifts_the_bypass_and_keeps_nothing_set_in_it(disk_db):
    Item = make(SQLerModel)
    Item(name="a").save(db=disk_db)
    f = Counted(Item, disk_db)

    with pytest.raises(KeyError):
        with disk_db.transaction():
            Item(name="new").save(db=disk_db)
            get_cache().set("tagged", "stale", table=Item)
            raise KeyError("leave")

    assert get_cache().get_entry("tagged") is None
    assert f.names() == ["a"]
    assert f.names() == ["a"]
    assert (f.runs, f.stored()) == (1, True)


def test_a_transaction_whose_commit_fails_lifts_the_bypass(disk_db):
    Item = make(SQLerModel)
    Item(name="a").save(db=disk_db)
    f = Counted(Item, disk_db)
    disk_db.adapter.execute("PRAGMA foreign_keys = ON")
    disk_db.adapter.execute("CREATE TABLE qc_parent (id INTEGER PRIMARY KEY)")
    disk_db.adapter.execute(
        "CREATE TABLE qc_child (pid INTEGER REFERENCES qc_parent(id) DEFERRABLE INITIALLY DEFERRED)"
    )
    disk_db.adapter.auto_commit()

    with pytest.raises(Exception, match="FOREIGN KEY constraint failed"):
        with disk_db.transaction():
            Item(name="new").save(db=disk_db)
            disk_db.adapter.execute("INSERT INTO qc_child VALUES (99)")

    assert f.names() == ["a"]
    assert f.names() == ["a"]
    assert (f.runs, f.stored()) == (1, True)


def test_a_transaction_a_raw_commit_ended_lifts_the_bypass_at_the_blocks_exit(disk_db):
    Item = make(SQLerModel)
    Item(name="a").save(db=disk_db)
    f = Counted(Item, disk_db)

    with pytest.raises(TransactionStateError):
        with disk_db.transaction():
            Item(name="new").save(db=disk_db)
            assert f.names() == ["a", "new"]
            disk_db.adapter.execute("COMMIT")
    assert f.stored() is False

    assert f.names() == ["a", "new"]
    assert f.names() == ["a", "new"]
    assert (f.runs, f.stored()) == (2, True)


@pytest.mark.asyncio
async def test_a_transaction_a_raw_commit_ended_lifts_the_bypass_at_the_blocks_exit_async(tmp_path):
    adb = AsyncSQLerDB.on_disk(str(tmp_path / "raw.db"))
    await adb.connect()
    try:
        Item = make(AsyncSQLerModel)
        await Item(name="a").save(db=adb)
        f = Counted(Item, adb, is_async=True)

        with pytest.raises(TransactionStateError):
            async with adb.transaction():
                await Item(name="new").save(db=adb)
                assert await f.names() == ["a", "new"]
                await adb.adapter.execute("COMMIT")
        assert f.stored() is False

        assert await f.names() == ["a", "new"]
        assert await f.names() == ["a", "new"]
        assert (f.runs, f.stored()) == (2, True)
    finally:
        await adb.close()


def test_ten_thousand_entries_of_a_table_are_dropped_by_its_write(db):
    configure_cache(max_size=20_000)
    Item = make(SQLerModel)
    Item(name="a").save(db=db)
    for i in range(10_000):
        get_cache().set(f"entry:{i}", i, table=Item.__tablename__)
    assert len(get_cache()) == 10_000

    with db.transaction():
        Item(name="b").save(db=db)

    assert len(get_cache()) == 0


@pytest.mark.parametrize("how", ["reconfigured", "no_room"])
def test_a_cache_reconfigured_inside_a_transaction_keeps_its_rule(disk_db, how):
    Item = make(SQLerModel)
    Item(name="a").save(db=disk_db)
    f = Counted(Item, disk_db)
    assert f.names() == ["a"]
    old = get_cache()
    seen = []

    with disk_db.transaction():
        Item(name="new").save(db=disk_db)
        configure_cache() if how == "reconfigured" else configure_cache(max_size=0)
        old.set(f.key, ["stale"], table=Item.__tablename__)
        t = threading.Thread(target=lambda: seen.append(f.names()))
        t.start()
        t.join()
        assert f.stored() is False

    assert seen == [["a"]]
    assert old.get_entry(f.key) is None
    assert f.names() == ["a", "new"]


def test_a_class_with_an_audit_log_drops_its_table_and_not_the_audit_table(db):
    Item = make(SQLerModel, AuditLogMixin)
    Item(name="a").save(db=db)
    f = Counted(Item, db)
    assert f.names() == ["a"]
    get_cache().set("audit", "kept", table=f"{Item.__tablename__}_audit")

    Item(name="b").save(db=db)

    assert f.names() == ["a", "b"]
    assert get_cache().get("audit") == "kept"


def test_an_entry_with_no_table_is_untouched_by_every_write(db):
    Item = make(SQLerModel)
    Item.save_many([Item(name="a"), Item(name="b")], db=db)
    get_cache().set("untagged", "kept")
    for way in ("save_new", "save_changed", "delete", "delete_with_policy", "save_many"):
        Item(name="a").save(db=db)
        SYNC_WAYS[way][0](Item, db)
    with db.transaction():
        Item(name="in").save(db=db)

    assert get_cache().get("untagged") == "kept"


# ----- what it costs -------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(SYNC_BASES))
def test_the_cache_mixin_sends_no_statement(db, kind):
    base = SYNC_BASES[kind]
    Cached = make(base)
    Plain = _plain(base)
    shapes = []
    for cls in (Cached, Plain):
        cls(name="warm").save(db=db)
        with query_logger.capture() as sent:
            m = cls(name="a").save(db=db)
            m.name = "b"
            m.save(db=db)
            cls.save_many([cls(name="c")], db=db)
            with db.transaction():
                cls(name="d").save(db=db)
            m.delete(db=db)
        shapes.append(_shape(sent, cls.__tablename__))

    assert shapes[0] == shapes[1]


def test_a_class_without_the_cache_mixin_takes_no_lock_of_the_cache(disk_db):
    Plain = _plain(SQLerModel)
    caches = [get_cache(), QueryCache()]
    held, release = threading.Event(), threading.Event()

    def holder():
        with contextlib.ExitStack() as stack:
            for lock in (cache_module._held_lock, caches[0]._lock, caches[1]._lock):
                stack.enter_context(lock)
            held.set()
            release.wait(30)

    def write():
        m = Plain(name="a").save(db=disk_db)
        with disk_db.transaction():
            m.name = "b"
            m.save(db=disk_db)
        Plain.save_many([Plain(name="c")], db=disk_db)
        m.delete(db=disk_db)
        done.append(True)

    done = []
    h = threading.Thread(target=holder)
    h.start()
    held.wait(5)
    w = threading.Thread(target=write)
    w.start()
    w.join(20)
    release.set()
    h.join()

    assert done == [True]


def test_the_cache_lock_is_free_while_a_cached_function_runs(db):
    Item = make(SQLerModel)
    free = []

    def try_lock():
        got = get_cache()._lock.acquire(blocking=False)
        free.append(got)
        if got:
            get_cache()._lock.release()

    @cached_query(table=Item, key="locked")
    def names():
        t = threading.Thread(target=try_lock)
        t.start()
        t.join()
        return sorted(m.name for m in Item.using(db).all())

    assert names() == []
    assert free == [True]

