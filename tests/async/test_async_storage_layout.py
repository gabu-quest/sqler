"""Async twin of the "what a database object remembers" part of ``tests/test_storage_layout.py``:
what an ``AsyncSQLerDB`` remembers of a table holds for one schema version of its database."""

import json
import sqlite3

import pytest

from sqler import AsyncSQLerDB, AsyncSQLerModel, F, SQLerDB
from sqler.logging import query_logger

# scene: (what ``a`` learns first, what ``b`` does, the key ``a`` writes)
SCENES = {
    "promote": ({}, lambda b: b.ensure_table("t", promoted={"status": "TEXT"}), "status"),
    "promote a second": (
        {"status": "TEXT"},
        lambda b: b.ensure_table("t", promoted={"status": "TEXT", "prio": "TEXT"}),
        "prio",
    ),
    "add _version": ({}, lambda b: b.ensure_table("t", versioned=True), "_version"),
    "drop and make again": (
        {"status": "TEXT"},
        lambda b: (
            b.adapter.execute("DROP TABLE t;"),
            b.adapter.auto_commit(),
            b.ensure_table("t", promoted={"other": "TEXT"}),
        ),
        "other",
    ),
    "rename a column": (
        {"status": "TEXT"},
        lambda b: (
            b.adapter.execute("ALTER TABLE t RENAME COLUMN status TO prio;"),
            b.adapter.auto_commit(),
        ),
        "prio",
    ),
    "drop a column": (
        {"status": "TEXT", "prio": "TEXT"},
        lambda b: (b.adapter.execute("ALTER TABLE t DROP COLUMN status;"), b.adapter.auto_commit()),
        "prio",
    ),
    # the names ``SELECT *`` gives, in another order: every statement runs, nothing is learned
    "same columns in another order": (
        {"status": "TEXT"},
        lambda b: (
            b.adapter.execute("DROP TABLE t;"),
            b.adapter.execute(
                "CREATE TABLE t (status TEXT, _id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL);"
            ),
            b.adapter.auto_commit(),
        ),
        "status",
    ),
}
# a scene of a change SQLite itself makes runs on one writer and one reader
ONE_WAY = {
    "rename a column": "insert_document",
    "drop a column": "find_document",
    "same columns in another order": "insert_document",
}


class PromotingRow(AsyncSQLerModel):
    """A class of ``t`` that promotes ``status`` itself: its writers are told the columns."""

    name: str
    status: str | None = None
    prio: str | None = None
    other: str | None = None

    __tablename__ = "t"
    __promoted__ = {"status": "TEXT"}


class SceneRow(AsyncSQLerModel):
    name: str
    status: str | None = None
    prio: str | None = None
    other: str | None = None

    __tablename__ = "t"


WAYS = [
    "insert_document",
    "upsert_document",
    "the fields update",
    "insert_many",
    "bulk_upsert",
    "upsert_with_version",
    "promoting model save",
    "promoting save_many",
    "find_document",
    "query all",
    "count",
]
READS = ("find_document", "query all", "count")
# a class's own promoted column is gone from a table remade without it: its save is refused
CELLS = [
    (scene, way)
    for scene in SCENES
    for way in WAYS
    if ONE_WAY.get(scene, way) == way
    and not (scene == "drop and make again" and way.startswith("promoting"))
]


async def _write(a, way, key):
    doc = {"name": "y"} if key == "_version" else {"name": "y", key: "v"}
    if way == "insert_document":
        return await a.insert_document("t", doc)
    if way == "upsert_document":
        return await a.upsert_document("t", 1, doc)
    if way == "the fields update":
        await a._update_fields("t", 1, doc, list(doc))
        return 1
    if way == "insert_many":
        return (await a.insert_many("t", [doc]))[0]
    if way == "bulk_upsert":
        return (await a.bulk_upsert("t", [dict(doc, _id=1)]))[0]
    if way == "promoting model save":
        return (await PromotingRow(**doc).save(db=a))._id
    if way == "promoting save_many":
        return (await PromotingRow.save_many([PromotingRow(**doc)], db=a))[0]._id
    return (await a.upsert_with_version("t", None, doc, None))[0]


