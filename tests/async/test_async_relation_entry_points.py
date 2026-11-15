"""What each async way into a row returns for its references, pinned on every async base.

The async twin of ``tests/test_relation_entry_points.py``: the same fixture, the same
five ways in (``from_id``, ``refresh``, ``from_ids``, ``query().all()``,
``query().first()``), described field by field and compared with the base's answer
and the SELECTs it ran.
"""

from __future__ import annotations

import dataclasses
import json
import typing
import warnings
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest
import pytest_asyncio
from pairwise import all_pairs
from pydantic import Field as PydField
from pydantic import PrivateAttr, field_validator, model_validator
from sqler import AsyncSQLerDB, F
from sqler.adapter import AsyncSQLiteAdapter
from sqler.exceptions import StaleVersionError
from sqler.logging import query_logger
from sqler.models import AsyncSQLerModel, AsyncSQLerQuerySet, AsyncSQLerSafeModel, _refs
from sqler.models._table_names import _ref_table
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel
from sqler.query.async_query import AsyncSQLerQuery


class APd_Child(AsyncSQLerModel):
    __tablename__ = "arep_pd_child"
    name: str = ""
    other: Any = None


class APd_Parent(AsyncSQLerModel):
    __tablename__ = "arep_pd_parent"
    name: str = ""
    one: Optional[APd_Child] = None
    many: list[APd_Child] = PydField(default_factory=list)
    loose: Any = None
    deep: Any = None
    cyc: Any = None
    me: Any = None
    gone: Any = None
    nowhere: Any = None


class ASf_Child(AsyncSQLerSafeModel):
    __tablename__ = "arep_sf_child"
    name: str = ""
    other: Any = None


class ASf_Parent(AsyncSQLerSafeModel):
    __tablename__ = "arep_sf_parent"
    name: str = ""
    one: Optional[ASf_Child] = None
    many: list[ASf_Child] = PydField(default_factory=list)
    loose: Any = None
    deep: Any = None
    cyc: Any = None
    me: Any = None
    gone: Any = None
    nowhere: Any = None


@dataclass
class ALt_Child(AsyncSQLerLiteModel):
    __tablename__ = "arep_lt_child"
    name: str = ""
    other: Any = None


@dataclass
class ALt_Parent(AsyncSQLerLiteModel):
    __tablename__ = "arep_lt_parent"
    name: str = ""
    one: Optional[ALt_Child] = None
    many: list[ALt_Child] = field(default_factory=list)
    loose: Any = None
    deep: Any = None
    cyc: Any = None
    me: Any = None
    gone: Any = None
    nowhere: Any = None


@dataclass
class ALs_Child(AsyncSQLerLiteSafeModel):
    __tablename__ = "arep_ls_child"
    name: str = ""
    other: Any = None


@dataclass
class ALs_Parent(AsyncSQLerLiteSafeModel):
    __tablename__ = "arep_ls_parent"
    name: str = ""
    one: Optional[ALs_Child] = None
    many: list[ALs_Child] = field(default_factory=list)
    loose: Any = None
    deep: Any = None
    cyc: Any = None
    me: Any = None
    gone: Any = None
    nowhere: Any = None


BASES = {
    "pydantic": (APd_Child, APd_Parent),
    "safe": (ASf_Child, ASf_Parent),
    "lite": (ALt_Child, ALt_Parent),
    "lite-safe": (ALs_Child, ALs_Parent),
}
WAYS = ["from_id", "refresh", "from_ids", "all", "first"]
NOBODY = "arep_nobody"


def ref(table: str, rid: Any) -> dict:
    return {"_table": table, "_id": rid}


async def bind(db: AsyncSQLerDB, child: type, parent: type) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        child.set_db(db)
        parent.set_db(db)
    safe = issubclass(child, (AsyncSQLerSafeModel, AsyncSQLerLiteSafeModel))
    for cls in (child, parent):
        if safe:
            await db._ensure_versioned_table(cls.__tablename__)
        else:
            await db._ensure_table(cls.__tablename__)


async def build(db: AsyncSQLerDB, child: type, parent: type) -> int:
    """The fixture rows; returns the parent's id.

    c1 plain; c2 -> c1 (two levels down); c3 deleted; c4 <-> c5 (cycle of two);
    c6 -> c6 (itself); ``arep_nobody`` row 1 in a table no class owns.
    """
    await bind(db, child, parent)
    ct = child.__tablename__
    c1 = await db.insert_document(ct, {"name": "c1", "other": None})
    c2 = await db.insert_document(ct, {"name": "c2", "other": ref(ct, c1)})
    c3 = await db.insert_document(ct, {"name": "c3", "other": None})
    c4 = await db.insert_document(ct, {"name": "c4", "other": None})
    c5 = await db.insert_document(ct, {"name": "c5", "other": ref(ct, c4)})
    await db.upsert_document(ct, c4, {"name": "c4", "other": ref(ct, c5)})
    c6 = await db.insert_document(ct, {"name": "c6", "other": None})
    await db.upsert_document(ct, c6, {"name": "c6", "other": ref(ct, c6)})
    await db.delete_document(ct, c3)
    nb = await db.insert_document(NOBODY, {"name": "nobody"})
    return await db.insert_document(
        parent.__tablename__,
        {
            "name": "p",
            "one": ref(ct, c1),
            "many": [ref(ct, c1), ref(ct, c2)],
            "loose": ref(ct, c1),
            "deep": ref(ct, c2),
            "cyc": ref(ct, c4),
            "me": ref(ct, c6),
            "gone": ref(ct, c3),
            "nowhere": ref(NOBODY, nb),
        },
    )


def describe(x: Any, depth: int = 4) -> Any:
    """A plain picture of a value: type, ``_id``, ``_version`` and fields, to ``depth``."""
    if x is None or isinstance(x, (str, int, float, bool)):
        return x
    if isinstance(x, list):
        return [describe(v, depth) for v in x]
    if isinstance(x, dict):
        if set(x) == {"_table", "_id"}:
            return ("ref", x["_table"], x["_id"])
        if depth <= 0:
            return ("dict", x.get("_id"), "...")
        body = {k: describe(v, depth - 1) for k, v in sorted(x.items()) if k != "_id"}
        return ("dict", x.get("_id"), body)
    kind = type(x).__name__.split("_")[-1]
    head = (kind, getattr(x, "_id", "-"), getattr(x, "_version", "-"))
    if depth <= 0:
        return (*head, "...")
    if dataclasses.is_dataclass(x):
        names = [f.name for f in dataclasses.fields(x)]
    else:
        names = list(type(x).model_fields)
    return (*head, {n: describe(getattr(x, n), depth - 1) for n in names})


async def selects_of(db: AsyncSQLerDB, coro_fn):
    traced: list[str] = []
    conns = list(db.adapter._all_conns)
    for conn in conns:
        await conn.set_trace_callback(traced.append)
    try:
        result = await coro_fn()
    finally:
        for conn in conns:
            await conn.set_trace_callback(None)
    return result, [s for s in traced if s.lstrip().upper().startswith("SELECT")]


async def run_way(db: AsyncSQLerDB, parent: type, pid: int, way: str):
    """``(instance, selects)`` of reading the parent row one way."""
    if way == "refresh":
        inst = await parent.query().resolve(False).first()
        return await selects_of(db, inst.refresh)

    async def from_ids():
        return (await parent.from_ids([pid]))[0]

    async def all_():
        return (await parent.query().all())[0]

    action = {
        "from_id": lambda: parent.from_id(pid),
        "from_ids": from_ids,
        "all": all_,
        "first": lambda: parent.query().first(),
    }[way]
    return await selects_of(db, action)


@pytest_asyncio.fixture
async def make_db(tmp_path):
    dbs: list[AsyncSQLerDB] = []

    async def make(name: str = "r.db") -> AsyncSQLerDB:
        db = AsyncSQLerDB(AsyncSQLiteAdapter(str(tmp_path / name), pool_size=2))
        await db.connect()
        dbs.append(db)
        return db

    yield make
    for db in dbs:
        await db.close()


async def _bound(make_db, base: str):
    child, parent = BASES[base]
    db = await make_db()
    await bind(db, child, parent)
    return db, child, parent, child.__tablename__


# ---- item 1: every way in, every base -----------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
async def test_each_way_in_returns_what_the_base_returned(make_db, base, way):
    child, parent = BASES[base]
    db = await make_db()
    pid = await build(db, child, parent)
    got, sel = await run_way(db, parent, pid, way)
    want_fields, want_selects = EXPECTED[(base, way)]
    picture = describe(got)
    assert picture[:2] == ("Parent", pid)
    assert picture[3] == want_fields
    assert len(sel) == want_selects


# ---- hostile inputs -----------------------------------------------------------------

