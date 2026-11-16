"""Async JSONL export and import: same bytes, same ImportResult, same rows as the sync pair."""

import asyncio
import json
import sys
import threading
import warnings
from dataclasses import dataclass

import pytest
import pytest_asyncio
from pydantic import field_validator

from sqler import (
    AsyncSQLerLiteModel,
    AsyncSQLerLiteSafeModel,
    AsyncSQLerModel,
    AsyncSQLerSafeModel,
    SQLerDB,
    SQLerLiteModel,
    SQLerLiteSafeModel,
    SQLerModel,
    SQLerSafeModel,
    async_export_jsonl,
    async_import_jsonl,
    export_jsonl,
    import_jsonl,
)
from sqler.db.async_db import AsyncSQLerDB

pytestmark = pytest.mark.asyncio

# ── model pairs: (async base, sync base) ────────────────────────────────────


def _pydantic_item(base):
    class Item(base):
        __tablename__ = "items"
        name: str
        qty: int = 0

        @field_validator("qty")
        @classmethod
        def _qty(cls, v):
            if v < 0:
                raise ValueError("qty must not be negative")
            return v

    return Item


def _lite_item(base):
    @dataclass
    class Item(base):
        __tablename__ = "items"
        name: str = ""
        qty: int = 0

        def __post_init__(self):
            if not isinstance(self.qty, int) or self.qty < 0:
                raise ValueError("qty must not be negative")

    return Item


def _pydantic_pair(async_base, sync_base):
    return _pydantic_item(async_base), _pydantic_item(sync_base)


def _lite_pair(async_base, sync_base):
    return _lite_item(async_base), _lite_item(sync_base)


PAIRS = {
    "pydantic": lambda: _pydantic_pair(AsyncSQLerModel, SQLerModel),
    "pydantic_safe": lambda: _pydantic_pair(AsyncSQLerSafeModel, SQLerSafeModel),
    "lite": lambda: _lite_pair(AsyncSQLerLiteModel, SQLerLiteModel),
    "lite_safe": lambda: _lite_pair(AsyncSQLerLiteSafeModel, SQLerLiteSafeModel),
}


@pytest.fixture(params=list(PAIRS))
def pair(request):
    return PAIRS[request.param]()


@pytest_asyncio.fixture
async def adb():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def sdb():
    db = SQLerDB.in_memory()
    try:
        yield db
    finally:
        db.close()


def _bind(model, db):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.set_db(db)


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def _jl(*objs):
    return "".join(json.dumps(o) + "\n" for o in objs)


async def _arows(A):
    out = await A.query().all()
    return sorted((r._id, r.name, r.qty) for r in out)


def _srows(S):
    return sorted((r._id, r.name, r.qty) for r in S.query().all())


def _result_tuple(r):
    return (r.count, r.succeeded, r.failed, r.errors)


async def _both(pair, adb, sdb, tmp_path, text, **kw):
    A, S = pair
    _bind(A, adb)
    _bind(S, sdb)
    p = tmp_path / "in.jsonl"
    _write(p, text)
    sres = import_jsonl(S, p, **kw)
    ares = await async_import_jsonl(A, p, **kw)
    return A, S, sres, ares


# ── item 1: export ──────────────────────────────────────────────────────────


async def test_aiofiles_absent_export_import_run(adb, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "aiofiles", None)  # import raises ImportError
    A, _ = _pydantic_pair(AsyncSQLerModel, SQLerModel)
    _bind(A, adb)
    await A(name="a", qty=1).save()
    p = tmp_path / "o.jsonl"
    res = await async_export_jsonl(A, p)
    assert res.count == 1
    ires = await async_import_jsonl(A, p)
    assert ires.succeeded == 1


@pytest.mark.parametrize("include_id", [True, False])
@pytest.mark.parametrize("fields", [None, ["name"]])
async def test_export_bytes_equal_sync(pair, adb, sdb, tmp_path, include_id, fields):
    A, S = pair
    _bind(A, adb)
    _bind(S, sdb)
    for n, q in [("a", 1), ("bé 日本", 2), ("c", 3)]:
        await A(name=n, qty=q).save()
        S(name=n, qty=q).save()
    sp, ap = tmp_path / "s.jsonl", tmp_path / "a.jsonl"
    sres = export_jsonl(S, sp, include_id=include_id, fields=fields)
    ares = await async_export_jsonl(A, ap, include_id=include_id, fields=fields)
    assert ap.read_bytes() == sp.read_bytes()
    assert (ares.count, ares.size_bytes) == (sres.count, sres.size_bytes)