def _stored(path, _id, key):
    conn = sqlite3.connect(path)
    try:
        data, value = conn.execute(f"SELECT data, {key} FROM t WHERE _id = ?", [_id]).fetchone()
    finally:
        conn.close()
    return json.loads(data), value


def _change(path, scene):
    """``b`` changes ``t`` and leaves row 1 with ``'old'`` in the key's column (``_version`` 7)."""
    _, change, key = SCENES[scene]
    b = SQLerDB.on_disk(path)
    change(b)
    if scene in ("drop and make again", "same columns in another order"):
        b.insert_document("t", {"name": "x"})
    b.adapter.execute(
        f"UPDATE t SET {key} = {7 if key == '_version' else repr('old')} WHERE _id = 1;"
    )
    b.adapter.auto_commit()
    b.close()


async def _read(a, way, key):
    """Row 1 of ``t`` read ``way``: the key's value (a plain model does not read ``_version``)."""
    old = 7 if key == "_version" else "old"
    if way == "find_document":
        read = a.find_document_with_version if key == "_version" else a.find_document
        return (await read("t", 1))[key], old
    model = None if key == "_version" else old
    if way == "query all":
        rows = await SceneRow.using(a).all()
        return next(r for r in rows if r._id == 1).model_dump().get(key), model
    return await SceneRow.using(a).filter(F(key) == old).count(), 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("scene", "way"), CELLS)
async def test_a_unit_after_another_object_changed_the_table_acts_on_it_as_it_is(
    tmp_path, scene, way
):
    first, _, key = SCENES[scene]
    path = str(tmp_path / "scene.db")
    setup = SQLerDB.on_disk(path)
    setup.ensure_table("t", promoted=first) if first else setup._ensure_table("t")
    setup.close()
    a = AsyncSQLerDB.on_disk(path)
    await a.connect()
    try:
        await a.insert_document("t", {"name": "x"})
        await a.find_document("t", 1)
        await a._has_version_column("t")
        _change(path, scene)
        with query_logger.capture() as sent:
            if way in READS:
                got, expected = await _read(a, way, key)
                assert got == expected
                return
            _id = await _write(a, way, key)
        learned = [q.sql for q in sent if q.sql.startswith("PRAGMA")]
        assert bool(learned) is (scene != "same columns in another order")
    finally:
        await a.close()
    data, value = _stored(path, _id, key)
    if key == "_version":
        assert ("_version" in data, type(value)) == (False, int)
    else:  # a model's document holds its other fields, as None
        assert ({k: v for k, v in data.items() if v is not None}, key in data, value) == (
            {"name": "y"},
            False,
            "v",
        )


@pytest.mark.asyncio
async def test_the_check_sends_no_statement_of_its_own(tmp_path):
    """The guard rides the write's own statement: the log holds what a write sent before, and
    a table another object made changes nothing SQLite checks, so nothing is learned again."""
    path = str(tmp_path / "cost.db")
    a = AsyncSQLerDB.on_disk(path)
    await a.connect()
    try:
        await a.ensure_table("t", promoted={"status": "TEXT"})
        with query_logger.capture() as steady:
            await a.insert_document("t", {"name": "x", "status": "s"})
        b = SQLerDB.on_disk(path)
        b._ensure_table("other")
        b.close()
        with query_logger.capture() as moved:
            await a.insert_document("t", {"name": "y", "status": "s"})
    finally:
        await a.close()
    assert [q.sql.split()[0] for q in steady] == ["INSERT"]
    assert [q.sql.split()[0] for q in moved] == ["INSERT"]
    assert _stored(path, 2, "status") == ({"name": "y"}, "s")