BAD_REFS = [
    pytest.param({"_table": "TBL", "_id": True}, id="id-true"),
    pytest.param({"_table": "TBL", "_id": "1"}, id="id-str"),
    pytest.param({"_table": "TBL", "_id": 1.0}, id="id-float"),
    pytest.param({"_table": 5, "_id": 1}, id="table-int"),
    pytest.param({"_table": None, "_id": 1}, id="table-none"),
    pytest.param({"_table": ["TBL"], "_id": 1}, id="table-list"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(*all_pairs(("base", sorted(BASES)), ("bad", BAD_REFS), ("way", WAYS)))
async def test_unusable_reference_is_left_as_it_is_and_never_sent_to_sql(make_db, base, bad, way):
    db, child, parent, ct = await _bound(make_db, base)
    await db.insert_document(ct, {"name": "c1"})
    value = {k: (ct if v == "TBL" else [ct] if v == ["TBL"] else v) for k, v in bad.items()}
    pid = await db.insert_document(parent.__tablename__, {"name": "p", "loose": value})
    got, sel = await run_way(db, parent, pid, way)
    assert got.loose == value
    assert [s for s in sel if ct in s] == []


def _ids_in(statement: str) -> list[str]:
    start = statement.index("IN (") + 4
    return [p.strip() for p in statement[start : statement.index(")", start)].split(",")]


@pytest.mark.asyncio
@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
async def test_the_same_reference_twice_in_one_row_is_read_once(make_db, base, way):
    db, child, parent, ct = await _bound(make_db, base)
    c1 = await db.insert_document(ct, {"name": "c1"})
    pid = await db.insert_document(
        parent.__tablename__, {"name": "p", "one": ref(ct, c1), "many": [ref(ct, c1)]}
    )
    got, sel = await run_way(db, parent, pid, way)
    reads = [s for s in sel if f"FROM {ct} " in s and " IN (" in s]
    assert len(reads) == 1
    assert _ids_in(reads[0]) == [str(c1)]
    assert (got.one.name, [c.name for c in got.many]) == ("c1", ["c1"])


@pytest.mark.asyncio
@pytest.mark.parametrize("base", sorted(BASES))
async def test_the_same_reference_in_two_rows_of_one_all_is_read_once(make_db, base):
    db, child, parent, ct = await _bound(make_db, base)
    c1 = await db.insert_document(ct, {"name": "c1"})
    for n in ("p1", "p2"):
        await db.insert_document(parent.__tablename__, {"name": n, "one": ref(ct, c1)})
    got, sel = await selects_of(db, lambda: parent.query().all())
    reads = [s for s in sel if f"FROM {ct} " in s]
    assert len(reads) == 1
    assert _ids_in(reads[0]) == [str(c1)]
    assert [g.one.name for g in got] == ["c1", "c1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
async def test_a_thousand_references_into_one_table_are_one_statement(make_db, base, way):
    db, child, parent, ct = await _bound(make_db, base)
    ids = await db.insert_many(ct, [{"name": f"c{i}"} for i in range(1000)])
    pid = await db.insert_document(
        parent.__tablename__, {"name": "p", "many": [ref(ct, i) for i in ids]}
    )
    got, sel = await run_way(db, parent, pid, way)
    reads = [s for s in sel if f"FROM {ct} " in s]
    assert len(reads) == 1
    assert len(got.many) == 1000
    assert (got.many[999].name, got.many[999]._id) == ("c999", ids[999])


@pytest.mark.asyncio
@pytest.mark.parametrize("base", sorted(BASES))
async def test_a_chain_seven_rows_long_through_all_is_followed_to_its_end(make_db, base):
    n_cls = TYPED[base][3]
    db = await make_db()
    await bind(db, n_cls, n_cls)
    nt = n_cls.__tablename__
    prev = None
    ids = []
    for i in range(7):
        prev = await db.insert_document(
            nt, {"name": f"n{i}", "one": None if prev is None else ref(nt, prev)}
        )
        ids.append(prev)
    # n6 -> n5 -> n4 -> n3 -> n2 -> n1 -> n0
    node = (await n_cls.query().filter(F("_id") == ids[6]).all())[0]
    names = []
    while node is not None:
        names.append((node._id, node.name))
        node = node.one
    assert names == [(ids[i], f"n{i}") for i in range(6, -1, -1)]


async def _tables(db: AsyncSQLerDB) -> set[str]:
    cur = await db.adapter.execute("SELECT name FROM sqlite_master WHERE type='table';")
    try:
        rows = await cur.fetchall()
    finally:
        await cur.close()
    return {r[0] for r in rows}


@pytest.mark.asyncio
@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
async def test_a_reference_into_a_dropped_table_of_a_bound_class_reads_as_none(
    make_db, base, way
):
    db, child, parent, ct = await _bound(make_db, base)
    c1 = await db.insert_document(ct, {"name": "c1"})
    pid = await db.insert_document(parent.__tablename__, {"name": "p", "one": ref(ct, c1)})
    await db.adapter.execute(f"DROP TABLE {ct};")
    await db.adapter.commit()
    got, _ = await run_way(db, parent, pid, way)
    assert got.one is None
    assert ct not in await _tables(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["from_ids", "all", "first"])
@pytest.mark.parametrize("base", ["safe", "lite-safe"])
async def test_a_reference_into_a_versioned_table_that_was_never_made_reads_as_none(
    make_db, base, way
):
    """The child class is bound, its table was never made, and the database remembers nothing
    about it, where a dropped table is still remembered."""
    child, parent = BASES[base]
    db = await make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        child.set_db(db)
        parent.set_db(db)
    await db._ensure_versioned_table(parent.__tablename__)
    ct = child.__tablename__
    assert ct not in await _tables(db)
    pid = await db.insert_document(parent.__tablename__, {"name": "p", "one": ref(ct, 1)})
    got, _ = await run_way(db, parent, pid, way)
    assert got.one is None
    assert ct not in await _tables(db)


# ---- hostile inputs at a typed position ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
async def test_a_reference_shaped_dict_that_is_not_usable_is_not_walked_into(make_db, base, way):
    """``{"_table", "_id"}`` with an id that is not one is left as it is, whole, and no base
    turns it into a model."""
    db, child, parent, ct = await _bound(make_db, base)
    c1 = await db.insert_document(ct, {"name": "c1"})
    shell = {"_table": ct, "_id": "x", "inner": ref(ct, c1)}
    pid = await db.insert_document(parent.__tablename__, {"name": "p", "loose": shell})
    got, _ = await run_way(db, parent, pid, way)
    assert got.loose == shell


@pytest.mark.asyncio
@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
async def test_an_id_past_sqlite_integer_range_is_left_as_it_is(make_db, base, way):
    db, child, parent, ct = await _bound(make_db, base)
    pid = await db.insert_document(parent.__tablename__, {"name": "p", "deep": ref(ct, 2**63)})
    got, sel = await run_way(db, parent, pid, way)
    assert got.deep == ref(ct, 2**63)
    assert [s for s in sel if ct in s] == []


# ---- the record: what the base returned ---------------------------------------------

# (base, way) -> (fields of the parent row, SELECTs run)
EXPECTED: dict = {('lite', 'all'): ({'name': 'p',
                    'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                    'many': [('Child', 1, '-', {'name': 'c1', 'other': None}),
                             ('Child',
                              2,
                              '-',
                              {'name': 'c2',
                               'other': ('ref', 'arep_lt_child', 1)})],
                    'loose': ('ref', 'arep_lt_child', 1),
                    'deep': ('ref', 'arep_lt_child', 2),
                    'cyc': ('ref', 'arep_lt_child', 4),
                    'me': ('ref', 'arep_lt_child', 6),
                    'gone': ('ref', 'arep_lt_child', 3),
                    'nowhere': ('ref', 'arep_nobody', 1)},
                   2),
 ('lite', 'first'): ({'name': 'p',
                      'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                      'many': [('Child', 1, '-', {'name': 'c1', 'other': None}),
                               ('Child',
                                2,
                                '-',
                                {'name': 'c2',
                                 'other': ('ref', 'arep_lt_child', 1)})],
                      'loose': ('ref', 'arep_lt_child', 1),
                      'deep': ('ref', 'arep_lt_child', 2),
                      'cyc': ('ref', 'arep_lt_child', 4),
                      'me': ('ref', 'arep_lt_child', 6),
                      'gone': ('ref', 'arep_lt_child', 3),
                      'nowhere': ('ref', 'arep_nobody', 1)},
                     2),
 ('lite', 'from_id'): ({'name': 'p',
                        'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                        'many': [('Child',
                                  1,
                                  '-',
                                  {'name': 'c1', 'other': None}),
                                 ('Child',
                                  2,
                                  '-',
                                  {'name': 'c2',
                                   'other': ('ref', 'arep_lt_child', 1)})],
                        'loose': ('ref', 'arep_lt_child', 1),
                        'deep': ('ref', 'arep_lt_child', 2),
                        'cyc': ('ref', 'arep_lt_child', 4),
                        'me': ('ref', 'arep_lt_child', 6),
                        'gone': ('ref', 'arep_lt_child', 3),
                        'nowhere': ('ref', 'arep_nobody', 1)},
                       2),
 ('lite', 'from_ids'): ({'name': 'p',
                         'one': ('Child',
                                 1,
                                 '-',
                                 {'name': 'c1', 'other': None}),
                         'many': [('Child',
                                   1,
                                   '-',
                                   {'name': 'c1', 'other': None}),
                                  ('Child',
                                   2,
                                   '-',
                                   {'name': 'c2',
                                    'other': ('ref', 'arep_lt_child', 1)})],
                         'loose': ('ref', 'arep_lt_child', 1),
                         'deep': ('ref', 'arep_lt_child', 2),
                         'cyc': ('ref', 'arep_lt_child', 4),
                         'me': ('ref', 'arep_lt_child', 6),
                         'gone': ('ref', 'arep_lt_child', 3),
                         'nowhere': ('ref', 'arep_nobody', 1)},
                        2),
 ('lite', 'refresh'): ({'name': 'p',
                        'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                        'many': [('Child',
                                  1,
                                  '-',
                                  {'name': 'c1', 'other': None}),
                                 ('Child',
                                  2,
                                  '-',
                                  {'name': 'c2',
                                   'other': ('ref', 'arep_lt_child', 1)})],
                        'loose': ('ref', 'arep_lt_child', 1),
                        'deep': ('ref', 'arep_lt_child', 2),
                        'cyc': ('ref', 'arep_lt_child', 4),
                        'me': ('ref', 'arep_lt_child', 6),
                        'gone': ('ref', 'arep_lt_child', 3),
                        'nowhere': ('ref', 'arep_nobody', 1)},
                       2),
 ('lite-safe', 'all'): ({'name': 'p',
                         'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                         'many': [('Child',
                                   1,
                                   0,
                                   {'name': 'c1', 'other': None}),
                                  ('Child',
                                   2,
                                   0,
                                   {'name': 'c2',
                                    'other': ('ref', 'arep_ls_child', 1)})],
                         'loose': ('ref', 'arep_ls_child', 1),
                         'deep': ('ref', 'arep_ls_child', 2),
                         'cyc': ('ref', 'arep_ls_child', 4),
                         'me': ('ref', 'arep_ls_child', 6),
                         'gone': ('ref', 'arep_ls_child', 3),
                         'nowhere': ('ref', 'arep_nobody', 1)},
                        2),
 ('lite-safe', 'first'): ({'name': 'p',
                           'one': ('Child',
                                   1,
                                   0,
                                   {'name': 'c1', 'other': None}),
                           'many': [('Child',
                                     1,
                                     0,
                                     {'name': 'c1', 'other': None}),
                                    ('Child',
                                     2,
                                     0,
                                     {'name': 'c2',
                                      'other': ('ref', 'arep_ls_child', 1)})],
                           'loose': ('ref', 'arep_ls_child', 1),
                           'deep': ('ref', 'arep_ls_child', 2),
                           'cyc': ('ref', 'arep_ls_child', 4),
                           'me': ('ref', 'arep_ls_child', 6),
                           'gone': ('ref', 'arep_ls_child', 3),
                           'nowhere': ('ref', 'arep_nobody', 1)},
                          2),
 ('lite-safe', 'from_id'): ({'name': 'p',
                             'one': ('Child',
                                     1,
                                     0,
                                     {'name': 'c1', 'other': None}),
                             'many': [('Child',
                                       1,
                                       0,
                                       {'name': 'c1', 'other': None}),
                                      ('Child',
                                       2,
                                       0,
                                       {'name': 'c2',
                                        'other': ('ref', 'arep_ls_child', 1)})],
                             'loose': ('ref', 'arep_ls_child', 1),
                             'deep': ('ref', 'arep_ls_child', 2),
                             'cyc': ('ref', 'arep_ls_child', 4),
                             'me': ('ref', 'arep_ls_child', 6),
                             'gone': ('ref', 'arep_ls_child', 3),
                             'nowhere': ('ref', 'arep_nobody', 1)},
                            2),
 ('lite-safe', 'from_ids'): ({'name': 'p',
                              'one': ('Child',
                                      1,
                                      0,
                                      {'name': 'c1', 'other': None}),
                              'many': [('Child',
                                        1,
                                        0,
                                        {'name': 'c1', 'other': None}),
                                       ('Child',
                                        2,
                                        0,
                                        {'name': 'c2',
                                         'other': ('ref',
                                                   'arep_ls_child',
                                                   1)})],
                              'loose': ('ref', 'arep_ls_child', 1),
                              'deep': ('ref', 'arep_ls_child', 2),
                              'cyc': ('ref', 'arep_ls_child', 4),
                              'me': ('ref', 'arep_ls_child', 6),
                              'gone': ('ref', 'arep_ls_child', 3),
                              'nowhere': ('ref', 'arep_nobody', 1)},
                             2),
 ('lite-safe', 'refresh'): ({'name': 'p',
                             'one': ('Child',
                                     1,
                                     0,
                                     {'name': 'c1', 'other': None}),
                             'many': [('Child',
                                       1,
                                       0,
                                       {'name': 'c1', 'other': None}),
                                      ('Child',
                                       2,
                                       0,
                                       {'name': 'c2',
                                        'other': ('ref', 'arep_ls_child', 1)})],
                             'loose': ('ref', 'arep_ls_child', 1),
                             'deep': ('ref', 'arep_ls_child', 2),
                             'cyc': ('ref', 'arep_ls_child', 4),
                             'me': ('ref', 'arep_ls_child', 6),
                             'gone': ('ref', 'arep_ls_child', 3),
                             'nowhere': ('ref', 'arep_nobody', 1)},
                            2),
 ('pydantic', 'all'): ({'name': 'p',
                        'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                        'many': [('Child',
                                  1,
                                  '-',
                                  {'name': 'c1', 'other': None}),
                                 ('Child',
                                  2,
                                  '-',
                                  {'name': 'c2',
                                   'other': ('ref', 'arep_pd_child', 1)})],
                        'loose': ('ref', 'arep_pd_child', 1),
                        'deep': ('ref', 'arep_pd_child', 2),
                        'cyc': ('ref', 'arep_pd_child', 4),
                        'me': ('ref', 'arep_pd_child', 6),
                        'gone': ('ref', 'arep_pd_child', 3),
                        'nowhere': ('ref', 'arep_nobody', 1)},
                       2),
 ('pydantic', 'first'): ({'name': 'p',
                          'one': ('Child',
                                  1,
                                  '-',
                                  {'name': 'c1', 'other': None}),
                          'many': [('Child',
                                    1,
                                    '-',
                                    {'name': 'c1', 'other': None}),
                                   ('Child',
                                    2,
                                    '-',
                                    {'name': 'c2',
                                     'other': ('ref', 'arep_pd_child', 1)})],
                          'loose': ('ref', 'arep_pd_child', 1),
                          'deep': ('ref', 'arep_pd_child', 2),
                          'cyc': ('ref', 'arep_pd_child', 4),
                          'me': ('ref', 'arep_pd_child', 6),
                          'gone': ('ref', 'arep_pd_child', 3),
                          'nowhere': ('ref', 'arep_nobody', 1)},
                         2),
 ('pydantic', 'from_id'): ({'name': 'p',
                            'one': ('Child',
                                    1,
                                    '-',
                                    {'name': 'c1', 'other': None}),
                            'many': [('Child',
                                      1,
                                      '-',
                                      {'name': 'c1', 'other': None}),
                                     ('Child',
                                      2,
                                      '-',
                                      {'name': 'c2',
                                       'other': ('ref', 'arep_pd_child', 1)})],
                            'loose': ('ref', 'arep_pd_child', 1),
                            'deep': ('ref', 'arep_pd_child', 2),
                            'cyc': ('ref', 'arep_pd_child', 4),
                            'me': ('ref', 'arep_pd_child', 6),
                            'gone': ('ref', 'arep_pd_child', 3),
                            'nowhere': ('ref', 'arep_nobody', 1)},
                           2),
 ('pydantic', 'from_ids'): ({'name': 'p',
                             'one': ('Child',
                                     1,
                                     '-',
                                     {'name': 'c1', 'other': None}),
                             'many': [('Child',
                                       1,
                                       '-',
                                       {'name': 'c1', 'other': None}),
                                      ('Child',
                                       2,
                                       '-',
                                       {'name': 'c2',
                                        'other': ('ref', 'arep_pd_child', 1)})],
                             'loose': ('ref', 'arep_pd_child', 1),
                             'deep': ('ref', 'arep_pd_child', 2),
                             'cyc': ('ref', 'arep_pd_child', 4),
                             'me': ('ref', 'arep_pd_child', 6),
                             'gone': ('ref', 'arep_pd_child', 3),
                             'nowhere': ('ref', 'arep_nobody', 1)},
                            2),
 ('pydantic', 'refresh'): ({'name': 'p',
                            'one': ('Child',
                                    1,
                                    '-',
                                    {'name': 'c1', 'other': None}),
                            'many': [('Child',
                                      1,
                                      '-',
                                      {'name': 'c1', 'other': None}),
                                     ('Child',
                                      2,
                                      '-',
                                      {'name': 'c2',
                                       'other': ('ref', 'arep_pd_child', 1)})],
                            'loose': ('ref', 'arep_pd_child', 1),
                            'deep': ('ref', 'arep_pd_child', 2),
                            'cyc': ('ref', 'arep_pd_child', 4),
                            'me': ('ref', 'arep_pd_child', 6),
                            'gone': ('ref', 'arep_pd_child', 3),
                            'nowhere': ('ref', 'arep_nobody', 1)},
                           2),
 ('safe', 'all'): ({'name': 'p',
                    'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                    'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                             ('Child',
                              2,
                              0,
                              {'name': 'c2',
                               'other': ('ref', 'arep_sf_child', 1)})],
                    'loose': ('ref', 'arep_sf_child', 1),
                    'deep': ('ref', 'arep_sf_child', 2),
                    'cyc': ('ref', 'arep_sf_child', 4),
                    'me': ('ref', 'arep_sf_child', 6),
                    'gone': ('ref', 'arep_sf_child', 3),
                    'nowhere': ('ref', 'arep_nobody', 1)},
                   2),
 ('safe', 'first'): ({'name': 'p',
                      'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                      'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                               ('Child',
                                2,
                                0,
                                {'name': 'c2',
                                 'other': ('ref', 'arep_sf_child', 1)})],
                      'loose': ('ref', 'arep_sf_child', 1),
                      'deep': ('ref', 'arep_sf_child', 2),
                      'cyc': ('ref', 'arep_sf_child', 4),
                      'me': ('ref', 'arep_sf_child', 6),
                      'gone': ('ref', 'arep_sf_child', 3),
                      'nowhere': ('ref', 'arep_nobody', 1)},
                     2),
 ('safe', 'from_id'): ({'name': 'p',
                        'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                        'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                                 ('Child',
                                  2,
                                  0,
                                  {'name': 'c2',
                                   'other': ('ref', 'arep_sf_child', 1)})],
                        'loose': ('ref', 'arep_sf_child', 1),
                        'deep': ('ref', 'arep_sf_child', 2),
                        'cyc': ('ref', 'arep_sf_child', 4),
                        'me': ('ref', 'arep_sf_child', 6),
                        'gone': ('ref', 'arep_sf_child', 3),
                        'nowhere': ('ref', 'arep_nobody', 1)},
                       2),
 ('safe', 'from_ids'): ({'name': 'p',
                         'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                         'many': [('Child',
                                   1,
                                   0,
                                   {'name': 'c1', 'other': None}),
                                  ('Child',
                                   2,
                                   0,
                                   {'name': 'c2',
                                    'other': ('ref', 'arep_sf_child', 1)})],
                         'loose': ('ref', 'arep_sf_child', 1),
                         'deep': ('ref', 'arep_sf_child', 2),
                         'cyc': ('ref', 'arep_sf_child', 4),
                         'me': ('ref', 'arep_sf_child', 6),
                         'gone': ('ref', 'arep_sf_child', 3),
                         'nowhere': ('ref', 'arep_nobody', 1)},
                        2),
 ('safe', 'refresh'): ({'name': 'p',
                        'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                        'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                                 ('Child',
                                  2,
                                  0,
                                  {'name': 'c2',
                                   'other': ('ref', 'arep_sf_child', 1)})],
                        'loose': ('ref', 'arep_sf_child', 1),
                        'deep': ('ref', 'arep_sf_child', 2),
                        'cyc': ('ref', 'arep_sf_child', 4),
                        'me': ('ref', 'arep_sf_child', 6),
                        'gone': ('ref', 'arep_sf_child', 3),
                        'nowhere': ('ref', 'arep_nobody', 1)},
                       2)}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_ref", [False, True])