async def test_export_file_work_off_loop_thread(adb, tmp_path, monkeypatch):
    A, _ = _pydantic_pair(AsyncSQLerModel, SQLerModel)
    _bind(A, adb)
    await A(name="a", qty=1).save()
    loop_thread = threading.get_ident()
    seen = []
    import builtins

    real_open = builtins.open

    def spy(file, *a, **k):
        if str(file).endswith("o.jsonl"):
            seen.append(threading.get_ident())
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    await async_export_jsonl(A, tmp_path / "o.jsonl")
    await async_import_jsonl(A, tmp_path / "o.jsonl")
    assert seen and all(t != loop_thread for t in seen)


async def test_export_empty_table(pair, adb, sdb, tmp_path):
    A, S = pair
    _bind(A, adb)
    _bind(S, sdb)
    gone = await A(name="gone").save()
    await gone.delete()
    S(name="gone").save().delete()
    sp, ap = tmp_path / "s.jsonl", tmp_path / "a.jsonl"
    export_jsonl(S, sp)
    res = await async_export_jsonl(A, ap)
    assert res.count == 0 and res.size_bytes == 0
    assert ap.read_bytes() == sp.read_bytes() == b""


# ── item 2: import parity ───────────────────────────────────────────────────


@pytest.mark.parametrize("update_existing", [False, True])
@pytest.mark.parametrize("skip_errors", [False, True])
async def test_import_parity(pair, adb, sdb, tmp_path, update_existing, skip_errors):
    A, S = pair
    _bind(A, adb)
    _bind(S, sdb)
    await A(name="seed", qty=1).save()
    S(name="seed", qty=1).save()
    text = (
        _jl({"_id": 1, "name": "upd", "qty": 5})
        + "\n   \n"
        + "not json\n"
        + _jl({"name": "bad", "qty": -1})
        + _jl([1, 2])
        + _jl({"_id": 999, "name": "ghost", "qty": 9})
        + _jl({"name": "new", "qty": 7})
    )
    p = tmp_path / "in.jsonl"
    _write(p, text)
    kw = dict(skip_errors=skip_errors, update_existing=update_existing)
    sres = import_jsonl(S, p, **kw)
    ares = await async_import_jsonl(A, p, **kw)
    assert _result_tuple(ares) == _result_tuple(sres)
    assert await _arows(A) == _srows(S)


async def test_import_stops_at_bad_line_later_not_imported(pair, adb, sdb, tmp_path):
    A, S, sres, ares = await _both(
        pair, adb, sdb, tmp_path, _jl({"name": "a"}) + "oops\n" + _jl({"name": "b"})
    )
    assert (ares.count, ares.succeeded, ares.failed) == (2, 1, 1)
    assert ares.errors[0]["line"] == 2 and ares.errors[0]["data"] == "oops"
    assert ares.errors[0]["error_type"] == "JSONDecodeError"
    assert [r[1] for r in await _arows(A)] == ["a"]
    assert _result_tuple(ares) == _result_tuple(sres)


async def test_import_skip_errors_continues(pair, adb, sdb, tmp_path):
    A, S, sres, ares = await _both(
        pair,
        adb,
        sdb,
        tmp_path,
        _jl({"name": "a"}) + "oops\n" + _jl({"name": "b"}),
        skip_errors=True,
    )
    assert (ares.count, ares.succeeded, ares.failed) == (3, 2, 1)
    assert [r[1] for r in await _arows(A)] == ["a", "b"]
    assert _result_tuple(ares) == _result_tuple(sres)


async def test_import_empty_and_blank_only(pair, adb, sdb, tmp_path):
    A, S, sres, ares = await _both(pair, adb, sdb, tmp_path, "\n\n  \n")
    assert (ares.count, ares.succeeded, ares.failed, ares.errors) == (0, 0, 0, [])
    assert _result_tuple(ares) == _result_tuple(sres)


async def test_import_list_line(pair, adb, sdb, tmp_path):
    A, S, sres, ares = await _both(pair, adb, sdb, tmp_path, "[1, 2]\n")
    assert ares.failed == 1 and ares.errors[0]["line"] == 1
    assert _result_tuple(ares) == _result_tuple(sres)


async def test_import_validation_failure(pair, adb, sdb, tmp_path):
    A, S, sres, ares = await _both(pair, adb, sdb, tmp_path, _jl({"name": "x", "qty": -3}))
    assert (ares.succeeded, ares.failed) == (0, 1)
    assert _result_tuple(ares) == _result_tuple(sres)


async def test_import_unknown_id_creates(pair, adb, sdb, tmp_path):
    A, S, sres, ares = await _both(
        pair, adb, sdb, tmp_path, _jl({"_id": 42, "name": "n", "qty": 1}), update_existing=True
    )
    assert ares.succeeded == 1
    assert await _arows(A) == _srows(S)