@pytest.mark.asyncio
async def test_a_cancelled_statement_is_not_caught_and_not_sent_again(tmp_path):
    """Only SQLite's refusal (``OperationalError``) is caught: a task cancelled while its
    statement runs is cancelled, and the statement is not sent again."""
    import asyncio

    from sqler.query import SQLerExpression

    slow = "(WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 3000000) SELECT count(*) FROM c) > 0"
    a = AsyncSQLerDB.on_disk(str(tmp_path / "cancel.db"))
    await a.connect()
    try:
        await a.insert_document("t", {"name": "x"})
        with query_logger.capture() as sent:
            task = asyncio.ensure_future(
                SceneRow.using(a).filter(SQLerExpression(slow, [])).count()
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        await a.close()
    assert [(q.sql.split()[0], q.error) for q in sent] == [
        ("SELECT", "CancelledError")
    ]  # sent once, nothing learned


@pytest.mark.asyncio
async def test_bulk_upsert_is_one_unit_a_failure_half_way_writes_nothing(tmp_path):
    """The run's inserts and its updates are one transaction of the object's: an
    ``_id`` that is no number fails after the insert was sent, and the insert is not kept,
    not even by the next write that commits."""
    path = str(tmp_path / "unit.db")
    a = AsyncSQLerDB.on_disk(path)
    await a.connect()
    try:
        await a.insert_document("t", {"name": "x"})
        with query_logger.capture() as sent:
            with pytest.raises(ValueError, match="invalid literal for int"):
                await a.bulk_upsert("t", [{"name": "new"}, {"name": "bad", "_id": "not a number"}])
        await a.insert_document("beside", {"name": "the next write"})
    finally:
        await a.close()
    assert [q.sql.split()[0] for q in sent] == ["CREATE", "INSERT"]
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT data FROM t ORDER BY _id").fetchall() == [('{"name":"x"}',)]
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["insert_many", "bulk_upsert"])
async def test_a_bulk_run_sends_the_check_once_and_its_rows_bare(tmp_path, way):
    path = str(tmp_path / "bulk.db")
    a = AsyncSQLerDB.on_disk(path)
    await a.connect()
    try:
        await a.insert_document("t", {"name": "x"})
        with query_logger.capture() as sent:
            ids = await getattr(a, way)("t", [{"name": f"r{i}"} for i in range(2500)])
    finally:
        await a.close()
    assert [q.sql.split()[0] for q in sent] == ["CREATE", "SELECT", "INSERT", "INSERT", "INSERT"]
    assert [q.sql for q in sent[2:] if "SELECT" in q.sql] == []
    assert (ids[0], ids[-1]) == (2, 2501)


@pytest.mark.asyncio
async def test_a_cancellation_after_the_check_of_a_bulk_run_writes_nothing(tmp_path):
    """The task is cancelled once its run's check has been answered: the cancellation is not
    caught, and the transaction the check and the rows share is rolled back."""
    import asyncio

    path = str(tmp_path / "cancel.db")
    a = AsyncSQLerDB.on_disk(path)
    await a.connect()
    task = None

    def cancel(entry):
        if entry.sql.startswith("SELECT CASE"):
            task.cancel()

    try:
        await a.insert_document("t", {"name": "x"})
        query_logger.add_callback(cancel)
        with query_logger.capture():
            task = asyncio.ensure_future(
                a.insert_many("t", [{"name": f"r{i}"} for i in range(2500)])
            )
            with pytest.raises(asyncio.CancelledError):
                await task
        depth = a.adapter._depth()
        await a.insert_document("beside", {"name": "the next write"})
    finally:
        query_logger.remove_callback(cancel)
        await a.close()
    conn = sqlite3.connect(path)
    try:
        assert (depth, conn.execute("SELECT count(*) FROM t").fetchone()) == (0, (1,))
    finally:
        conn.close()