@pytest.mark.parametrize("base", ["pydantic", "lite"])
async def test_all_follows_references_level_by_level_and_never_row_by_row(make_db, base, with_ref):
    """``all()`` of three parents reads the parents and then, if one holds a reference, its
    child: one SELECT per level, never one per row."""
    child, parent = BASES[base]
    db = await make_db()
    await bind(db, child, parent)
    c1 = await db.insert_document(child.__tablename__, {"name": "c1"})
    for i in range(3):
        one = ref(child.__tablename__, c1) if (with_ref and i == 1) else None
        await db.insert_document(parent.__tablename__, {"name": f"p{i}", "one": one})
    with query_logger.capture() as sent:
        got = await parent.query().all()
    assert [g.name for g in got] == ["p0", "p1", "p2"]
    assert (got[1].one.name if with_ref else got[1].one) == ("c1" if with_ref else None)
    assert sum(q.sql.lstrip().upper().startswith("SELECT") for q in sent) == 1 + with_ref


# ---- typed references keep their row ------------------------------------------------
#
# The async twin of the section in tests/test_relation_entry_points.py. On the base the
# batch ways gave a child in a typed field ``_id`` None and the safe ``from_id`` built an
# empty one, and ``parent.save()`` then raised "Related model must be saved before
# saving parent". These tests load a parent and save it again: it must NOT raise that.


class ATyG(AsyncSQLerModel):
    __tablename__ = "arep_ty_g"
    name: str = ""


class ATyC(AsyncSQLerModel):
    __tablename__ = "arep_ty_c"
    name: str = ""
    g: Optional[ATyG] = None


class ATyP(AsyncSQLerModel):
    __tablename__ = "arep_ty_p"
    title: str = ""
    one: Optional[ATyC] = None
    many: list[ATyC] = PydField(default_factory=list)
    bag: dict[str, ATyC] = PydField(default_factory=dict)


class ATyN(AsyncSQLerModel):
    __tablename__ = "arep_ty_n"
    name: str = ""
    one: Optional["ATyN"] = None


class ATysG(AsyncSQLerSafeModel):
    __tablename__ = "arep_tys_g"
    name: str = ""


class ATysC(AsyncSQLerSafeModel):
    __tablename__ = "arep_tys_c"
    name: str = ""
    g: Optional[ATysG] = None


class ATysP(AsyncSQLerSafeModel):
    __tablename__ = "arep_tys_p"
    title: str = ""
    one: Optional[ATysC] = None
    many: list[ATysC] = PydField(default_factory=list)
    bag: dict[str, ATysC] = PydField(default_factory=dict)


class ATysN(AsyncSQLerSafeModel):
    __tablename__ = "arep_tys_n"
    name: str = ""
    one: Optional["ATysN"] = None


@dataclass
class ALtyG(AsyncSQLerLiteModel):
    __tablename__ = "arep_lty_g"
    name: str = ""


@dataclass
class ALtyC(AsyncSQLerLiteModel):
    __tablename__ = "arep_lty_c"
    name: str = ""
    g: Optional[ALtyG] = None


@dataclass
class ALtyP(AsyncSQLerLiteModel):
    __tablename__ = "arep_lty_p"
    title: str = ""
    one: Optional[ALtyC] = None
    many: list[ALtyC] = field(default_factory=list)
    bag: dict[str, ALtyC] = field(default_factory=dict)


@dataclass
class ALtyN(AsyncSQLerLiteModel):
    __tablename__ = "arep_lty_n"
    name: str = ""
    one: Optional["ALtyN"] = None


@dataclass
class ALtysG(AsyncSQLerLiteSafeModel):
    __tablename__ = "arep_ltys_g"
    name: str = ""


@dataclass
class ALtysC(AsyncSQLerLiteSafeModel):
    __tablename__ = "arep_ltys_c"
    name: str = ""
    g: Optional[ALtysG] = None


@dataclass
class ALtysP(AsyncSQLerLiteSafeModel):
    __tablename__ = "arep_ltys_p"
    title: str = ""
    one: Optional[ALtysC] = None
    many: list[ALtysC] = field(default_factory=list)
    bag: dict[str, ALtysC] = field(default_factory=dict)


@dataclass
class ALtysN(AsyncSQLerLiteSafeModel):
    __tablename__ = "arep_ltys_n"
    name: str = ""
    one: Optional["ALtysN"] = None


TYPED = {
    "pydantic": (ATyG, ATyC, ATyP, ATyN),
    "safe": (ATysG, ATysC, ATysP, ATysN),
    "lite": (ALtyG, ALtyC, ALtyP, ALtyN),
    "lite-safe": (ALtysG, ALtysC, ALtysP, ALtysN),
}
VERSIONED = ("safe", "lite-safe")
TYPED_WAYS = ["from_id", "refresh", "from_ids", "all", "first"]


def pic(x: Any) -> Any:
    """Type, ``_id``, ``_version`` and fields of a loaded value, all the way down."""
    if x is None or isinstance(x, (str, int, float, bool)):
        return x
    if isinstance(x, list):
        return [pic(v) for v in x]
    if isinstance(x, dict):
        if set(x) == {"_table", "_id"}:
            return ("ref", x["_table"], x["_id"])
        return {k: pic(v) for k, v in x.items()}
    head = (type(x).__name__, x._id, getattr(x, "_version", "-"))
    return (*head, {n: pic(getattr(x, n)) for n in type(x).model_fields})


async def load(cls: type, rid: int, way: str):
    if way == "refresh":
        inst = await cls.query().filter(F("_id") == rid).resolve(False).first()
        return await inst.refresh()
    if way == "from_id":
        return await cls.from_id(rid)
    if way == "from_ids":
        return (await cls.from_ids([rid]))[0]
    if way == "all":
        return (await cls.query().filter(F("_id") == rid).all())[0]
    return await cls.query().filter(F("_id") == rid).first()


async def rows_of(db: AsyncSQLerDB, table: str) -> list:
    cur = await db.adapter.execute(f"SELECT _id, data FROM {table} ORDER BY _id")
    try:
        rows = await cur.fetchall()
    finally:
        await cur.close()
    return [(r[0], json.loads(r[1])) for r in rows]


async def version_of(db: AsyncSQLerDB, table: str, rid: int) -> int:
    cur = await db.adapter.execute(f"SELECT _version FROM {table} WHERE _id = ?", [rid])
    try:
        return (await cur.fetchone())[0]
    finally:
        await cur.close()


def sent_to(sent: list, table: str) -> int:
    return len([q for q in sent if f"FROM {table} " in q.sql or f"FROM {table};" in q.sql])


async def _typed_world(make_db, base: str):
    """g (saved twice), c1 -> g (saved twice), c2 (once); p holds c1, [c1, c2, c1], {a: c2}."""
    g_cls, c_cls, p_cls, n_cls = TYPED[base]
    db = await make_db()
    await bind(db, g_cls, c_cls)
    await bind(db, p_cls, n_cls)
    g = await g_cls(name="g").save()
    g.name = "g2"
    await g.save()
    c1 = await c_cls(name="c1", g=g).save()
    c1.name = "c1b"
    await c1.save()
    c2 = await c_cls(name="c2").save()
    p = await p_cls(title="p", one=c1, many=[c1, c2, c1], bag={"a": c2}).save()
    return db, (g_cls, c_cls, p_cls, n_cls), p._id