async def test_import_one_megabyte_line(pair, adb, sdb, tmp_path):
    big = "x" * (1024 * 1024)
    A, S, sres, ares = await _both(pair, adb, sdb, tmp_path, _jl({"name": big, "qty": 1}))
    assert ares.succeeded == 1
    assert (await A.query().all())[0].name == big
    assert _result_tuple(ares) == _result_tuple(sres)


async def test_import_bad_line_truncated_to_200(pair, adb, sdb, tmp_path):
    A, S, sres, ares = await _both(pair, adb, sdb, tmp_path, "z" * 1000 + "\n")
    assert len(ares.errors[0]["data"]) == 200
    assert _result_tuple(ares) == _result_tuple(sres)


async def test_import_path_with_space(pair, adb, sdb, tmp_path):
    A, S = pair
    _bind(A, adb)
    d = tmp_path / "a dir"
    d.mkdir()
    p = d / "my file.jsonl"
    _write(p, _jl({"name": "a"}))
    res = await async_import_jsonl(A, str(p))
    assert res.succeeded == 1
    out = await async_export_jsonl(A, d / "out file.jsonl")
    assert out.count == 1


async def test_import_missing_file_raises_like_sync(pair, adb, sdb, tmp_path):
    A, S = pair
    _bind(A, adb)
    _bind(S, sdb)
    with pytest.raises(FileNotFoundError):
        import_jsonl(S, tmp_path / "nope.jsonl")
    with pytest.raises(FileNotFoundError):
        await async_import_jsonl(A, tmp_path / "nope.jsonl")


async def test_import_non_utf8_raises_like_sync(pair, adb, sdb, tmp_path):
    A, S = pair
    _bind(A, adb)
    _bind(S, sdb)
    p = tmp_path / "bin.jsonl"
    p.write_bytes(b'{"name": "\xff\xfe"}\n')
    with pytest.raises(UnicodeDecodeError):
        import_jsonl(S, p)
    with pytest.raises(UnicodeDecodeError):
        await async_import_jsonl(A, p)


async def test_import_transform_raises_recorded(pair, adb, sdb, tmp_path):
    def boom(rec):
        if rec["name"] == "b":
            raise RuntimeError("no b")
        return rec

    A, S, sres, ares = await _both(
        pair,
        adb,
        sdb,
        tmp_path,
        _jl({"name": "a"}, {"name": "b"}, {"name": "c"}),
        transform=boom,
        skip_errors=True,
    )
    assert ares.errors[0]["error_type"] == "RuntimeError" and ares.errors[0]["error"] == "no b"
    assert _result_tuple(ares) == _result_tuple(sres)
    assert [r[1] for r in await _arows(A)] == ["a", "c"]


# ── item 3: round trip ──────────────────────────────────────────────────────


async def test_round_trip_ids_kept(pair, tmp_path):
    A, _ = pair
    src = AsyncSQLerDB.in_memory(shared=False)
    dst = AsyncSQLerDB.in_memory(shared=False)
    await src.connect()
    await dst.connect()
    try:
        _bind(A, src)
        for i in (1, 2, 3):
            await A(name=f"n{i}", qty=i).save()
        # make ids non-contiguous
        gone = (await A.query().all())[1]
        await gone.delete()
        want = await _arows(A)
        p = tmp_path / "rt.jsonl"
        await async_export_jsonl(A, p)
        _bind(A, dst)
        res = await async_import_jsonl(A, p, update_existing=True)
        assert (res.count, res.succeeded, res.failed) == (2, 2, 0)
        assert [(n, q) for _, n, q in await _arows(A)] == [(n, q) for _, n, q in want]
    finally:
        await src.close()
        await dst.close()


# ── cancellation ────────────────────────────────────────────────────────────