async def _learned(path, *changes):
    """``a`` with ``t`` (row 1 ``{"name": "x", "status": "old"}``) learned, after another
    object made each change of ``changes``."""
    a = AsyncSQLerDB.on_disk(path)
    await a.connect()
    await a.insert_document("t", {"name": "x", "status": "old"})
    await a.find_document("t", 1)
    b = SQLerDB.on_disk(path)
    for change in changes:
        change(b)
    b.close()
    return a


@pytest.mark.asyncio
async def test_statements_at_once_after_another_object_promoted_each_return_with_the_column(
    tmp_path,
):
    """Each refused statement is built again when the names it was built from are not the
    table's, also when another task has learned the table by the time it looks."""
    import asyncio

    path = str(tmp_path / "once.db")
    a = await _learned(path, lambda b: b.ensure_table("t", promoted={"status": "TEXT"}))
    try:
        got = await asyncio.gather(
            a.find_document("t", 1),
            a.find_document("t", 1),
            a.insert_document("t", {"name": "y", "status": "v"}),
            a.find_document("t", 1),
            return_exceptions=True,
        )
    finally:
        await a.close()
    row = {"name": "x", "status": "old", "_id": 1}
    assert got == [row, row, 2, row]
    assert _stored(path, 2, "status") == ({"name": "y"}, "v")


@pytest.mark.asyncio
async def test_ten_tasks_after_two_changes_each_return_and_the_table_is_read_once(tmp_path):
    import asyncio

    path = str(tmp_path / "ten.db")
    a = await _learned(
        path,
        lambda b: b.ensure_table("t", promoted={"status": "TEXT"}),
        lambda b: b.ensure_table("t", promoted={"status": "TEXT", "prio": "TEXT"}),
    )
    try:
        with query_logger.capture() as sent:
            got = await asyncio.gather(
                *[a.find_document("t", 1) for _ in range(10)], return_exceptions=True
            )
    finally:
        await a.close()
    assert got == [{"name": "x", "status": "old", "prio": None, "_id": 1}] * 10
    assert len([q for q in sent if q.sql.startswith("PRAGMA")]) == 1


@pytest.mark.asyncio
async def test_a_cancellation_in_a_statement_sent_again_is_not_caught(tmp_path):
    import asyncio

    path = str(tmp_path / "cancel.db")
    a = await _learned(path, lambda b: b.ensure_table("t", promoted={"status": "TEXT"}))
    inserts = []
    task = None

    def cancel(entry):
        if entry.sql.startswith("INSERT"):
            inserts.append(entry.error)
            if len(inserts) == 2:
                task.cancel()

    query_logger.add_callback(cancel)
    try:
        with query_logger.capture():
            task = asyncio.ensure_future(a.insert_document("t", {"name": "y", "status": "v"}))
            with pytest.raises(asyncio.CancelledError):
                await task
        if (
            a.adapter._reclaims
        ):  # a connection left by an ended task is given back in the background
            await asyncio.wait_for(asyncio.gather(*a.adapter._reclaims), 5)
        in_use = a.pool_stats().in_use_connections
        await a.insert_document("beside", {"name": "the next write"})
    finally:
        query_logger.remove_callback(cancel)
        await a.close()
    # sent twice, never a third time; the cancellation leaves the pool whole
    assert (len(inserts), inserts[0], inserts[1], in_use) == (
        2,
        "sub-select returns 3 columns - expected 2",
        None,
        0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "way, n, refused", [("insert_many", 1, "INSERT"), ("bulk_upsert", 3, "SELECT")]
)
async def test_a_bulk_run_in_the_callers_unit_after_a_promotion_lands_every_row_in_the_column(
    tmp_path, way, n, refused
):
    """Async twin of the sync test of that name: the run's guarded statement is refused, ``a``
    learns ``t`` again and builds the run again; the caller's earlier write is kept."""
    path = str(tmp_path / "unit.db")
    a = await _learned(path, lambda b: b.ensure_table("t", promoted={"status": "TEXT"}))
    try:
        with query_logger.capture() as sent:
            async with a.transaction():
                await a.insert_document("beside", {"name": "the caller's own row"})
                ids = await getattr(a, way)(
                    "t", [{"name": f"r{i}", "status": "v"} for i in range(n)]
                )
        beside = await a.find_document("beside", 1)
    finally:
        await a.close()
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            f"SELECT data, status FROM t WHERE _id IN ({','.join('?' * len(ids))})", ids
        ).fetchall()
    finally:
        conn.close()
    assert [(q.sql.split()[0], q.error) for q in sent if q.error] == [
        (refused, "sub-select returns 3 columns - expected 2")
    ]
    assert rows == [(f'{{"name":"r{i}"}}', "v") for i in range(n)]
    assert beside == {"name": "the caller's own row", "_id": 1}