def _want_typed(base: str) -> dict:
    v1, v0 = (1, 0) if base in VERSIONED else ("-", "-")
    g_cls, c_cls, _, _ = TYPED[base]
    g = (g_cls.__name__, 1, v1, {"name": "g2"})
    c1 = (c_cls.__name__, 1, v1, {"name": "c1b", "g": g})
    c2 = (c_cls.__name__, 2, v0, {"name": "c2", "g": None})
    return {"title": "p", "one": c1, "many": [c1, c2, c1], "bag": {"a": c2}}


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(TYPED))
async def test_a_typed_reference_holds_its_row_and_the_parent_saves_unchanged(make_db, base, way):
    """The async twin: every child is a model of its row and the parent saves unchanged."""
    db, (g_cls, c_cls, p_cls, _), pid = await _typed_world(make_db, base)
    pt, ct, gt = p_cls.__tablename__, c_cls.__tablename__, g_cls.__tablename__
    stored, children, grand = await rows_of(db, pt), await rows_of(db, ct), await rows_of(db, gt)
    got = await load(p_cls, pid, way)
    assert pic(got)[3] == _want_typed(base)
    await got.save()  # must NOT raise "Related ... model must be saved before saving parent"
    after = (await rows_of(db, pt))[0][1]
    assert after["one"] == {"_table": ct, "_id": 1}
    assert after["many"] == [
        {"_table": ct, "_id": 1}, {"_table": ct, "_id": 2}, {"_table": ct, "_id": 1}
    ]
    assert after["bag"] == {"a": {"_table": ct, "_id": 2}}
    assert after == ({**stored[0][1], "_version": 1} if base in VERSIONED else stored[0][1])
    assert await rows_of(db, ct) == children
    assert len(children) == 2
    assert await rows_of(db, gt) == grand
    assert len(grand) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(TYPED))
async def test_a_loaded_child_saves_its_own_row(make_db, base, way):
    db, (_, c_cls, p_cls, _), pid = await _typed_world(make_db, base)
    ct = c_cls.__tablename__
    got = await load(p_cls, pid, way)
    got.many[1].name = "c2x"
    await got.many[1].save()
    rows = await rows_of(db, ct)
    assert len(rows) == 2
    assert (rows[1][0], rows[1][1]["name"]) == (2, "c2x")
    if base in VERSIONED:
        assert await version_of(db, ct, 2) == 1
        assert got.many[1]._version == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(TYPED))
async def test_a_list_holding_one_child_twice_reads_the_child_table_once(make_db, base, way):
    db, (_, c_cls, p_cls, _), pid = await _typed_world(make_db, base)
    with query_logger.capture() as sent:
        got = await load(p_cls, pid, way)
    assert [(type(m).__name__, m._id) for m in got.many] == [
        (c_cls.__name__, 1), (c_cls.__name__, 2), (c_cls.__name__, 1)
    ]
    assert sent_to(sent, c_cls.__tablename__) == 1
    # one object per row per load, by every way (the batch ways built one each before)
    assert got.many[0] is got.many[2]


def chain(x: Any, depth: int = 4) -> list:
    """``(_id, name)`` down ``.one`` from ``x``, ``depth`` levels or until it is None; a
    reference left as it is shows as ``("ref", id)``."""
    out = []
    for _ in range(depth):
        if x is None:
            break
        if isinstance(x, dict):
            out.append(("ref", x["_id"]))
            break
        out.append((x._id, x.name))
        x = x.one
    return out


# SELECTs of the cycle's table to load n1 (n1 -> n2 -> n1) and n3 (n3 -> n3), every way in:
# one per row (refresh adds the first() that loads the row to refresh)
CYCLE_SELECTS = {"from_id": 2, "refresh": 3, "from_ids": 2, "all": 2, "first": 2}
SELF_SELECTS = {"from_id": 1, "refresh": 2, "from_ids": 1, "all": 1, "first": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(TYPED))
async def test_a_cycle_through_typed_fields_closes_and_saves(make_db, base, way):
    """Every way closes a cycle on its objects (it cut it before, leaving a blank model on
    the pydantic bases whose save() refused); every model of it saves, rows unchanged."""
    db, (_, _, _, n_cls), _ = await _typed_world(make_db, base)
    nt = n_cls.__tablename__
    n1 = await db.insert_document(nt, {"name": "n1", "one": None})
    n2 = await db.insert_document(nt, {"name": "n2", "one": {"_table": nt, "_id": n1}})
    await db.upsert_document(nt, n1, {"name": "n1", "one": {"_table": nt, "_id": n2}})
    n3 = await db.insert_document(nt, {"name": "n3", "one": None})
    await db.upsert_document(nt, n3, {"name": "n3", "one": {"_table": nt, "_id": n3}})
    before = await rows_of(db, nt)
    with query_logger.capture() as sent:
        a = await load(n_cls, n1, way)
    assert sent_to(sent, nt) == CYCLE_SELECTS[way]
    with query_logger.capture() as sent:
        b = await load(n_cls, n3, way)
    assert sent_to(sent, nt) == SELF_SELECTS[way]
    assert chain(a, 2) == [(1, "n1"), (2, "n2")]
    assert a.one.one is a
    assert (b._id, b.name, b.one is b) == (3, "n3", True)
    await a.save()
    await a.one.save()
    await b.save()
    after = await rows_of(db, nt)
    assert [r[1]["one"] for r in after] == [r[1]["one"] for r in before]
    assert [r[1]["one"] for r in after] == [
        {"_table": nt, "_id": 2}, {"_table": nt, "_id": 1}, {"_table": nt, "_id": 3}
    ]


async def _try_save(db: AsyncSQLerDB, inst: Any, table: str) -> Any:
    """What ``save()`` did: the error it raised, or the parent's stored fields after it."""
    try:
        await inst.save()
    except Exception as exc:  # the outcome is pinned, whatever it is
        return (type(exc).__name__, str(exc).splitlines()[0])
    data = (await rows_of(db, table))[-1][1]
    data.pop("_version", None)
    return data


async def _observe(db: AsyncSQLerDB, cls: type, rid: int, way: str, table: str) -> Any:
    try:
        got = await load(cls, rid, way)
    except Exception as exc:
        return ("load raised", type(exc).__name__)
    return pic(got)[3], await _try_save(db, got, table)


async def _gone(make_db, base: str, way: str) -> Any:
    db, (_, c_cls, p_cls, _), _ = await _typed_world(make_db, base)
    pt = p_cls.__tablename__
    c1, c2 = await c_cls.from_id(1), await c_cls.from_id(2)
    p2 = await p_cls(title="p2", one=c2, many=[c1, c2]).save()
    await db.delete_document(c_cls.__tablename__, 2)
    return await _observe(db, p_cls, p2._id, way, pt)


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(TYPED))
async def test_a_typed_field_whose_row_is_gone(make_db, base, way):
    """See the sync twin: one answer for every way in (``gone_expected``)."""
    assert await _gone(make_db, base, way) == gone_expected(base)


async def _unfollowed(make_db, base: str, way: str, shape: str) -> Any:
    db, (g_cls, c_cls, p_cls, _), _ = await _typed_world(make_db, base)
    pt = p_cls.__tablename__
    held = (
        {"_table": c_cls.__tablename__, "_id": "x"}
        if shape == "id-str"
        else {"_table": g_cls.__tablename__, "_id": 1}
    )
    rid = await db.insert_document(pt, {"title": "h", "one": held})
    return await _observe(db, p_cls, rid, way, pt)


@pytest.mark.asyncio
@pytest.mark.parametrize(*all_pairs(
    ("shape", ["id-str", "other-table"]),
    ("base", sorted(TYPED)),
    ("way", TYPED_WAYS),
))
async def test_a_typed_field_holding_a_reference_that_cannot_be_followed(make_db, base, way, shape):
    got = await _unfollowed(make_db, base, way, shape)
    assert got == UNFOLLOWED_EXPECTED


@pytest.mark.asyncio
@pytest.mark.parametrize("base", sorted(TYPED))
async def test_a_thousand_parents_sharing_one_child_read_it_once(make_db, base):
    db, (_, c_cls, p_cls, _), _ = await _typed_world(make_db, base)
    pt, ct = p_cls.__tablename__, c_cls.__tablename__
    await db.insert_many(pt, [{"title": f"q{i}", "one": {"_table": ct, "_id": 2}} for i in range(1000)])
    with query_logger.capture() as sent:
        got = await p_cls.query().filter(F("title") != "p").all()
    assert sent_to(sent, ct) == 1
    assert len(got) == 1000
    assert {(type(g.one).__name__, g.one._id, g.one.name) for g in got} == {
        (c_cls.__name__, 2, "c2")
    }
    assert got[0].one is got[999].one  # one object per row per load (one each before)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", sorted(TYPED))
async def test_a_load_without_resolution_is_left_as_it_was(make_db, base):
    """A reference not followed is the stored reference on the pydantic bases too (they
    built a blank model of it before, whose parent's save() refused); saving writes the
    row back unchanged."""
    db, (_, c_cls, p_cls, _), pid = await _typed_world(make_db, base)
    got = await p_cls.query().filter(F("_id") == pid).resolve(False).first()
    ct, pt = c_cls.__tablename__, p_cls.__tablename__
    before = await rows_of(db, pt)
    assert (got.one, got.many, got.bag) == (
        {"_table": ct, "_id": 1},
        [{"_table": ct, "_id": 1}, {"_table": ct, "_id": 2}, {"_table": ct, "_id": 1}],
        {"a": {"_table": ct, "_id": 2}},
    )
    await got.save()
    after = await rows_of(db, pt)
    for rows in (before, after):
        for r in rows:
            r[1].pop("_version", None)
    assert after == before


# ---- a field validator that drops, reorders or replaces the children ------------------


def _reshape(v: list, info: Any) -> list:
    """The parent's title says what the validator does to ``many``."""
    mode = info.data.get("title")
    if mode == "drop":
        return v[1:]
    if mode == "reverse":
        return list(reversed(v))
    if mode == "new":
        return [type(c)(name=c.name) for c in v]
    return v


class AVdC(AsyncSQLerModel):
    __tablename__ = "arep_vd_c"
    name: str = ""


class AVdP(AsyncSQLerModel):
    __tablename__ = "arep_vd_p"
    title: str = ""
    many: list[AVdC] = PydField(default_factory=list)
    check_many = field_validator("many")(_reshape)


class AVdsC(AsyncSQLerSafeModel):
    __tablename__ = "arep_vds_c"
    name: str = ""


class AVdsP(AsyncSQLerSafeModel):
    __tablename__ = "arep_vds_p"
    title: str = ""
    many: list[AVdsC] = PydField(default_factory=list)
    check_many = field_validator("many")(_reshape)


VALIDATED = {"pydantic": (AVdC, AVdP), "safe": (AVdsC, AVdsP)}
RESHAPED = {"drop": [2, 3], "reverse": [3, 2, 1], "new": [None, None, None]}


@pytest.mark.asyncio
@pytest.mark.parametrize(*all_pairs(
    ("base", sorted(VALIDATED)),
    ("way", TYPED_WAYS),
    ("mode", sorted(RESHAPED)),
))
async def test_a_validator_that_reshapes_the_children_keeps_each_child_its_row(
    make_db, base, way, mode
):
    c_cls, p_cls = VALIDATED[base]
    db = await make_db()
    await bind(db, c_cls, p_cls)
    ct, pt = c_cls.__tablename__, p_cls.__tablename__
    ids = [await db.insert_document(ct, {"name": f"c{i}"}) for i in (1, 2, 3)]
    pid = await db.insert_document(
        pt, {"title": mode, "many": [{"_table": ct, "_id": i} for i in ids]}
    )
    children, stored = await rows_of(db, ct), await rows_of(db, pt)
    got = await load(p_cls, pid, way)
    assert [c._id for c in got.many] == RESHAPED[mode]
    assert [c.name for c in got.many] == [
        f"c{i}" for i in (RESHAPED[mode] if mode != "new" else (1, 2, 3))
    ]
    if mode == "new":
        with pytest.raises(ValueError, match="Related .*model must be saved before saving parent"):
            await got.save()
        assert await rows_of(db, pt) == stored
    else:
        await got.save()
        assert (await rows_of(db, pt))[0][1]["many"] == [
            {"_table": ct, "_id": i} for i in RESHAPED[mode]
        ]
    assert await rows_of(db, ct) == children
    assert len(children) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", VERSIONED)
async def test_a_child_holds_the_version_its_row_holds_after_a_set_update_and_saves(make_db, base, way):
    """See the sync twin: ``update_one()`` moves the version in the column and the document."""
    db, (_, c_cls, p_cls, _), pid = await _typed_world(make_db, base)
    ct = c_cls.__tablename__
    await c_cls.query().filter(F("_id") == 2).update_one(name="u")
    await c_cls.query().filter(F("_id") == 2).update_one(name="c2")
    assert await version_of(db, ct, 2) == 2
    assert (await rows_of(db, ct))[1][1]["_version"] == 2
    got = await load(p_cls, pid, way)
    alone = await c_cls.from_id(2)
    assert (got.bag["a"]._version, alone._version) == (2, 2)
    await got.bag["a"].save()
    with pytest.raises(StaleVersionError):
        await alone.save()
    assert (await version_of(db, ct, 2), (await rows_of(db, ct))[1][1]["_version"]) == (3, 3)


# ---- a child class bound to another database, and one never bound ---------------------


class ATyoC(AsyncSQLerModel):
    __tablename__ = "arep_tyo_c"
    name: str = ""