async def test_import_cancelled_while_real_save_waits_on_lock(tmp_path, monkeypatch):
    import builtins
    import sqlite3

    db_path = tmp_path / "lock.db"
    db = AsyncSQLerDB.on_disk(str(db_path))
    await db.connect()
    A, _ = _pydantic_pair(AsyncSQLerModel, SQLerModel)
    _bind(A, db)
    await A(name="seed").save()  # the table exists; the import starts after it
    await (await A.query().all())[0].delete()
    p = tmp_path / "c.jsonl"
    _write(p, _jl(*[{"name": f"n{i}"} for i in range(5)]))

    # a second, real connection takes the write lock when the third line is read
    locker = sqlite3.connect(str(db_path), isolation_level=None)

    def take_lock_on_third(rec):
        if rec["name"] == "n2":
            locker.execute("BEGIN IMMEDIATE")
        return rec

    opened = []
    real_open = builtins.open

    def spy(file, *a, **k):
        f = real_open(file, *a, **k)
        if str(file).endswith("c.jsonl"):
            opened.append(f)
        return f

    monkeypatch.setattr(builtins, "open", spy)
    try:
        task = asyncio.create_task(async_import_jsonl(A, p, transform=take_lock_on_third))
        # the third save is waiting on the database: its connection is out of the pool
        for _ in range(500):
            if db.pool_stats().in_use_connections > 0 and locker.in_transaction:
                break
            await asyncio.sleep(0.01)
        assert locker.in_transaction
        assert db.pool_stats().in_use_connections == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        locker.execute("ROLLBACK")
        # the cancel also releases the connection; give the reclaim task a bounded moment
        for _ in range(500):
            if db.pool_stats().in_use_connections == 0:
                break
            await asyncio.sleep(0.01)
        assert [r.name for r in await A.query().all()] == ["n0", "n1"]
        st = db.pool_stats()
        assert st.in_use_connections == 0
        assert st.available_connections == st.total_connections
        assert len(opened) == 1 and opened[0].closed
    finally:
        locker.close()
        await db.close()


SAFE = ["pydantic_safe", "lite_safe"]


@pytest.mark.parametrize("name", SAFE)
async def test_import_keeps_stored_version_and_new_row_starts_fresh(name, adb, sdb, tmp_path):
    A, S = PAIRS[name]()
    _bind(A, adb)
    _bind(S, sdb)
    await A(name="a", qty=1).save()
    S(name="a", qty=1).save()
    a0 = await A.from_id(1)
    s0 = S.from_id(1)
    # move the stored versions off zero so "stored + 1" differs from "file + 1" and from 0
    a0.qty = 5
    await a0.save()
    s0.qty = 5
    s0.save()
    a_before = (await A.from_id(1))._version
    s_before = S.from_id(1)._version
    assert a_before == s_before == 1
    p = tmp_path / "v.jsonl"
    _write(
        p,
        _jl(
            {"_id": 1, "_version": 7, "name": "b", "qty": 2},
            {"name": "new", "_version": 7, "qty": 3},
        ),
    )
    sres = import_jsonl(S, p, update_existing=True)
    ares = await async_import_jsonl(A, p, update_existing=True)
    assert _result_tuple(ares) == _result_tuple(sres)
    assert (ares.succeeded, ares.failed) == (2, 0)
    assert await _arows(A) == _srows(S) == [(1, "b", 2), (2, "new", 3)]
    assert (await A.from_id(1))._version == a_before + 1
    assert S.from_id(1)._version == s_before + 1
    fresh_a = await A(name="x").save()
    fresh_s = S(name="x").save()
    assert (await A.from_id(2))._version == fresh_a._version
    assert S.from_id(2)._version == fresh_s._version


async def test_import_ignores_exported_version_rows(pair, adb, sdb, tmp_path):
    A, S = pair
    _bind(A, adb)
    _bind(S, sdb)
    await A(name="a", qty=1).save()
    S(name="a", qty=1).save()
    p = tmp_path / "v.jsonl"
    _write(p, _jl({"_id": 1, "_version": 7, "name": "b", "qty": 2}))
    sres = import_jsonl(S, p, update_existing=True)
    ares = await async_import_jsonl(A, p, update_existing=True)
    assert (ares.succeeded, ares.failed) == (1, 0)
    assert _result_tuple(ares) == _result_tuple(sres)
    assert await _arows(A) == _srows(S) == [(1, "b", 2)]


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"include_id": False}, {"include_id": False, "fields": ["name"]}, {"fields": ["name"]}],
    ids=["defaults", "no_id", "fields_no_id", "fields_with_id"],
)
async def test_export_of_a_dropped_table_gives_what_an_empty_table_gives(adb, tmp_path, kwargs):
    class AKept(AsyncSQLerModel):
        name: str = ""

    class AGone(AsyncSQLerModel):
        name: str = ""

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        AKept.set_db(adb, "akept_empty")
        AGone.set_db(adb, "agone_table")
    await adb.ensure_table("akept_empty")
    await adb.ensure_table("agone_table")
    await AGone(name="a").save()
    await adb.adapter.execute("DROP TABLE agone_table")
    await adb.adapter.commit()

    ref_path, out_path = tmp_path / "ref.jsonl", tmp_path / "out.jsonl"
    reference = await async_export_jsonl(AKept, ref_path, **kwargs)
    result = await async_export_jsonl(AGone, out_path, **kwargs)

    assert (result.count, result.format, out_path.read_text()) == (
        reference.count,
        reference.format,
        ref_path.read_text(),
    )
    cur = await adb.adapter.execute("SELECT name FROM sqlite_master WHERE type='table'")
    assert "agone_table" not in {r[0] for r in await cur.fetchall()}