@pytest.mark.asyncio
async def test_a_bulk_upsert_that_fails_leaves_the_callers_documents_as_they_were_passed(tmp_path):
    a = AsyncSQLerDB.on_disk(str(tmp_path / "ids.db"))
    await a.connect()
    docs = [{"name": "new"}, {"name": "bad", "_id": "not a number"}]
    try:
        await a.insert_document("t", {"name": "x"})
        with pytest.raises(ValueError, match="invalid literal for int"):
            await a.bulk_upsert("t", docs)
        failed = [dict(doc) for doc in docs]
        ids = await a.bulk_upsert("t", [docs[0]])
    finally:
        await a.close()
    assert failed == [{"name": "new"}, {"name": "bad", "_id": "not a number"}]
    assert (ids, docs[0]) == ([2], {"name": "new", "_id": 2})


@pytest.mark.asyncio
async def test_the_callers_own_mistake_after_a_pending_statement_ends_the_unit(tmp_path):
    from sqler.query import SQLerExpression

    path = str(tmp_path / "mistake.db")
    a = await _learned(path)
    try:
        cur = await a.adapter.execute('INSERT INTO t (data) VALUES (\'{"name": "pending"}\');')
        await cur.close()
        with query_logger.capture() as sent:
            with pytest.raises(sqlite3.OperationalError, match="^no such column: nocolumn$"):
                await SceneRow.using(a).filter(SQLerExpression("nocolumn = 1", [])).all()
        in_use = a.pool_stats().in_use_connections
    finally:
        await a.close()
    assert [q.sql.split()[0] for q in sent] == ["SELECT", "PRAGMA"]
    assert (in_use, sqlite3.connect(path).execute("SELECT count(*) FROM t").fetchone()) == (0, (1,))


@pytest.mark.asyncio
async def test_a_new_row_in_a_remembered_table_sends_its_insert_alone(tmp_path):
    path = str(tmp_path / "steady.db")
    a = await _learned(path)
    try:
        with query_logger.capture() as sent:
            await a.insert_document("t", {"name": "y"})
            await SceneRow(name="z").save(db=a)
    finally:
        await a.close()
    assert [q.sql.split()[0] for q in sent] == ["INSERT", "INSERT"]
    assert (_stored(path, 2, "data")[0], _stored(path, 3, "data")[0]["name"]) == (
        {"name": "y"},
        "z",
    )


@pytest.mark.asyncio
async def test_a_new_row_in_a_remembered_table_another_object_dropped_makes_the_table_again(
    tmp_path,
):
    """The insert is sent with no ``CREATE TABLE`` before it and SQLite refuses it; the table
    is found gone and forgotten, and the insert built again makes it."""
    path = str(tmp_path / "gone.db")
    a = await _learned(
        path, lambda b: (b.adapter.execute("DROP TABLE t;"), b.adapter.auto_commit())
    )
    try:
        with query_logger.capture() as sent:
            _id = await a.insert_document("t", {"name": "y"})
    finally:
        await a.close()
    assert [q.sql.split()[0] for q in sent] == ["INSERT", "PRAGMA", "CREATE", "PRAGMA", "INSERT"]
    assert (_id, _stored(path, _id, "data")) == (1, ({"name": "y"}, '{"name":"y"}'))