class ATyuC(AsyncSQLerModel):
    __tablename__ = "arep_tyu_c"
    name: str = ""


class ATyoP(AsyncSQLerModel):
    __tablename__ = "arep_tyo_p"
    title: str = ""
    other: Optional[ATyoC] = None
    never: Optional[ATyuC] = None


class ATysoC(AsyncSQLerSafeModel):
    __tablename__ = "arep_tyso_c"
    name: str = ""


class ATysuC(AsyncSQLerSafeModel):
    __tablename__ = "arep_tysu_c"
    name: str = ""


class ATysoP(AsyncSQLerSafeModel):
    __tablename__ = "arep_tyso_p"
    title: str = ""
    other: Optional[ATysoC] = None
    never: Optional[ATysuC] = None


@dataclass
class ALtyoC(AsyncSQLerLiteModel):
    __tablename__ = "arep_ltyo_c"
    name: str = ""


@dataclass
class ALtyuC(AsyncSQLerLiteModel):
    __tablename__ = "arep_ltyu_c"
    name: str = ""


@dataclass
class ALtyoP(AsyncSQLerLiteModel):
    __tablename__ = "arep_ltyo_p"
    title: str = ""
    other: Optional[ALtyoC] = None
    never: Optional[ALtyuC] = None


ELSEWHERE = {
    "pydantic": (ATyoC, ATyuC, ATyoP),
    "safe": (ATysoC, ATysuC, ATysoP),
    "lite": (ALtyoC, ALtyuC, ALtyoP),
}


async def _elsewhere(make_db, base: str):
    o_cls, u_cls, p_cls = ELSEWHERE[base]
    db, other_db = await make_db("parent.db"), await make_db("child.db")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        p_cls.set_db(db)
        o_cls.set_db(other_db)
    ot, ut = o_cls.__tablename__, u_cls.__tablename__
    for d, t in ((db, p_cls.__tablename__), (db, ot), (other_db, ot), (db, ut)):
        if base == "safe":
            await d._ensure_versioned_table(t)
        else:
            await d._ensure_table(t)
    # the same id in both databases, with different names, shows which one was read
    await db.insert_document(ot, {"name": "in-parent-db"})
    await other_db.insert_document(ot, {"name": "in-child-db"})
    await db.insert_document(ut, {"name": "never-bound"})
    rid = await db.insert_document(
        p_cls.__tablename__,
        {"title": "e", "other": {"_table": ot, "_id": 1}, "never": {"_table": ut, "_id": 1}},
    )
    return db, other_db, rid


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(ELSEWHERE))
async def test_a_child_class_bound_elsewhere_or_never_bound(make_db, base, way):
    """A child whose class is bound to another database than the parent's, and one whose
    class is never bound. By every way the first is read from its class's database, where
    its ``save()`` writes it back; the second from the parent's database. Every way gives
    what ``from_id`` gives (the batch ways read the parent's database before)."""
    _, _, p_cls = ELSEWHERE[base]
    db, _, rid = await _elsewhere(make_db, base)
    got = await _observe(db, p_cls, rid, way, p_cls.__tablename__)
    assert got == ELSEWHERE_EXPECTED[(base, "from_id")]


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", ["pydantic", "safe"])
async def test_a_child_bound_elsewhere_saves_and_deletes_in_its_own_database(make_db, base, way):
    o_cls, _, p_cls = ELSEWHERE[base]
    db, other_db, rid = await _elsewhere(make_db, base)
    ot = o_cls.__tablename__
    child = (await load(p_cls, rid, way)).other
    # every way reads the child from its own class's database: its row there
    assert (child._id, child.name) == (1, "in-child-db")
    child.name = "saved"
    await child.save()
    assert [r[1]["name"] for r in await rows_of(other_db, ot)] == ["saved"]
    await child.delete()
    assert await rows_of(other_db, ot) == []
    assert [r[1]["name"] for r in await rows_of(db, ot)] == ["in-parent-db"]


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["from_ids", "all", "first"])
@pytest.mark.parametrize("base", sorted(ELSEWHERE))
async def test_a_child_of_a_class_never_bound_read_through_using_saves_its_row(
    make_db, base, way
):
    _, u_cls, p_cls = ELSEWHERE[base]
    db, _, rid = await _elsewhere(make_db, base)
    ut = u_cls.__tablename__
    if way == "from_ids":
        got = (await p_cls.from_ids([rid], db=db))[0]
    elif way == "all":
        got = (await p_cls.using(db).filter(F("_id") == rid).all())[0]
    else:
        got = await p_cls.using(db).filter(F("_id") == rid).first()
    assert (got.never._id, got.never.name) == (1, "never-bound")
    got.never.name = "renamed"
    await got.never.save(db=db)
    want = {"name": "renamed", "_version": 1} if base == "safe" else {"name": "renamed"}
    assert await rows_of(db, ut) == [(1, want)]


# ---- typed fields of a class --------------------------------------------------------


def _at(cls: type) -> Any:
    """Where ``Optional[cls]`` puts a model: the field's value."""
    return _refs.Position((cls,), None, None)


def _in_list(cls: type) -> Any:
    """Where ``list[cls]`` puts a model: each entry."""
    return _refs.Position((), _at(cls), None)


def _in_dict(cls: type) -> Any:
    """Where ``dict[str, cls]`` puts a model: each value."""
    return _refs.Position((), None, _at(cls))


@pytest.mark.asyncio
async def test_typed_fields_of_a_class_defined_before_the_class_it_refers_to(make_db):
    class AFwdP(AsyncSQLerModel):
        __tablename__ = "arep_fwd_p"
        title: str = ""
        one: Optional["AFwdC"] = None

    @dataclass
    class ALFwdP(AsyncSQLerLiteModel):
        __tablename__ = "arep_lfwd_p"
        title: str = ""
        one: Optional["ALFwdC"] = None

    assert _refs.typed_fields(AFwdP) == {}
    assert _refs.typed_fields(ALFwdP) == {}

    class AFwdC(AsyncSQLerModel):
        __tablename__ = "arep_fwd_c"
        name: str = ""

    @dataclass
    class ALFwdC(AsyncSQLerLiteModel):
        __tablename__ = "arep_lfwd_c"
        name: str = ""

    AFwdP.model_rebuild()
    assert _refs.typed_fields(AFwdP) == {"one": _at(AFwdC)}
    assert _refs.typed_fields(ALFwdP) == {"one": _at(ALFwdC)}
    db = await make_db()
    for c_cls, p_cls in ((AFwdC, AFwdP), (ALFwdC, ALFwdP)):
        ct = c_cls.__tablename__
        cid = await db.insert_document(ct, {"name": "c"})
        await db.insert_document(p_cls.__tablename__, {"title": "p", "one": {"_table": ct, "_id": cid}})
        got = await p_cls.using(db).first()
        assert (type(got.one).__name__, got.one._id, got.one.name) == (c_cls.__name__, cid, "c")


def test_typed_fields_names_every_model_class_a_field_admits():
    assert _refs.typed_fields(ATyP) == {"one": _at(ATyC), "many": _in_list(ATyC), "bag": _in_dict(ATyC)}
    assert _refs.typed_fields(ALtyP) == {"one": _at(ALtyC), "many": _in_list(ALtyC), "bag": _in_dict(ALtyC)}
    assert _refs.typed_fields(ATyG) == {}


# ---- a child document its class refuses ----------------------------------------------


class ARqC(AsyncSQLerModel):
    __tablename__ = "arep_rq_c"
    name: str


class ARqP(AsyncSQLerModel):
    __tablename__ = "arep_rq_p"
    one: Optional[ARqC] = None


@dataclass
class ALRqC(AsyncSQLerLiteModel):
    __tablename__ = "arep_lrq_c"
    name: str


@dataclass
class ALRqP(AsyncSQLerLiteModel):
    __tablename__ = "arep_lrq_p"
    one: Optional[ALRqC] = None


REFUSED = {"pydantic": (ARqC, ARqP), "lite": (ALRqC, ALRqP)}


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(REFUSED))
async def test_a_child_document_its_class_refuses_fails_the_load_as_on_the_base(make_db, base, way):
    c_cls, p_cls = REFUSED[base]
    db = await make_db()
    await bind(db, c_cls, p_cls)
    ct = c_cls.__tablename__
    cid = await db.insert_document(ct, {"other": 1})
    pid = await db.insert_document(p_cls.__tablename__, {"one": {"_table": ct, "_id": cid}})
    try:
        got = await load(p_cls, pid, way)
        outcome = ("loaded", pic(got.one))
    except Exception as exc:  # the outcome is pinned, whatever it is
        outcome = type(exc).__name__
    assert outcome == REFUSED_EXPECTED[(base, way)]


# ---- classes never bound, read through using(db) ------------------------------------


class AUbC(AsyncSQLerSafeModel):
    __tablename__ = "arep_ub_c"
    name: str = ""


class AUbP(AsyncSQLerSafeModel):
    __tablename__ = "arep_ub_p"
    name: str = ""
    one: Optional[AUbC] = None
    many: list[AUbC] = PydField(default_factory=list)


@dataclass
class ALUbC(AsyncSQLerLiteSafeModel):
    __tablename__ = "arep_lub_c"
    name: str = ""


@dataclass
class ALUbP(AsyncSQLerLiteSafeModel):
    __tablename__ = "arep_lub_p"
    name: str = ""
    one: Optional[ALUbC] = None
    many: list[ALUbC] = field(default_factory=list)


UNBOUND = {"safe": (AUbC, AUbP), "lite-safe": (ALUbC, ALUbP)}


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["from_ids", "all", "first"])
@pytest.mark.parametrize("base", sorted(UNBOUND))
async def test_a_child_of_a_class_never_bound_holds_its_column_version(make_db, base, way):
    """See the sync twin (``p6_child_version.py``)."""
    c_cls, p_cls = UNBOUND[base]
    db = await make_db()
    child = c_cls(name="c")
    for name in ("c", "c1", "c2"):
        child.name = name
        await child.save(db=db)
    await p_cls(name="p", one=child, many=[child]).save(db=db)
    ct, pt = c_cls.__tablename__, p_cls.__tablename__
    stored = await rows_of(db, pt)
    if way == "from_ids":
        got = (await p_cls.from_ids([1], db=db))[0]
    elif way == "all":
        got = (await p_cls.using(db).all())[0]
    else:
        got = await p_cls.using(db).first()
    assert (got.one._id, got.one._version, got.many[0]._id, got.many[0]._version) == (1, 2, 1, 2)
    got.one.name = "again"
    await got.one.save(db=db)  # on the base: StaleVersionError (the child held version 0)
    assert await version_of(db, ct, 1) == 3
    assert (await rows_of(db, ct))[0][1]["name"] == "again"
    await got.save(db=db)
    assert [r[1]["one"] for r in await rows_of(db, pt)] == [r[1]["one"] for r in stored]
    assert [r[1]["many"] for r in await rows_of(db, pt)] == [[{"_table": ct, "_id": 1}]]


# ---- a queryset without a database ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_queryset_built_by_hand_without_a_database_reads_a_child_from_its_class_database(make_db):
    """Without a database handed over, the child of a bound class is read from its class's
    database and knows its row (it was built without its ``_id`` before)."""
    db, (_, _, p_cls, _), pid = await _typed_world(make_db, "pydantic")
    qs = AsyncSQLerQuerySet(p_cls, AsyncSQLerQuery(table=p_cls.__tablename__, adapter=db.adapter))
    got = await qs.first()
    assert (type(got.one).__name__, got.one._id, got.one.name) == ("ATyC", 1, "c1b")


# What each way gives, pinned. Where it differs from the base: a child built by the
# batch gets its row's _id (and _version); the safe from_id / refresh resolve typed
# references, so they give what AsyncSQLerModel.from_id gives (a ValidationError
# where a typed field cannot take the row it names).
REFUSED_EXPECTED: dict = {('lite', 'all'): 'TypeError',
 ('lite', 'first'): 'TypeError',
 ('lite', 'from_id'): 'TypeError',
 ('lite', 'from_ids'): 'TypeError',
 ('lite', 'refresh'): 'TypeError',
 ('pydantic', 'all'): 'ValidationError',
 ('pydantic', 'first'): 'ValidationError',
 ('pydantic', 'from_id'): 'ValidationError',
 ('pydantic', 'from_ids'): 'ValidationError',
 ('pydantic', 'refresh'): 'ValidationError'}
def gone_expected(base: str) -> Any:
    """One answer for every way in: the reference to the row that is gone reads as ``None``
    in ``one`` and is left out of ``many``; the row that is there is a model of its row.
    Saving writes the parent without the reference to the gone row."""
    g_cls, c_cls, _, _ = TYPED[base]
    v = 1 if base in VERSIONED else "-"
    child = (c_cls.__name__, 1, v, {"g": (g_cls.__name__, 1, v, {"name": "g2"}), "name": "c1b"})
    return (
        {"bag": {}, "many": [child], "one": None, "title": "p2"},
        {"bag": {}, "many": [{"_id": 1, "_table": c_cls.__tablename__}], "one": None, "title": "p2"},
    )


# a typed reference that cannot be followed leads to no row of the field's class: ``None``,
# on every base and every way in, and saving writes ``None`` in its place
UNFOLLOWED_EXPECTED = (
    {"bag": {}, "many": [], "one": None, "title": "h"},
    {"bag": {}, "many": [], "one": None, "title": "h"},
)
ELSEWHERE_EXPECTED: dict = {('lite', 'all'): ({'never': ('ALtyuC', 1, '-', {'name': 'never-bound'}),
                    'other': ('ALtyoC', 1, '-', {'name': 'in-parent-db'}),
                    'title': 'e'},
                   {'never': {'_id': 1, '_table': 'arep_ltyu_c'},
                    'other': {'_id': 1, '_table': 'arep_ltyo_c'},
                    'title': 'e'}),
 ('lite', 'first'): ({'never': ('ALtyuC', 1, '-', {'name': 'never-bound'}),
                      'other': ('ALtyoC', 1, '-', {'name': 'in-parent-db'}),
                      'title': 'e'},
                     {'never': {'_id': 1, '_table': 'arep_ltyu_c'},
                      'other': {'_id': 1, '_table': 'arep_ltyo_c'},
                      'title': 'e'}),
 ('lite', 'from_id'): ({'never': ('ALtyuC', 1, '-', {'name': 'never-bound'}),
                        'other': ('ALtyoC', 1, '-', {'name': 'in-child-db'}),
                        'title': 'e'},
                       {'never': {'_id': 1, '_table': 'arep_ltyu_c'},
                        'other': {'_id': 1, '_table': 'arep_ltyo_c'},
                        'title': 'e'}),
 ('lite', 'from_ids'): ({'never': ('ALtyuC', 1, '-', {'name': 'never-bound'}),
                         'other': ('ALtyoC', 1, '-', {'name': 'in-parent-db'}),
                         'title': 'e'},
                        {'never': {'_id': 1, '_table': 'arep_ltyu_c'},
                         'other': {'_id': 1, '_table': 'arep_ltyo_c'},
                         'title': 'e'}),
 ('lite', 'refresh'): ({'never': ('ALtyuC', 1, '-', {'name': 'never-bound'}),
                        'other': ('ALtyoC', 1, '-', {'name': 'in-child-db'}),
                        'title': 'e'},
                       {'never': {'_id': 1, '_table': 'arep_ltyu_c'},
                        'other': {'_id': 1, '_table': 'arep_ltyo_c'},
                        'title': 'e'}),
 ('pydantic', 'all'): ({'never': ('ATyuC', 1, '-', {'name': 'never-bound'}),
                        'other': ('ATyoC', None, '-', {'name': 'in-parent-db'}),
                        'title': 'e'},
                       ('ValueError', 'Related async model must be saved before saving parent')),
 ('pydantic', 'first'): ({'never': ('ATyuC', 1, '-', {'name': 'never-bound'}),
                          'other': ('ATyoC', None, '-', {'name': 'in-parent-db'}),
                          'title': 'e'},
                         ('ValueError', 'Related async model must be saved before saving parent')),
 ('pydantic', 'from_id'): ({'never': ('ATyuC', 1, '-', {'name': 'never-bound'}),
                            'other': ('ATyoC', 1, '-', {'name': 'in-child-db'}),
                            'title': 'e'},
                           {'never': {'_id': 1, '_table': 'arep_tyu_c'},
                            'other': {'_id': 1, '_table': 'arep_tyo_c'},
                            'title': 'e'}),
 ('pydantic', 'from_ids'): ({'never': ('ATyuC', 1, '-', {'name': 'never-bound'}),
                             'other': ('ATyoC', None, '-', {'name': 'in-parent-db'}),
                             'title': 'e'},
                            ('ValueError',
                             'Related async model must be saved before saving parent')),
 ('pydantic', 'refresh'): ({'never': ('ATyuC', 1, '-', {'name': 'never-bound'}),
                            'other': ('ATyoC', 1, '-', {'name': 'in-child-db'}),
                            'title': 'e'},
                           {'never': {'_id': 1, '_table': 'arep_tyu_c'},
                            'other': {'_id': 1, '_table': 'arep_tyo_c'},
                            'title': 'e'}),
 ('safe', 'all'): ({'never': ('ATysuC', 1, 0, {'name': 'never-bound'}),
                    'other': ('ATysoC', None, 0, {'name': 'in-parent-db'}),
                    'title': 'e'},
                   ('ValueError', 'Related async model must be saved before saving parent')),
 ('safe', 'first'): ({'never': ('ATysuC', 1, 0, {'name': 'never-bound'}),
                      'other': ('ATysoC', None, 0, {'name': 'in-parent-db'}),
                      'title': 'e'},
                     ('ValueError', 'Related async model must be saved before saving parent')),
 ('safe', 'from_id'): ({'never': ('ATysuC', 1, 0, {'name': 'never-bound'}),
                        'other': ('ATysoC', 1, 0, {'name': 'in-child-db'}),
                        'title': 'e'},
                       {'never': {'_id': 1, '_table': 'arep_tysu_c'},
                        'other': {'_id': 1, '_table': 'arep_tyso_c'},
                        'title': 'e'}),
 ('safe', 'from_ids'): ({'never': ('ATysuC', 1, 0, {'name': 'never-bound'}),
                         'other': ('ATysoC', None, 0, {'name': 'in-parent-db'}),
                         'title': 'e'},
                        ('ValueError', 'Related async model must be saved before saving parent')),
 ('safe', 'refresh'): ({'never': ('ATysuC', 1, 0, {'name': 'never-bound'}),
                        'other': ('ATysoC', 1, 0, {'name': 'in-child-db'}),
                        'title': 'e'},
                       {'never': {'_id': 1, '_table': 'arep_tysu_c'},
                        'other': {'_id': 1, '_table': 'arep_tyso_c'},
                        'title': 'e'})}


# ---- a batch read changes no schema -------------------------------------------------


class ANcC(AsyncSQLerSafeModel):
    __tablename__ = "arep_nc_c"
    name: str = ""


class ANcP(AsyncSQLerModel):
    __tablename__ = "arep_nc_p"
    name: str = ""
    one: Optional[ANcC] = None
    loose: Any = None


async def _columns(db: AsyncSQLerDB, table: str) -> list:
    cur = await db.adapter.execute(f"PRAGMA table_info({table})")
    try:
        return [r[1] for r in await cur.fetchall()]
    finally:
        await cur.close()


async def _plain_child_table(db: AsyncSQLerDB) -> None:
    """A child table written as plain documents (no ``_version`` column), referenced by a
    typed and an untyped field of one parent row."""
    await db.insert_document(ANcC.__tablename__, {"name": "c"})
    ref = {"_table": ANcC.__tablename__, "_id": 1}
    await db.insert_document(ANcP.__tablename__, {"name": "p", "one": ref, "loose": ref})


async def _batch(db: AsyncSQLerDB, way: str) -> Any:
    if way == "from_ids":
        return (await ANcP.from_ids([1], db=db))[0]
    if way == "all":
        return (await ANcP.using(db).all())[0]
    return await ANcP.using(db).first()


BATCH_WAYS = ["all", "first", "from_ids"]


@pytest.mark.asyncio
@pytest.mark.parametrize("way", BATCH_WAYS)
async def test_a_batch_read_adds_no_version_column_and_the_child_saves(make_db, way):
    """``p14_read_adds_column.py``: the child's table keeps its columns; the child holds
    version 0, and its ``save()`` (a write) adds the column and stores version 1."""
    db = await make_db()
    await _plain_child_table(db)
    before = await _columns(db, ANcC.__tablename__)
    got = await _batch(db, way)
    assert await _columns(db, ANcC.__tablename__) == before == ["_id", "data"]
    assert (got.one._id, got.one._version, got.loose) == (1, 0, {"_table": ANcC.__tablename__, "_id": 1})
    got.one.name = "c2"
    await got.one.save(db=db)
    assert await version_of(db, ANcC.__tablename__, 1) == 1
    assert await rows_of(db, ANcC.__tablename__) == [(1, {"name": "c2", "_version": 1})]


@pytest.mark.asyncio
@pytest.mark.parametrize("way", BATCH_WAYS)
async def test_a_batch_read_of_a_table_without_the_version_column_returns_its_connection(
    make_db, way
):
    db = await make_db()
    await _plain_child_table(db)
    got = await _batch(db, way)
    assert got.one._id == 1
    assert db.pool_stats().in_use_connections == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("way", BATCH_WAYS)
async def test_a_batch_read_works_on_a_read_only_database(tmp_path, make_db, way):
    """Opened with SQLite's ``file:...?mode=ro`` URI: the batch reads, sends no write."""
    path = tmp_path / "ro.db"
    await _plain_child_table(await make_db("ro.db"))
    ro = AsyncSQLerDB(AsyncSQLiteAdapter(f"file:{path}?mode=ro", pool_size=1))
    await ro.connect()
    try:
        got = await _batch(ro, way)
        assert (got.one._id, got.one._version, got.one.name) == (1, 0, "c")
        assert await _columns(ro, ANcC.__tablename__) == ["_id", "data"]
    finally:
        await ro.close()


@pytest.mark.asyncio
async def test_a_table_that_gets_its_version_column_between_two_reads(make_db):
    """A safe ``save()`` between two batch reads of one database object adds the column;
    the second read holds the row's version."""
    db = await make_db()
    await _plain_child_table(db)
    first = await _batch(db, "from_ids")
    assert first.one._version == 0
    await first.one.save(db=db)
    second = await _batch(db, "from_ids")
    assert (second.one._id, second.one._version) == (1, 1)


async def _scalar(db: AsyncSQLerDB, sql: str) -> Any:
    cur = await db.adapter.execute(sql)
    try:
        return (await cur.fetchone())[0]
    finally:
        await cur.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("way", BATCH_WAYS)
async def test_a_batch_read_keeps_a_raw_statement_left_uncommitted(tmp_path, way):
    """``p16_read_keeps_uncommitted.py``: a row inserted raw outside a transaction and not
    committed is still there after the batch read of a version-less child table (one
    connection, so the raw row and the read share it)."""
    db = AsyncSQLerDB(AsyncSQLiteAdapter(str(tmp_path / "raw.db"), pool_size=1))
    await db.connect()
    try:
        await _plain_child_table(db)
        await (await db.adapter.execute("CREATE TABLE arep_nc_raw (n INTEGER)")).close()
        await db.adapter.auto_commit()
        await (await db.adapter.execute("INSERT INTO arep_nc_raw VALUES (1)")).close()
        got = await _batch(db, way)
        assert (got.one._id, got.one._version, got.one.name) == (1, 0, "c")
        assert await _scalar(db, "SELECT count(*) FROM arep_nc_raw") == 1
    finally:
        await db.close()


def _pragmas(sent: list, table: str) -> int:
    return len([q for q in sent if q.sql == f'PRAGMA table_info("{table}");'])


@pytest.mark.asyncio
async def test_a_second_read_of_a_version_less_table_asks_the_table_once(make_db):
    """The second batch read by the same object sends one ``PRAGMA`` and one ``SELECT`` on
    the child table: nothing remembered, no statement that fails."""
    db = await make_db()
    await _plain_child_table(db)
    await _batch(db, "from_ids")
    with query_logger.capture() as sent:
        got = await _batch(db, "from_ids")
    assert got.one._version == 0
    assert (_pragmas(sent, ANcC.__tablename__), sent_to(sent, ANcC.__tablename__)) == (1, 1)


@pytest.mark.asyncio
async def test_a_second_read_of_a_versioned_table_sends_no_pragma(make_db):
    db = await make_db()
    await ANcC(name="c").save(db=db)
    ref = {"_table": ANcC.__tablename__, "_id": 1}
    await db.insert_document(ANcP.__tablename__, {"name": "p", "one": ref})
    await _batch(db, "from_ids")
    with query_logger.capture() as sent:
        got = await _batch(db, "from_ids")
    assert (got.one._id, got.one._version) == (1, 0)
    assert (_pragmas(sent, ANcC.__tablename__), sent_to(sent, ANcC.__tablename__)) == (0, 1)


@pytest.mark.asyncio
async def test_a_read_in_a_rolled_back_transaction_remembers_nothing(make_db):
    """The child table gets its column inside a transaction that is rolled back; the table
    is then made again without it: the next read gives version 0."""
    db = await make_db()
    await _plain_child_table(db)
    with pytest.raises(RuntimeError, match="undo"):
        async with db.transaction():
            await (await db.adapter.execute(
                f"ALTER TABLE {ANcC.__tablename__} ADD COLUMN _version INTEGER NOT NULL DEFAULT 3"
            )).close()
            assert (await _batch(db, "from_ids")).one._version == 3
            raise RuntimeError("undo")
    await (await db.adapter.execute(f"DROP TABLE {ANcC.__tablename__}")).close()
    await db.adapter.auto_commit()
    await db.insert_document(ANcC.__tablename__, {"name": "c"})
    got = await _batch(db, "from_ids")
    assert (got.one._id, got.one._version) == (1, 0)


# ---- typed fields read annotations, not words --------------------------------------


class ALitC(AsyncSQLerModel):
    __tablename__ = "arep_lit_c"
    name: str = ""


class ALitP(AsyncSQLerModel):
    __tablename__ = "arep_lit_p"
    state: typing.Literal["open", "closed"] = "open"
    kind: typing.Literal["ALitC"] = "ALitC"
    one: Optional[ALitC] = None


@dataclass
class ALLitC(AsyncSQLerLiteModel):
    __tablename__ = "arep_llit_c"
    name: str = ""


@dataclass
class ALLitP(AsyncSQLerLiteModel):
    __tablename__ = "arep_llit_p"
    state: typing.Literal["open", "closed"] = "open"
    kind: typing.Literal["ALLitC"] = "ALLitC"
    one: Optional[ALLitC] = None


@pytest.mark.parametrize(("p_cls", "c_cls"), [(ALitP, ALitC), (ALLitP, ALLitC)])
def test_a_literal_field_is_not_typed_and_the_answer_is_kept(p_cls, c_cls):
    """The values of a ``Literal`` are not names; the answer is found once and kept."""
    _refs._TYPED.pop(p_cls, None)
    first = _refs.typed_fields(p_cls)
    assert first == {"one": _at(c_cls)}
    assert p_cls in _refs._TYPED
    assert _refs.typed_fields(p_cls) is first


# ---- what a parent's mode="before" validator receives -------------------------------

SEEN_BEFORE: list = []


class ABvC(AsyncSQLerSafeModel):
    __tablename__ = "arep_bv_c"
    name: str = ""


class ABvP(AsyncSQLerSafeModel):
    __tablename__ = "arep_bv_p"
    name: str = ""
    one: Optional[ABvC] = None

    @field_validator("one", mode="before")
    @classmethod
    def _seen(cls, v: Any) -> Any:
        SEEN_BEFORE.append(type(v).__name__)
        return v


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
async def test_a_before_validator_on_a_typed_field_receives_an_instance(make_db, way):
    """Pinned: through ``from_id`` / ``refresh`` and through the batch ways the validator is
    handed the child model (the batch ways handed it the stored dict before)."""
    db = await make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        ABvC.set_db(db)
        ABvP.set_db(db)
    await ABvP(name="p", one=await ABvC(name="c").save()).save()
    SEEN_BEFORE.clear()
    got = await load(ABvP, 1, way)
    assert SEEN_BEFORE[-1] == "ABvC"
    assert (got.one._id, got.one.name) == (1, "c")


# ---- one loader: a typed reference is the model of its row ----------------------------
#
# The async twin of the section of the same name in tests/test_relation_entry_points.py.


class AGnBad(AsyncSQLerModel):
    __tablename__ = "arep_gn_bad"
    n: int


class AGnNode(AsyncSQLerModel):
    __tablename__ = "arep_gn_node"
    name: str = ""
    next: Optional[AGnNode] = None
    kids: list[AGnNode] = PydField(default_factory=list)
    bad: Optional[AGnBad] = None
    bads: list[AGnBad] = PydField(default_factory=list)


class AGsBad(AsyncSQLerSafeModel):
    __tablename__ = "arep_gs_bad"
    n: int


class AGsNode(AsyncSQLerSafeModel):
    __tablename__ = "arep_gs_node"
    name: str = ""
    next: Optional[AGsNode] = None
    kids: list[AGsNode] = PydField(default_factory=list)
    bad: Optional[AGsBad] = None
    bads: list[AGsBad] = PydField(default_factory=list)


@dataclass
class AGlBad(AsyncSQLerLiteModel):
    __tablename__ = "arep_gl_bad"
    n: int


@dataclass
class AGlNode(AsyncSQLerLiteModel):
    __tablename__ = "arep_gl_node"
    name: str = ""
    next: Optional[AGlNode] = None
    kids: list[AGlNode] = field(default_factory=list)
    bad: Optional[AGlBad] = None
    bads: list[AGlBad] = field(default_factory=list)


@dataclass
class AGlsBad(AsyncSQLerLiteSafeModel):
    __tablename__ = "arep_gls_bad"
    n: int


@dataclass
class AGlsNode(AsyncSQLerLiteSafeModel):
    __tablename__ = "arep_gls_node"
    name: str = ""
    next: Optional[AGlsNode] = None
    kids: list[AGlsNode] = field(default_factory=list)
    bad: Optional[AGlsBad] = None
    bads: list[AGlsBad] = field(default_factory=list)


AGnNode.model_rebuild()
AGsNode.model_rebuild()

AGRAPH = {
    "pydantic": (AGnNode, AGnBad),
    "safe": (AGsNode, AGsBad),
    "lite": (AGlNode, AGlBad),
    "lite-safe": (AGlsNode, AGlsBad),
}


async def _graph(make_db, base: str):
    db = await make_db()
    node, bad = AGRAPH[base]
    await bind(db, bad, node)
    return db, node, bad


def _node(name: str, **fields: Any) -> dict:
    return {"name": name, "next": None, "kids": [], "bad": None, "bads": [], **fields}


def _selects(sent: list) -> int:
    return sum(1 for q in sent if q.sql.lstrip().upper().startswith("SELECT"))


async def _put(db: AsyncSQLerDB, node: type, doc: dict, rid: Optional[int] = None) -> int:
    if rid is None:
        return await db.insert_document(node.__tablename__, doc)
    await db.upsert_document(node.__tablename__, rid, doc)
    return rid


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(AGRAPH))
@pytest.mark.parametrize("length", [2000])  # past the recursion limit, on every base and way
async def test_a_chain_is_followed_to_its_end_by_every_way(make_db, base, way, length):
    db, node, _ = await _graph(make_db, base)
    t = node.__tablename__
    # a fresh table numbers its rows 1..length: row i references row i + 1
    ids = await db.insert_many(t, [
        _node(f"n{i}", next=ref(t, i + 1) if i < length else None) for i in range(1, length + 1)
    ])
    assert ids == list(range(1, length + 1))
    with query_logger.capture() as sent:
        got = await load(node, ids[0], way)
    names, x = [], got
    while x is not None:
        assert x._id == ids[len(names)]
        names.append(x.name)
        x = x.next
    assert names == [f"n{i}" for i in range(1, length + 1)]
    assert _selects(sent) == length + (way == "refresh")


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(AGRAPH))
async def test_a_cycle_closes_on_its_objects_and_saves_by_every_way(make_db, base, way):
    db, node, _ = await _graph(make_db, base)
    t = node.__tablename__
    s = await _put(db, node, _node("s"))
    await _put(db, node, _node("s", next=ref(t, s)), s)
    a, b = await _put(db, node, _node("a")), await _put(db, node, _node("b"))
    await _put(db, node, _node("a", next=ref(t, b)), a)
    await _put(db, node, _node("b", next=ref(t, a)), b)
    k1, k2 = await _put(db, node, _node("k1")), await _put(db, node, _node("k2"))
    await _put(db, node, _node("k1", kids=[ref(t, k2)]), k1)
    await _put(db, node, _node("k2", kids=[ref(t, k1)]), k2)
    before = await rows_of(db, t)
    me = await load(node, s, way)
    assert me.next is me
    got = await load(node, a, way)
    assert (got.name, got.next.name, got.next.next is got) == ("a", "b", True)
    k = await load(node, k1, way)
    assert ([c.name for c in k.kids], k.kids[0].kids[0] is k) == (["k2"], True)
    shown = repr(got)
    assert (shown.count("name='a'"), shown.count("name='b'"), "..." in shown) == (1, 1, True)
    for x in (me, got, got.next, k, k.kids[0]):
        await x.save()
    after = await rows_of(db, t)
    for rows in (before, after):
        for r in rows:
            r[1].pop("_version", None)
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("base", sorted(AGRAPH))
async def test_two_rows_of_one_all_that_reference_each_other_are_the_result_objects(make_db, base):
    db, node, _ = await _graph(make_db, base)
    t = node.__tablename__
    a, b = await _put(db, node, _node("a")), await _put(db, node, _node("b"))
    await _put(db, node, _node("a", next=ref(t, b)), a)
    await _put(db, node, _node("b", next=ref(t, a)), b)
    got = await node.query().order_by("name").all()
    assert [g.name for g in got] == ["a", "b"]
    assert (got[0].next is got[1], got[1].next is got[0]) == (True, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(AGRAPH))
async def test_a_diamond_and_a_repeated_list_entry_are_one_object(make_db, base, way):
    db, node, _ = await _graph(make_db, base)
    t = node.__tablename__
    d = await _put(db, node, _node("d"))
    b = await _put(db, node, _node("b", next=ref(t, d)))
    c = await _put(db, node, _node("c", next=ref(t, d)))
    a = await _put(db, node, _node("a", kids=[ref(t, b), ref(t, c), ref(t, b)]))
    with query_logger.capture() as sent:
        got = await load(node, a, way)
    assert [k.name for k in got.kids] == ["b", "c", "b"]
    assert (got.kids[0] is got.kids[2], got.kids[0].next is got.kids[1].next) == (True, True)
    assert _selects(sent) == 3 + (way == "refresh")


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", ["safe", "lite-safe"])
async def test_a_versioned_child_in_a_diamond_saves_through_both_parents(make_db, base, way):
    db, node, _ = await _graph(make_db, base)
    t = node.__tablename__
    d = await _put(db, node, _node("d"))
    b = await _put(db, node, _node("b", next=ref(t, d)))
    c = await _put(db, node, _node("c", next=ref(t, d)))
    a = await _put(db, node, _node("a", kids=[ref(t, b), ref(t, c)]))
    got = await load(node, a, way)
    got.kids[0].next.name = "d1"
    await got.kids[0].next.save()
    got.kids[1].next.name = "d2"
    await got.kids[1].next.save()  # one object: it holds the version the first save wrote
    assert await version_of(db, t, d) == 2
    assert (await rows_of(db, t))[0][1]["name"] == "d2"


@pytest.mark.asyncio
@pytest.mark.parametrize(*all_pairs(
    ("where", ["field-depth-1", "list-depth-1", "field-depth-3", "list-depth-3"]),
    ("base", sorted(AGRAPH)),
    ("way", TYPED_WAYS),
))
async def test_a_referenced_row_its_class_refuses_raises_with_a_note(make_db, base, way, where):
    db, node, bad = await _graph(make_db, base)
    t, bt = node.__tablename__, bad.__tablename__
    b = await db.insert_document(bt, {})  # no ``n``: the class refuses the row
    in_list = where.startswith("list")
    holder = _node("h3", **({"bads": [ref(bt, b)]} if in_list else {"bad": ref(bt, b)}))
    top = await _put(db, node, holder)
    if where.endswith("3"):
        mid = await _put(db, node, _node("h2", next=ref(t, top)))
        top = await _put(db, node, _node("h1", next=ref(t, mid)))
    try:
        await bad.from_id(b)
    except Exception as exc:  # the type its own from_id raises is the expectation
        want = type(exc)
    with pytest.raises(want) as caught:
        await load(node, top, way)
    assert type(caught.value) is want
    holder_id = top if where.endswith("1") else top - 2
    assert caught.value.__notes__ == [
        f"loading row {b} of {bt!r}, referenced by field {'bads' if in_list else 'bad'!r}"
        f" of row {holder_id} of {t!r}"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(AGRAPH))
async def test_a_gone_row_inside_a_cycle_and_at_depth_three(make_db, base, way):
    db, node, _ = await _graph(make_db, base)
    t = node.__tablename__
    gone = await _put(db, node, _node("gone"))
    a, b = await _put(db, node, _node("a")), await _put(db, node, _node("b"))
    await _put(db, node, _node("a", next=ref(t, b), kids=[ref(t, gone), ref(t, b)]), a)
    await _put(db, node, _node("b", next=ref(t, a), kids=[ref(t, gone)]), b)
    d3 = await _put(db, node, _node("d3", next=ref(t, gone)))
    d2 = await _put(db, node, _node("d2", next=ref(t, d3)))
    d1 = await _put(db, node, _node("d1", next=ref(t, d2)))
    await db.delete_document(t, gone)
    got = await load(node, a, way)
    assert (got.next.next is got, [k.name for k in got.kids], got.kids[0] is got.next) == (
        True, ["b"], True
    )
    assert got.next.kids == []
    deep = await load(node, d1, way)
    assert [deep.name, deep.next.name, deep.next.next.name] == ["d1", "d2", "d3"]
    assert deep.next.next.next is None


@pytest.mark.asyncio
@pytest.mark.parametrize("base", sorted(AGRAPH))
async def test_a_reference_not_followed_is_the_stored_reference_and_saves_back(make_db, base):
    db, node, _ = await _graph(make_db, base)
    t = node.__tablename__
    one, two = await _put(db, node, _node("one")), await _put(db, node, _node("two"))
    await _put(db, node, _node("two", next=ref(t, one), kids=[ref(t, one), ref(t, two)]), two)
    before = await rows_of(db, t)
    got = await node.query().filter(F("_id") == two).resolve(False).first()
    assert (got.next, got.kids) == (ref(t, one), [ref(t, one), ref(t, two)])
    await got.save()
    after = await rows_of(db, t)
    for rows in (before, after):
        for r in rows:
            r[1].pop("_version", None)
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("base", sorted(AGRAPH))
async def test_statement_budget_of_a_hundred_children(make_db, base):
    """100 parents each holding one of 100 children: 2 SELECTs by ``all()``; one parent
    holding the 100 in a typed list: 2 by ``from_id``."""
    db, node, _ = await _graph(make_db, base)
    t = node.__tablename__
    kids = await db.insert_many(t, [_node(f"k{i}") for i in range(100)])
    await db.insert_many(t, [_node(f"p{i}", next=ref(t, k)) for i, k in enumerate(kids)])
    p = await _put(db, node, _node("q", kids=[ref(t, k) for k in kids]))
    with query_logger.capture() as sent:
        got = await node.query().filter(F("name").like("p%")).all()
    assert ([g.next.name for g in got], _selects(sent)) == ([f"k{i}" for i in range(100)], 2)
    with query_logger.capture() as sent:
        one = await node.from_id(p)
    assert ([k.name for k in one.kids], _selects(sent)) == ([f"k{i}" for i in range(100)], 2)


# ---- the object a row in a cycle becomes ------------------------------------------------
#
# The async twin of the section of the same name in tests/test_relation_entry_points.py
# (there is no async msgspec model).


@dataclass(slots=True)
class ACsSlots(AsyncSQLerLiteModel):
    __tablename__ = "arep_cs_slots"
    name: str
    n: int = 0
    next: Optional[ACsSlots] = None


@dataclass(frozen=True)
class ACsLiteFrozen(AsyncSQLerLiteModel):
    __tablename__ = "arep_cs_lite_frozen"
    name: str
    n: int = 0
    next: Optional[ACsLiteFrozen] = None


@dataclass(frozen=True, slots=True)
class ACsLiteFrozenSlots(AsyncSQLerLiteModel):
    __tablename__ = "arep_cs_lite_frozen_slots"
    name: str
    n: int = 0
    next: Optional[ACsLiteFrozenSlots] = None


class ACsPydFrozen(AsyncSQLerModel):
    __tablename__ = "arep_cs_pyd_frozen"
    model_config = {"frozen": True}
    name: str
    n: int = 0
    next: Optional[ACsPydFrozen] = None


class ACsPydPrivate(AsyncSQLerModel):
    __tablename__ = "arep_cs_pyd_private"
    name: str
    n: int = 0
    next: Optional[ACsPydPrivate] = None
    _tag: str = PrivateAttr(default="unset")

    def model_post_init(self, context: Any) -> None:
        super().model_post_init(context)
        self._tag = f"tag-{self.name}"


ACsPydFrozen.model_rebuild()
ACsPydPrivate.model_rebuild()

STATE_CLASSES = {
    "lite-slots": ACsSlots,
    "lite-frozen": ACsLiteFrozen,
    "lite-frozen-slots": ACsLiteFrozenSlots,
    "pydantic-frozen": ACsPydFrozen,
    "pydantic-private": ACsPydPrivate,
}
# Left out: ``refresh()`` assigns the fields of the instance it refreshes, which a frozen
# pydantic model refuses before any reference is loaded.
STATE_LEFT_OUT = {("pydantic-frozen", "refresh")}
STATE_WAYS = [
    (kind, way)
    for kind in STATE_CLASSES
    for way in TYPED_WAYS
    if (kind, way) not in STATE_LEFT_OUT
]


async def _two_cycle(make_db, cls: type) -> tuple:
    """A database with ``cls`` bound, and its rows a (n=1) and b (n=2), each ``next`` the
    other: ``(a, b)``."""
    db = await make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(db)
    await db._ensure_table(cls.__tablename__)
    t = cls.__tablename__
    a = await _put(db, cls, {"name": "a", "n": 1})
    b = await _put(db, cls, {"name": "b", "n": 2})
    await _put(db, cls, {"name": "a", "n": 1, "next": ref(t, b)}, a)
    await _put(db, cls, {"name": "b", "n": 2, "next": ref(t, a)}, b)
    return a, b


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "way"), STATE_WAYS)
async def test_a_row_in_a_cycle_holds_every_field_whatever_way_its_class_keeps_state(
    make_db, kind, way
):
    cls = STATE_CLASSES[kind]
    a, b = await _two_cycle(make_db, cls)
    got = await load(cls, a, way)
    assert (type(got.next), got.next.next is got) == (cls, True)
    assert [(x._id, x.name, x.n) for x in (got, got.next)] == [(a, "a", 1), (b, "b", 2)]
    if kind.startswith("lite"):  # the async pydantic base has no synchronous ``is_dirty``
        assert [cls.is_dirty(x) for x in (got, got.next)] == [False, False]
    if kind == "pydantic-private":
        assert (got._tag, got.next._tag) == ("tag-a", "tag-b")


HOOKED: list = []


class AHkPydPost(AsyncSQLerModel):
    __tablename__ = "arep_hk_pyd_post"
    name: str
    next: Optional[AHkPydPost] = None

    def model_post_init(self, context: Any) -> None:
        super().model_post_init(context)
        HOOKED.append(self)


class AHkPydAfter(AsyncSQLerModel):
    __tablename__ = "arep_hk_pyd_after"
    name: str
    next: Optional[AHkPydAfter] = None

    @model_validator(mode="after")
    def _keep(self) -> AHkPydAfter:
        HOOKED.append(self)
        return self


@dataclass
class AHkLite(AsyncSQLerLiteModel):
    __tablename__ = "arep_hk_lite"
    name: str
    next: Optional[AHkLite] = None

    def __post_init__(self) -> None:
        super().__post_init__()
        HOOKED.append(self)


AHkPydPost.model_rebuild()
AHkPydAfter.model_rebuild()
HOOK_CLASSES = {"pydantic-post-init": AHkPydPost, "pydantic-after": AHkPydAfter, "lite": AHkLite}


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("kind", sorted(HOOK_CLASSES))
async def test_a_hook_that_keeps_self_keeps_the_object_the_graph_holds(make_db, kind, way):
    """Each row's hook runs on the object the loaded graph holds for the row: once per row,
    except that pydantic runs a class's after-validators again on every instance of it that
    it validates as a field value. ``refresh()`` reads its row's fields onto the instance: on
    pydantic from a model it validates for that, which the hook of the row runs on; on lite
    without building one, so no hook runs for the row."""
    cls = HOOK_CLASSES[kind]
    a, _ = await _two_cycle(make_db, cls)
    after = kind == "pydantic-after"
    if way == "refresh":
        inst = await cls.query().filter(F("_id") == a).resolve(False).first()
        HOOKED.clear()
        got = await inst.refresh()
        assert (got is inst, got.next.next is got) == (True, True)
        graph_objects = [x for x in HOOKED if x is got or x is got.next]
        assert [x is got.next for x in graph_objects] == ([False, True, True] if after else [True])
        assert len(HOOKED) == (4 if after else 1 if kind == "lite" else 2)
        return
    HOOKED.clear()
    got = await load(cls, a, way)
    assert got.next.next is got
    assert len(HOOKED) == (4 if after else 2)
    assert {id(x) for x in HOOKED} == {id(got), id(got.next)}


# ---- a class that builds its model its own way ------------------------------------------


def _named_by_title(data: dict) -> dict:
    """``data`` of the shape these classes read: a row written before ``title`` became
    ``name`` is given its ``name``."""
    return {**data, "name": data["title"]} if "title" in data else data


class AOvPyd(AsyncSQLerModel):
    __tablename__ = "arep_ov_pyd"
    name: str
    next: Optional[AOvPyd] = None

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> AOvPyd:
        return super().model_validate(_named_by_title(obj), **kwargs)


class AOvPydWrap(AsyncSQLerModel):
    __tablename__ = "arep_ov_pyd_wrap"
    name: str
    next: Optional[AOvPydWrap] = None

    @model_validator(mode="wrap")
    @classmethod
    def _another(cls, data: Any, handler: Any) -> AOvPydWrap:
        if not isinstance(data, dict):
            return handler(data)  # a model in a field of another: itself
        return handler(_named_by_title(data)).model_copy()  # a row: not the object given


@dataclass
class AOvLite(AsyncSQLerLiteModel):
    __tablename__ = "arep_ov_lite"
    name: str = ""
    next: Optional[AOvLite] = None

    @classmethod
    def model_validate(cls, data: dict) -> AOvLite:
        return AsyncSQLerLiteModel.model_validate.__func__(cls, _named_by_title(data))


@dataclass(frozen=True, slots=True)
class AOvLiteFrozenSlots(AsyncSQLerLiteModel):
    __tablename__ = "arep_ov_lite_frozen_slots"
    name: str = ""
    next: Optional[AOvLiteFrozenSlots] = None

    @classmethod
    def model_validate(cls, data: dict) -> AOvLiteFrozenSlots:
        return AsyncSQLerLiteModel.model_validate.__func__(cls, _named_by_title(data))


AOvPyd.model_rebuild()
AOvPydWrap.model_rebuild()
OWN_WAY = {
    "pydantic-model-validate": AOvPyd,
    "pydantic-wrap-validator": AOvPydWrap,
    "lite": AOvLite,
    "lite-frozen-slots": AOvLiteFrozenSlots,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("kind", sorted(OWN_WAY))
async def test_a_class_that_builds_its_model_its_own_way_builds_a_row_in_a_cycle_that_way(
    make_db, kind, way
):
    """Rows stored with ``title`` load only through the class's own ``model_validate`` (or
    its validator): outside a cycle and inside one."""
    cls = OWN_WAY[kind]
    db = await make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(db)
    t = cls.__tablename__
    await db._ensure_table(t)
    a, b = await _put(db, cls, {"title": "a"}), await _put(db, cls, {"title": "b"})
    await _put(db, cls, {"title": "a", "next": ref(t, b)}, a)
    await _put(db, cls, {"title": "b", "next": ref(t, a)}, b)
    got = await load(cls, a, way)
    assert (type(got.next), got.next.next is got) == (cls, True)
    assert [(x._id, x.name) for x in (got, got.next)] == [(a, "a"), (b, "b")]


# ---- a frozen class read by a queryset --------------------------------------------------


@dataclass(frozen=True)
class AFzLite(AsyncSQLerLiteModel):
    __tablename__ = "arep_fz_lite"
    name: str = ""


@dataclass(frozen=True, slots=True)
class AFzLiteSlots(AsyncSQLerLiteModel):
    __tablename__ = "arep_fz_lite_slots"
    name: str = ""


class AFzPyd(AsyncSQLerModel):
    __tablename__ = "arep_fz_pyd"
    model_config = {"frozen": True}
    name: str


FROZEN = {"lite": AFzLite, "lite-slots": AFzLiteSlots, "pydantic": AFzPyd}


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["all", "first", "update_one", "update_n", "other-table"])
@pytest.mark.parametrize("kind", sorted(FROZEN))
async def test_a_frozen_class_read_by_a_queryset_knows_its_row(make_db, kind, how):
    cls = FROZEN[kind]
    db = await make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cls.set_db(db)
    await db._ensure_table(cls.__tablename__)
    rid = await _put(db, cls, {"name": "n"})
    rows = cls.query().filter(F("_id") == rid)
    other = f"{cls.__tablename__}_copy"
    if how == "all":
        got = (await rows.all())[0]
    elif how == "first":
        got = await rows.first()
    elif how == "update_one":
        got = await rows.update_one(name="m")
    elif how == "update_n":
        got = (await rows.update_n(1, name="m"))[0]
    else:
        await db._ensure_table(other)
        assert await db.insert_document(other, {"name": "n"}) == rid
        got = (await cls.using(db, other).all())[0]
    assert (type(got), got._id, got.name) == (cls, rid, "m" if how.startswith("update") else "n")
    if kind.startswith("lite"):  # the async pydantic base has no synchronous ``is_dirty``
        assert cls.is_dirty(got) is False
    assert got._snapshot == {"name": got.name}
    assert _ref_table(got) == (other if how == "other-table" else cls.__tablename__)
