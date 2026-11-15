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
from typing import Any, ClassVar, Optional

import pytest
import pytest_asyncio
from pydantic import Field as PydField
from pydantic import field_validator
from sqler import AsyncSQLerDB, F
from sqler.adapter import AsyncSQLiteAdapter
from sqler.exceptions import StaleVersionError
from sqler.logging import query_logger
from sqler.models import AsyncSQLerModel, AsyncSQLerQuerySet, AsyncSQLerSafeModel, _refs
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


class ACount_Pd(AsyncSQLerModel):
    __tablename__ = "arep_count_pd"
    name: str = ""
    loose: Any = None
    per_row_calls: ClassVar[int] = 0

    @classmethod
    async def _aresolve_relations(cls, data, db=None, _seen=frozenset()):
        cls.per_row_calls += 1
        return await super()._aresolve_relations(data, db=db, _seen=_seen)


@dataclass
class ACount_Lt(AsyncSQLerLiteModel):
    __tablename__ = "arep_count_lt"
    name: str = ""
    loose: Any = None
    per_row_calls: ClassVar[int] = 0

    @classmethod
    async def _aresolve_relations(cls, data, db=None, _seen=frozenset()):
        cls.per_row_calls += 1
        return await super()._aresolve_relations(data, db=db, _seen=_seen)


COUNTING = {"pydantic": (APd_Child, ACount_Pd), "lite": (ALt_Child, ACount_Lt)}

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
@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("bad", BAD_REFS)
@pytest.mark.parametrize("base", sorted(BASES))
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
        parent.__tablename__, {"name": "p", "loose": ref(ct, c1), "deep": [ref(ct, c1)]}
    )
    got, sel = await run_way(db, parent, pid, way)
    reads = [s for s in sel if f"FROM {ct} " in s and " IN (" in s]
    expected = 0 if (base == "safe" and way in ("from_id", "refresh")) else 1
    assert len(reads) == expected
    for s in reads:
        assert _ids_in(s) == [str(c1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("base", sorted(BASES))
async def test_the_same_reference_in_two_rows_of_one_all_is_read_once(make_db, base):
    db, child, parent, ct = await _bound(make_db, base)
    c1 = await db.insert_document(ct, {"name": "c1"})
    for n in ("p1", "p2"):
        await db.insert_document(parent.__tablename__, {"name": n, "loose": ref(ct, c1)})
    got, sel = await selects_of(db, lambda: parent.query().all())
    reads = [s for s in sel if f"FROM {ct} " in s]
    assert len(reads) == 1
    assert _ids_in(reads[0]) == [str(c1)]
    assert [g.loose["name"] for g in got] == ["c1", "c1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
async def test_a_thousand_references_into_one_table_are_one_statement(make_db, base, way):
    db, child, parent, ct = await _bound(make_db, base)
    ids = await db.insert_many(ct, [{"name": f"c{i}"} for i in range(1000)])
    pid = await db.insert_document(
        parent.__tablename__, {"name": "p", "loose": [ref(ct, i) for i in ids]}
    )
    got, sel = await run_way(db, parent, pid, way)
    reads = [s for s in sel if f"FROM {ct} " in s]
    if base == "safe" and way in ("from_id", "refresh"):
        assert reads == []
        assert got.loose[999] == ref(ct, ids[999])
        return
    assert len(reads) == 1
    assert len(got.loose) == 1000
    last = got.loose[999]
    name = last["name"] if isinstance(last, dict) else last.name
    assert name == "c999"


@pytest.mark.asyncio
@pytest.mark.parametrize("base", sorted(BASES))
async def test_a_chain_six_levels_deep_through_all_is_finished_by_the_per_row_pass(
    make_db, base
):
    db, child, parent, ct = await _bound(make_db, base)
    prev = None
    ids = []
    for i in range(7):
        prev = await db.insert_document(
            ct, {"name": f"c{i}", "other": None if prev is None else ref(ct, prev)}
        )
        ids.append(prev)
    # parent -> c6 -> c5 -> c4 -> c3 -> c2 -> c1 -> c0
    await db.insert_document(parent.__tablename__, {"name": "p", "loose": ref(ct, ids[6])})
    got = (await parent.query().all())[0]
    node = got.loose
    names = []
    for _ in range(5):
        names.append(node["name"])
        node = node["other"]
    assert names == ["c6", "c5", "c4", "c3", "c2"]
    # the batch stops at five levels; the per-row pass loads the sixth as a model, and
    # through it the seventh, except on the pydantic safe base, whose loader stops there
    version = 0 if "safe" in base else "-"
    assert describe(node, depth=0) == ("Child", ids[1], version, "...")
    if base == "safe":
        assert node.other == ref(ct, ids[0])
    else:
        assert describe(node.other, depth=1) == ("Child", ids[0], version, {"name": "c0", "other": None})


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
    pid = await db.insert_document(parent.__tablename__, {"name": "p", "loose": ref(ct, c1)})
    await db.adapter.execute(f"DROP TABLE {ct};")
    await db.adapter.commit()
    got, _ = await run_way(db, parent, pid, way)
    if base == "safe" and way in ("from_id", "refresh"):
        # the pydantic safe base's from_id and refresh resolve no untyped reference
        assert got.loose == ref(ct, c1)
    else:
        assert got.loose is None
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
    pid = await db.insert_document(parent.__tablename__, {"name": "p", "loose": ref(ct, 1)})
    got, _ = await run_way(db, parent, pid, way)
    assert got.loose is None
    assert ct not in await _tables(db)


# ---- hostile inputs whose answer this stream sets ----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
async def test_a_reference_shaped_dict_that_is_not_usable_is_not_walked_into(make_db, base, way):
    """``{"_table", "_id"}`` with an id that is not one is left as it is, whole."""
    db, child, parent, ct = await _bound(make_db, base)
    c1 = await db.insert_document(ct, {"name": "c1"})
    shell = {"_table": ct, "_id": "x", "inner": ref(ct, c1)}
    pid = await db.insert_document(parent.__tablename__, {"name": "p", "loose": shell})
    got, _ = await run_way(db, parent, pid, way)
    if base.startswith("lite") and way != "refresh":
        version = 0 if base == "lite-safe" else "-"
        assert describe(got.loose) == ("Child", "x", version, {"name": "", "other": None})
    else:
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
                    'many': [('Child', 1, '-', {'name': 'c1', 'other': None}), ('Child', 2, '-', {'name': 'c2', 'other': ('dict', 1, {'name': 'c1', 'other': None})})],
                    'loose': ('dict', 1, {'name': 'c1', 'other': None}),
                    'deep': ('dict',
                             2,
                             {'name': 'c2', 'other': ('dict', 1, {'name': 'c1', 'other': None})}),
                    'cyc': ('dict',
                            4,
                            {'name': 'c4',
                             'other': ('dict',
                                       5,
                                       {'name': 'c5',
                                        'other': ('Child',
                                                  4,
                                                  '-',
                                                  {'name': 'c4',
                                                   'other': ('Child', 5, '-', '...')})})}),
                    'me': ('dict',
                           6,
                           {'name': 'c6',
                            'other': ('Child',
                                      6,
                                      '-',
                                      {'name': 'c6', 'other': ('ref', 'arep_lt_child', 6)})}),
                    'gone': None,
                    'nowhere': ('dict', 1, {'name': 'nobody'})},
                   6),
 ('lite', 'first'): ({'name': 'p',
                      'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                      'many': [('Child', 1, '-', {'name': 'c1', 'other': None}), ('Child', 2, '-', {'name': 'c2', 'other': ('dict', 1, {'name': 'c1', 'other': None})})],
                      'loose': ('dict', 1, {'name': 'c1', 'other': None}),
                      'deep': ('dict',
                               2,
                               {'name': 'c2', 'other': ('dict', 1, {'name': 'c1', 'other': None})}),
                      'cyc': ('dict',
                              4,
                              {'name': 'c4',
                               'other': ('dict',
                                         5,
                                         {'name': 'c5', 'other': ('ref', 'arep_lt_child', 4)})}),
                      'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'arep_lt_child', 6)}),
                      'gone': ('ref', 'arep_lt_child', 3),
                      'nowhere': ('dict', 1, {'name': 'nobody'})},
                     4),
 ('lite', 'from_id'): ({'name': 'p',
                        'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                        'many': [('Child', 1, '-', {'name': 'c1', 'other': None}),
                                 ('Child',
                                  2,
                                  '-',
                                  {'name': 'c2',
                                   'other': ('Child', 1, '-', {'name': 'c1', 'other': None})})],
                        'loose': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                        'deep': ('Child',
                                 2,
                                 '-',
                                 {'name': 'c2',
                                  'other': ('Child', 1, '-', {'name': 'c1', 'other': None})}),
                        'cyc': ('Child',
                                4,
                                '-',
                                {'name': 'c4',
                                 'other': ('Child',
                                           5,
                                           '-',
                                           {'name': 'c5', 'other': ('ref', 'arep_lt_child', 4)})}),
                        'me': ('Child',
                               6,
                               '-',
                               {'name': 'c6', 'other': ('ref', 'arep_lt_child', 6)}),
                        'gone': None,
                        'nowhere': ('ref', 'arep_nobody', 1)},
                       4),
 ('lite', 'from_ids'): ({'name': 'p',
                         'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                         'many': [('Child', 1, '-', {'name': 'c1', 'other': None}), ('Child', 2, '-', {'name': 'c2', 'other': ('dict', 1, {'name': 'c1', 'other': None})})],
                         'loose': ('dict', 1, {'name': 'c1', 'other': None}),
                         'deep': ('dict',
                                  2,
                                  {'name': 'c2',
                                   'other': ('dict', 1, {'name': 'c1', 'other': None})}),
                         'cyc': ('dict',
                                 4,
                                 {'name': 'c4',
                                  'other': ('dict',
                                            5,
                                            {'name': 'c5', 'other': ('ref', 'arep_lt_child', 4)})}),
                         'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'arep_lt_child', 6)}),
                         'gone': ('ref', 'arep_lt_child', 3),
                         'nowhere': ('dict', 1, {'name': 'nobody'})},
                        4),
 ('lite', 'refresh'): ({'name': 'p',
                        'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                        'many': [('Child', 1, '-', {'name': 'c1', 'other': None}),
                                 ('Child',
                                  2,
                                  '-',
                                  {'name': 'c2',
                                   'other': ('Child', 1, '-', {'name': 'c1', 'other': None})})],
                        'loose': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                        'deep': ('Child',
                                 2,
                                 '-',
                                 {'name': 'c2',
                                  'other': ('Child', 1, '-', {'name': 'c1', 'other': None})}),
                        'cyc': ('Child',
                                4,
                                '-',
                                {'name': 'c4',
                                 'other': ('Child',
                                           5,
                                           '-',
                                           {'name': 'c5', 'other': ('ref', 'arep_lt_child', 4)})}),
                        'me': ('Child',
                               6,
                               '-',
                               {'name': 'c6', 'other': ('ref', 'arep_lt_child', 6)}),
                        'gone': None,
                        'nowhere': ('ref', 'arep_nobody', 1)},
                       4),
 ('lite-safe', 'all'): ({'name': 'p',
                         'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                         'many': [('Child', 1, 0, {'name': 'c1', 'other': None}), ('Child', 2, 0, {'name': 'c2', 'other': ('dict', 1, {'name': 'c1', 'other': None})})],
                         'loose': ('dict', 1, {'name': 'c1', 'other': None}),
                         'deep': ('dict',
                                  2,
                                  {'name': 'c2',
                                   'other': ('dict', 1, {'name': 'c1', 'other': None})}),
                         'cyc': ('dict',
                                 4,
                                 {'name': 'c4',
                                  'other': ('dict',
                                            5,
                                            {'name': 'c5',
                                             'other': ('Child',
                                                       4,
                                                       0,
                                                       {'name': 'c4',
                                                        'other': ('Child', 5, 0, '...')})})}),
                         'me': ('dict',
                                6,
                                {'name': 'c6',
                                 'other': ('Child',
                                           6,
                                           0,
                                           {'name': 'c6', 'other': ('ref', 'arep_ls_child', 6)})}),
                         'gone': None,
                         'nowhere': ('dict', 1, {'name': 'nobody'})},
                        6),
 ('lite-safe', 'first'): ({'name': 'p',
                           'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                           'many': [('Child', 1, 0, {'name': 'c1', 'other': None}), ('Child', 2, 0, {'name': 'c2', 'other': ('dict', 1, {'name': 'c1', 'other': None})})],
                           'loose': ('dict', 1, {'name': 'c1', 'other': None}),
                           'deep': ('dict',
                                    2,
                                    {'name': 'c2',
                                     'other': ('dict', 1, {'name': 'c1', 'other': None})}),
                           'cyc': ('dict',
                                   4,
                                   {'name': 'c4',
                                    'other': ('dict',
                                              5,
                                              {'name': 'c5',
                                               'other': ('ref', 'arep_ls_child', 4)})}),
                           'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'arep_ls_child', 6)}),
                           'gone': ('ref', 'arep_ls_child', 3),
                           'nowhere': ('dict', 1, {'name': 'nobody'})},
                          4),
 ('lite-safe', 'from_id'): ({'name': 'p',
                             'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                             'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                                      ('Child',
                                       2,
                                       0,
                                       {'name': 'c2',
                                        'other': ('Child', 1, 0, {'name': 'c1', 'other': None})})],
                             'loose': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                             'deep': ('Child',
                                      2,
                                      0,
                                      {'name': 'c2',
                                       'other': ('Child', 1, 0, {'name': 'c1', 'other': None})}),
                             'cyc': ('Child',
                                     4,
                                     0,
                                     {'name': 'c4',
                                      'other': ('Child',
                                                5,
                                                0,
                                                {'name': 'c5',
                                                 'other': ('ref', 'arep_ls_child', 4)})}),
                             'me': ('Child',
                                    6,
                                    0,
                                    {'name': 'c6', 'other': ('ref', 'arep_ls_child', 6)}),
                             'gone': None,
                             'nowhere': ('ref', 'arep_nobody', 1)},
                            4),
 ('lite-safe', 'from_ids'): ({'name': 'p',
                              'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                              'many': [('Child', 1, 0, {'name': 'c1', 'other': None}), ('Child', 2, 0, {'name': 'c2', 'other': ('dict', 1, {'name': 'c1', 'other': None})})],
                              'loose': ('dict', 1, {'name': 'c1', 'other': None}),
                              'deep': ('dict',
                                       2,
                                       {'name': 'c2',
                                        'other': ('dict', 1, {'name': 'c1', 'other': None})}),
                              'cyc': ('dict',
                                      4,
                                      {'name': 'c4',
                                       'other': ('dict',
                                                 5,
                                                 {'name': 'c5',
                                                  'other': ('ref', 'arep_ls_child', 4)})}),
                              'me': ('dict',
                                     6,
                                     {'name': 'c6', 'other': ('ref', 'arep_ls_child', 6)}),
                              'gone': ('ref', 'arep_ls_child', 3),
                              'nowhere': ('dict', 1, {'name': 'nobody'})},
                             4),
 ('lite-safe', 'refresh'): ({'name': 'p',
                             'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                             'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                                      ('Child',
                                       2,
                                       0,
                                       {'name': 'c2',
                                        'other': ('Child', 1, 0, {'name': 'c1', 'other': None})})],
                             'loose': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                             'deep': ('Child',
                                      2,
                                      0,
                                      {'name': 'c2',
                                       'other': ('Child', 1, 0, {'name': 'c1', 'other': None})}),
                             'cyc': ('Child',
                                     4,
                                     0,
                                     {'name': 'c4',
                                      'other': ('Child',
                                                5,
                                                0,
                                                {'name': 'c5',
                                                 'other': ('ref', 'arep_ls_child', 4)})}),
                             'me': ('Child',
                                    6,
                                    0,
                                    {'name': 'c6', 'other': ('ref', 'arep_ls_child', 6)}),
                             'gone': None,
                             'nowhere': ('ref', 'arep_nobody', 1)},
                            4),
 ('pydantic', 'all'): ({'name': 'p',
                        'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                        'many': [('Child', 1, '-', {'name': 'c1', 'other': None}),
                                 ('Child',
                                  2,
                                  '-',
                                  {'name': 'c2',
                                   'other': ('dict', 1, {'name': 'c1', 'other': None})})],
                        'loose': ('dict', 1, {'name': 'c1', 'other': None}),
                        'deep': ('dict',
                                 2,
                                 {'name': 'c2',
                                  'other': ('dict', 1, {'name': 'c1', 'other': None})}),
                        'cyc': ('dict',
                                4,
                                {'name': 'c4',
                                 'other': ('dict',
                                           5,
                                           {'name': 'c5',
                                            'other': ('Child',
                                                      4,
                                                      '-',
                                                      {'name': 'c4',
                                                       'other': ('Child', 5, '-', '...')})})}),
                        'me': ('dict',
                               6,
                               {'name': 'c6',
                                'other': ('Child',
                                          6,
                                          '-',
                                          {'name': 'c6', 'other': ('ref', 'arep_pd_child', 6)})}),
                        'gone': None,
                        'nowhere': ('dict', 1, {'name': 'nobody'})},
                       6),
 ('pydantic', 'first'): ({'name': 'p',
                          'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                          'many': [('Child', 1, '-', {'name': 'c1', 'other': None}),
                                   ('Child',
                                    2,
                                    '-',
                                    {'name': 'c2',
                                     'other': ('dict', 1, {'name': 'c1', 'other': None})})],
                          'loose': ('dict', 1, {'name': 'c1', 'other': None}),
                          'deep': ('dict',
                                   2,
                                   {'name': 'c2',
                                    'other': ('dict', 1, {'name': 'c1', 'other': None})}),
                          'cyc': ('dict',
                                  4,
                                  {'name': 'c4',
                                   'other': ('dict',
                                             5,
                                             {'name': 'c5',
                                              'other': ('ref', 'arep_pd_child', 4)})}),
                          'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'arep_pd_child', 6)}),
                          'gone': ('ref', 'arep_pd_child', 3),
                          'nowhere': ('dict', 1, {'name': 'nobody'})},
                         4),
 ('pydantic', 'from_id'): ({'name': 'p',
                            'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                            'many': [('Child', 1, '-', {'name': 'c1', 'other': None}),
                                     ('Child',
                                      2,
                                      '-',
                                      {'name': 'c2',
                                       'other': ('Child', 1, '-', {'name': 'c1', 'other': None})})],
                            'loose': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                            'deep': ('Child',
                                     2,
                                     '-',
                                     {'name': 'c2',
                                      'other': ('Child', 1, '-', {'name': 'c1', 'other': None})}),
                            'cyc': ('Child',
                                    4,
                                    '-',
                                    {'name': 'c4',
                                     'other': ('Child',
                                               5,
                                               '-',
                                               {'name': 'c5',
                                                'other': ('ref', 'arep_pd_child', 4)})}),
                            'me': ('Child',
                                   6,
                                   '-',
                                   {'name': 'c6', 'other': ('ref', 'arep_pd_child', 6)}),
                            'gone': None,
                            'nowhere': ('ref', 'arep_nobody', 1)},
                           4),
 ('pydantic', 'from_ids'): ({'name': 'p',
                             'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                             'many': [('Child', 1, '-', {'name': 'c1', 'other': None}),
                                      ('Child',
                                       2,
                                       '-',
                                       {'name': 'c2',
                                        'other': ('dict', 1, {'name': 'c1', 'other': None})})],
                             'loose': ('dict', 1, {'name': 'c1', 'other': None}),
                             'deep': ('dict',
                                      2,
                                      {'name': 'c2',
                                       'other': ('dict', 1, {'name': 'c1', 'other': None})}),
                             'cyc': ('dict',
                                     4,
                                     {'name': 'c4',
                                      'other': ('dict',
                                                5,
                                                {'name': 'c5',
                                                 'other': ('ref', 'arep_pd_child', 4)})}),
                             'me': ('dict',
                                    6,
                                    {'name': 'c6', 'other': ('ref', 'arep_pd_child', 6)}),
                             'gone': ('ref', 'arep_pd_child', 3),
                             'nowhere': ('dict', 1, {'name': 'nobody'})},
                            4),
 ('pydantic', 'refresh'): ({'name': 'p',
                            'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                            'many': [('Child', 1, '-', {'name': 'c1', 'other': None}),
                                     ('Child',
                                      2,
                                      '-',
                                      {'name': 'c2',
                                       'other': ('Child', 1, '-', {'name': 'c1', 'other': None})})],
                            'loose': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                            'deep': ('Child',
                                     2,
                                     '-',
                                     {'name': 'c2',
                                      'other': ('Child', 1, '-', {'name': 'c1', 'other': None})}),
                            'cyc': ('Child',
                                    4,
                                    '-',
                                    {'name': 'c4',
                                     'other': ('Child',
                                               5,
                                               '-',
                                               {'name': 'c5',
                                                'other': ('ref', 'arep_pd_child', 4)})}),
                            'me': ('Child',
                                   6,
                                   '-',
                                   {'name': 'c6', 'other': ('ref', 'arep_pd_child', 6)}),
                            'gone': None,
                            'nowhere': ('ref', 'arep_nobody', 1)},
                           4),
 ('safe', 'all'): ({'name': 'p',
                    'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                    'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                             ('Child',
                              2,
                              0,
                              {'name': 'c2', 'other': ('dict', 1, {'name': 'c1', 'other': None})})],
                    'loose': ('dict', 1, {'name': 'c1', 'other': None}),
                    'deep': ('dict',
                             2,
                             {'name': 'c2', 'other': ('dict', 1, {'name': 'c1', 'other': None})}),
                    'cyc': ('dict',
                            4,
                            {'name': 'c4',
                             'other': ('dict',
                                       5,
                                       {'name': 'c5',
                                        'other': ('Child',
                                                  4,
                                                  0,
                                                  {'name': 'c4',
                                                   'other': ('ref', 'arep_sf_child', 5)})})}),
                    'me': ('dict',
                           6,
                           {'name': 'c6',
                            'other': ('Child',
                                      6,
                                      0,
                                      {'name': 'c6', 'other': ('ref', 'arep_sf_child', 6)})}),
                    'gone': None,
                    'nowhere': ('dict', 1, {'name': 'nobody'})},
                   5),
 ('safe', 'first'): ({'name': 'p',
                      'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                      'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                               ('Child',
                                2,
                                0,
                                {'name': 'c2',
                                 'other': ('dict', 1, {'name': 'c1', 'other': None})})],
                      'loose': ('dict', 1, {'name': 'c1', 'other': None}),
                      'deep': ('dict',
                               2,
                               {'name': 'c2', 'other': ('dict', 1, {'name': 'c1', 'other': None})}),
                      'cyc': ('dict',
                              4,
                              {'name': 'c4',
                               'other': ('dict',
                                         5,
                                         {'name': 'c5', 'other': ('ref', 'arep_sf_child', 4)})}),
                      'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'arep_sf_child', 6)}),
                      'gone': ('ref', 'arep_sf_child', 3),
                      'nowhere': ('dict', 1, {'name': 'nobody'})},
                     4),
 ('safe', 'from_id'): ({'name': 'p',
                        'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                        'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                                 ('Child',
                                  2,
                                  0,
                                  {'name': 'c2', 'other': ('ref', 'arep_sf_child', 1)})],
                        'loose': ('ref', 'arep_sf_child', 1),
                        'deep': ('ref', 'arep_sf_child', 2),
                        'cyc': ('ref', 'arep_sf_child', 4),
                        'me': ('ref', 'arep_sf_child', 6),
                        'gone': ('ref', 'arep_sf_child', 3),
                        'nowhere': ('ref', 'arep_nobody', 1)},
                       2),
 ('safe', 'from_ids'): ({'name': 'p',
                         'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                         'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                                  ('Child',
                                   2,
                                   0,
                                   {'name': 'c2',
                                    'other': ('dict', 1, {'name': 'c1', 'other': None})})],
                         'loose': ('dict', 1, {'name': 'c1', 'other': None}),
                         'deep': ('dict',
                                  2,
                                  {'name': 'c2',
                                   'other': ('dict', 1, {'name': 'c1', 'other': None})}),
                         'cyc': ('dict',
                                 4,
                                 {'name': 'c4',
                                  'other': ('dict',
                                            5,
                                            {'name': 'c5', 'other': ('ref', 'arep_sf_child', 4)})}),
                         'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'arep_sf_child', 6)}),
                         'gone': ('ref', 'arep_sf_child', 3),
                         'nowhere': ('dict', 1, {'name': 'nobody'})},
                        4),
 ('safe', 'refresh'): ({'name': 'p',
                        'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                        'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                                 ('Child',
                                  2,
                                  0,
                                  {'name': 'c2', 'other': ('ref', 'arep_sf_child', 1)})],
                        'loose': ('ref', 'arep_sf_child', 1),
                        'deep': ('ref', 'arep_sf_child', 2),
                        'cyc': ('ref', 'arep_sf_child', 4),
                        'me': ('ref', 'arep_sf_child', 6),
                        'gone': ('ref', 'arep_sf_child', 3),
                        'nowhere': ('ref', 'arep_nobody', 1)},
                       2)}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_ref,calls", [(False, 0), (True, 3)])
@pytest.mark.parametrize("base", sorted(COUNTING))
async def test_all_runs_the_per_row_pass_only_when_a_row_holds_a_reference(
    make_db, base, with_ref, calls
):
    child, parent = COUNTING[base]
    db = await make_db()
    await bind(db, child, parent)
    c1 = await db.insert_document(child.__tablename__, {"name": "c1"})
    for i in range(3):
        loose = ref(child.__tablename__, c1) if (with_ref and i == 1) else i
        await db.insert_document(parent.__tablename__, {"name": f"p{i}", "loose": loose})
    parent.per_row_calls = 0
    got = await parent.query().all()
    assert [g.name for g in got] == ["p0", "p1", "p2"]
    assert parent.per_row_calls == calls


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
    # as on the base: the per-row ways put one model at both places, the batch one each
    assert (got.many[0] is got.many[2]) is (way in ("from_id", "refresh"))


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


# SELECTs of the cycle's table to load n1 (n1 -> n2 -> n1) and n3 (n3 -> n3), every way
# in (refresh includes the first() that loads the row to refresh). On the base all() sent
# 5 and 3: its per-row pass followed the reference the batch left at the cycle one level
# further. The batch now hands that pass built children, so all() cuts where the others do.
CYCLE_SELECTS = {"from_id": 3, "refresh": 4, "from_ids": 3, "all": 3, "first": 3}
SELF_SELECTS = {"from_id": 2, "refresh": 3, "from_ids": 2, "all": 2, "first": 2}
# what saving the level below each cut does, per base: on the pydantic bases it holds an
# empty model and refuses, as AsyncSQLerModel.from_id's answer always did; on lite-safe it
# is row n1 again, read at a version a.save() moved past; on lite it saves
_REFUSE = (ValueError, "Related .*model must be saved before saving parent")
BELOW_CUT = {"pydantic": _REFUSE, "safe": _REFUSE, "lite": None, "lite-safe": (StaleVersionError, None)}


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(TYPED))
async def test_a_cycle_through_typed_fields_ends_and_saves(make_db, base, way):
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
    assert (chain(a, 5), chain(b, 5)) == CYCLE_CHAINS[(base, way)]
    await a.save()
    await a.one.save()
    await b.save()
    refused = BELOW_CUT[base]
    for level in (a.one.one, b.one):
        if refused is None:
            await level.save()
        else:
            with pytest.raises(refused[0], match=refused[1]):
                await level.save()
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
    """Pinned (see the sync twin): a reference to a row that is gone stays what each way
    gave on the base; the rows that are there are models of their rows."""
    assert await _gone(make_db, base, way) == GONE_EXPECTED[(base, way)]


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
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(TYPED))
@pytest.mark.parametrize("shape", ["id-str", "other-table"])
async def test_a_typed_field_holding_a_reference_that_cannot_be_followed(make_db, base, way, shape):
    got = await _unfollowed(make_db, base, way, shape)
    assert got == UNFOLLOWED_EXPECTED[(shape, base, way)]


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
    assert got[0].one is not got[1].one  # each parent its own model, as on the base


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["pydantic", "safe"])
async def test_a_load_without_resolution_is_left_as_it_was(make_db, base):
    db, (_, _, p_cls, _), pid = await _typed_world(make_db, base)
    got = await p_cls.query().filter(F("_id") == pid).resolve(False).first()
    kind = "ATysC" if base == "safe" else "ATyC"
    v = 0 if base == "safe" else "-"
    empty = (kind, None, v, {"name": "", "g": None})
    assert pic(got)[3] == {"title": "p", "one": empty, "many": [empty] * 3, "bag": {"a": empty}}
    with pytest.raises(ValueError, match="Related .*model must be saved before saving parent"):
        await got.save()


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
@pytest.mark.parametrize("mode", sorted(RESHAPED))
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(VALIDATED))
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
async def test_a_child_holds_the_version_of_its_column_not_of_its_json(make_db, base, way):
    """See the sync twin: ``update_one()`` bumps the column only."""
    db, (_, c_cls, p_cls, _), pid = await _typed_world(make_db, base)
    ct = c_cls.__tablename__
    await c_cls.query().filter(F("_id") == 2).update_one(name="u")
    await c_cls.query().filter(F("_id") == 2).update_one(name="c2")
    assert await version_of(db, ct, 2) == 2
    assert (await rows_of(db, ct))[1][1]["_version"] == 0
    got = await load(p_cls, pid, way)
    alone = await c_cls.from_id(2)
    assert (got.bag["a"]._version, alone._version) == (2, 2)
    for inst in (got.bag["a"], alone):
        with pytest.raises(StaleVersionError):
            await inst.save()
    assert await version_of(db, ct, 2) == 2


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
    class is never bound. On the pydantic bases the batch builds no child of a class bound
    elsewhere: it stays what the base gave (``_id`` None). On the lite bases the batch
    gives that child the ``_id`` of the row in the parent's database, and its ``save()``
    writes the row of that number in its own class's database, as the released version
    does. A class never bound reads the parent's database, so its child is built with its
    row."""
    _, _, p_cls = ELSEWHERE[base]
    db, _, rid = await _elsewhere(make_db, base)
    got = await _observe(db, p_cls, rid, way, p_cls.__tablename__)
    assert got == ELSEWHERE_EXPECTED[(base, way)]


@pytest.mark.asyncio
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", ["pydantic", "safe"])
async def test_a_child_bound_elsewhere_saves_and_deletes_in_its_own_database(make_db, base, way):
    o_cls, _, p_cls = ELSEWHERE[base]
    db, other_db, rid = await _elsewhere(make_db, base)
    ot = o_cls.__tablename__
    child = (await load(p_cls, rid, way)).other
    if way in ("from_id", "refresh"):
        # the per-row ways read the child from its own class's database: its row there
        assert (child._id, child.name) == (1, "in-child-db")
        child.name = "saved"
        await child.save()
        assert [r[1]["name"] for r in await rows_of(other_db, ot)] == ["saved"]
        await child.delete()
        assert await rows_of(other_db, ot) == []
    else:
        # the batch read the parent's database: the child is not given that row's id
        assert (child._id, child.name) == (None, "in-parent-db")
        with pytest.raises(ValueError, match="Cannot delete unsaved model"):
            await child.delete()
        await child.save()  # a new row in its own database
        names = [r[1]["name"] for r in await rows_of(other_db, ot)]
        assert names == ["in-child-db", "in-parent-db"]
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
    assert _refs.typed_fields(AFwdP) == {"one": (AFwdC,)}
    assert _refs.typed_fields(ALFwdP) == {"one": (ALFwdC,)}
    db = await make_db()
    for c_cls, p_cls in ((AFwdC, AFwdP), (ALFwdC, ALFwdP)):
        ct = c_cls.__tablename__
        cid = await db.insert_document(ct, {"name": "c"})
        await db.insert_document(p_cls.__tablename__, {"title": "p", "one": {"_table": ct, "_id": cid}})
        got = await p_cls.using(db).first()
        assert (type(got.one).__name__, got.one._id, got.one.name) == (c_cls.__name__, cid, "c")


def test_typed_fields_names_every_model_class_a_field_admits():
    assert _refs.typed_fields(ATyP) == {"one": (ATyC,), "many": (ATyC,), "bag": (ATyC,)}
    assert _refs.typed_fields(ALtyP) == {"one": (ALtyC,), "many": (ALtyC,), "bag": (ALtyC,)}
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


# ---- p6: classes never bound, read through using(db) --------------------------------


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
async def test_a_queryset_built_by_hand_without_a_database_builds_no_child(make_db):
    db, (_, _, p_cls, _), pid = await _typed_world(make_db, "pydantic")
    qs = AsyncSQLerQuerySet(p_cls, AsyncSQLerQuery(table=p_cls.__tablename__, adapter=db.adapter))
    got = await qs.first()
    assert (type(got.one).__name__, got.one._id, got.one.name) == ("ATyC", None, "c1b")


# What each way gives, pinned. Where it differs from the base: a child built by the
# batch gets its row's _id (and _version); the safe from_id / refresh resolve typed
# references, so they give what AsyncSQLerModel.from_id gives (a ValidationError
# where a typed field cannot take the row it names).
# where each way cuts the cycle: at the second time a row is reached below the row
# loaded; the cut reference is left to the class (an empty model on the pydantic
# bases, the reference itself on the lite ones)
CYCLE_CHAINS: dict = {('lite', 'all'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), ('ref', 2)],
                   [(3, 'n3'), (3, 'n3'), ('ref', 3)]),
 ('lite', 'first'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), ('ref', 2)],
                     [(3, 'n3'), (3, 'n3'), ('ref', 3)]),
 ('lite', 'from_id'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), ('ref', 2)],
                       [(3, 'n3'), (3, 'n3'), ('ref', 3)]),
 ('lite', 'from_ids'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), ('ref', 2)],
                        [(3, 'n3'), (3, 'n3'), ('ref', 3)]),
 ('lite', 'refresh'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), ('ref', 2)],
                       [(3, 'n3'), (3, 'n3'), ('ref', 3)]),
 ('lite-safe', 'all'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), ('ref', 2)],
                        [(3, 'n3'), (3, 'n3'), ('ref', 3)]),
 ('lite-safe', 'first'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), ('ref', 2)],
                          [(3, 'n3'), (3, 'n3'), ('ref', 3)]),
 ('lite-safe', 'from_id'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), ('ref', 2)],
                            [(3, 'n3'), (3, 'n3'), ('ref', 3)]),
 ('lite-safe', 'from_ids'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), ('ref', 2)],
                             [(3, 'n3'), (3, 'n3'), ('ref', 3)]),
 ('lite-safe', 'refresh'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), ('ref', 2)],
                            [(3, 'n3'), (3, 'n3'), ('ref', 3)]),
 ('pydantic', 'all'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), (None, '')],
                       [(3, 'n3'), (3, 'n3'), (None, '')]),
 ('pydantic', 'first'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), (None, '')],
                         [(3, 'n3'), (3, 'n3'), (None, '')]),
 ('pydantic', 'from_id'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), (None, '')],
                           [(3, 'n3'), (3, 'n3'), (None, '')]),
 ('pydantic', 'from_ids'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), (None, '')],
                            [(3, 'n3'), (3, 'n3'), (None, '')]),
 ('pydantic', 'refresh'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), (None, '')],
                           [(3, 'n3'), (3, 'n3'), (None, '')]),
 ('safe', 'all'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), (None, '')],
                   [(3, 'n3'), (3, 'n3'), (None, '')]),
 ('safe', 'first'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), (None, '')],
                     [(3, 'n3'), (3, 'n3'), (None, '')]),
 ('safe', 'from_id'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), (None, '')],
                       [(3, 'n3'), (3, 'n3'), (None, '')]),
 ('safe', 'from_ids'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), (None, '')],
                        [(3, 'n3'), (3, 'n3'), (None, '')]),
 ('safe', 'refresh'): ([(1, 'n1'), (2, 'n2'), (1, 'n1'), (None, '')],
                       [(3, 'n3'), (3, 'n3'), (None, '')])}
REFUSED_EXPECTED: dict = {('lite', 'all'): 'TypeError',
 ('lite', 'first'): 'TypeError',
 ('lite', 'from_id'): ('loaded', ('ref', 'arep_lrq_c', 1)),
 ('lite', 'from_ids'): 'TypeError',
 ('lite', 'refresh'): ('loaded', ('ref', 'arep_lrq_c', 1)),
 ('pydantic', 'all'): 'ValidationError',
 ('pydantic', 'first'): 'ValidationError',
 ('pydantic', 'from_id'): 'ValidationError',
 ('pydantic', 'from_ids'): 'ValidationError',
 ('pydantic', 'refresh'): 'ValidationError'}
GONE_EXPECTED: dict = {('lite', 'all'): ({'bag': {},
                    'many': [('ALtyC',
                              1,
                              '-',
                              {'g': ('ALtyG', 1, '-', {'name': 'g2'}), 'name': 'c1b'}),
                             None],
                    'one': None,
                    'title': 'p2'},
                   {'bag': {},
                    'many': [{'_id': 1, '_table': 'arep_lty_c'}, None],
                    'one': None,
                    'title': 'p2'}),
 ('lite', 'first'): ({'bag': {},
                      'many': [('ALtyC',
                                1,
                                '-',
                                {'g': ('ALtyG', 1, '-', {'name': 'g2'}), 'name': 'c1b'}),
                               ('ref', 'arep_lty_c', 2)],
                      'one': ('ref', 'arep_lty_c', 2),
                      'title': 'p2'},
                     {'bag': {},
                      'many': [{'_id': 1, '_table': 'arep_lty_c'},
                               {'_id': 2, '_table': 'arep_lty_c'}],
                      'one': {'_id': 2, '_table': 'arep_lty_c'},
                      'title': 'p2'}),
 ('lite', 'from_id'): ({'bag': {},
                        'many': [('ALtyC',
                                  1,
                                  '-',
                                  {'g': ('ALtyG', 1, '-', {'name': 'g2'}), 'name': 'c1b'}),
                                 None],
                        'one': None,
                        'title': 'p2'},
                       {'bag': {},
                        'many': [{'_id': 1, '_table': 'arep_lty_c'}, None],
                        'one': None,
                        'title': 'p2'}),
 ('lite', 'from_ids'): ({'bag': {},
                         'many': [('ALtyC',
                                   1,
                                   '-',
                                   {'g': ('ALtyG', 1, '-', {'name': 'g2'}), 'name': 'c1b'}),
                                  ('ref', 'arep_lty_c', 2)],
                         'one': ('ref', 'arep_lty_c', 2),
                         'title': 'p2'},
                        {'bag': {},
                         'many': [{'_id': 1, '_table': 'arep_lty_c'},
                                  {'_id': 2, '_table': 'arep_lty_c'}],
                         'one': {'_id': 2, '_table': 'arep_lty_c'},
                         'title': 'p2'}),
 ('lite', 'refresh'): ({'bag': {},
                        'many': [('ALtyC',
                                  1,
                                  '-',
                                  {'g': ('ALtyG', 1, '-', {'name': 'g2'}), 'name': 'c1b'}),
                                 None],
                        'one': None,
                        'title': 'p2'},
                       {'bag': {},
                        'many': [{'_id': 1, '_table': 'arep_lty_c'}, None],
                        'one': None,
                        'title': 'p2'}),
 ('lite-safe', 'all'): ({'bag': {},
                         'many': [('ALtysC',
                                   1,
                                   1,
                                   {'g': ('ALtysG', 1, 1, {'name': 'g2'}), 'name': 'c1b'}),
                                  None],
                         'one': None,
                         'title': 'p2'},
                        {'bag': {},
                         'many': [{'_id': 1, '_table': 'arep_ltys_c'}, None],
                         'one': None,
                         'title': 'p2'}),
 ('lite-safe', 'first'): ({'bag': {},
                           'many': [('ALtysC',
                                     1,
                                     1,
                                     {'g': ('ALtysG', 1, 1, {'name': 'g2'}), 'name': 'c1b'}),
                                    ('ref', 'arep_ltys_c', 2)],
                           'one': ('ref', 'arep_ltys_c', 2),
                           'title': 'p2'},
                          {'bag': {},
                           'many': [{'_id': 1, '_table': 'arep_ltys_c'},
                                    {'_id': 2, '_table': 'arep_ltys_c'}],
                           'one': {'_id': 2, '_table': 'arep_ltys_c'},
                           'title': 'p2'}),
 ('lite-safe', 'from_id'): ({'bag': {},
                             'many': [('ALtysC',
                                       1,
                                       1,
                                       {'g': ('ALtysG', 1, 1, {'name': 'g2'}), 'name': 'c1b'}),
                                      None],
                             'one': None,
                             'title': 'p2'},
                            {'bag': {},
                             'many': [{'_id': 1, '_table': 'arep_ltys_c'}, None],
                             'one': None,
                             'title': 'p2'}),
 ('lite-safe', 'from_ids'): ({'bag': {},
                              'many': [('ALtysC',
                                        1,
                                        1,
                                        {'g': ('ALtysG', 1, 1, {'name': 'g2'}), 'name': 'c1b'}),
                                       ('ref', 'arep_ltys_c', 2)],
                              'one': ('ref', 'arep_ltys_c', 2),
                              'title': 'p2'},
                             {'bag': {},
                              'many': [{'_id': 1, '_table': 'arep_ltys_c'},
                                       {'_id': 2, '_table': 'arep_ltys_c'}],
                              'one': {'_id': 2, '_table': 'arep_ltys_c'},
                              'title': 'p2'}),
 ('lite-safe', 'refresh'): ({'bag': {},
                             'many': [('ALtysC',
                                       1,
                                       1,
                                       {'g': ('ALtysG', 1, 1, {'name': 'g2'}), 'name': 'c1b'}),
                                      None],
                             'one': None,
                             'title': 'p2'},
                            {'bag': {},
                             'many': [{'_id': 1, '_table': 'arep_ltys_c'}, None],
                             'one': None,
                             'title': 'p2'}),
 ('pydantic', 'all'): ('load raised', 'ValidationError'),
 ('pydantic', 'first'): ({'bag': {},
                          'many': [('ATyC',
                                    1,
                                    '-',
                                    {'g': ('ATyG', 1, '-', {'name': 'g2'}), 'name': 'c1b'}),
                                   ('ATyC', None, '-', {'g': None, 'name': ''})],
                          'one': ('ATyC', None, '-', {'g': None, 'name': ''}),
                          'title': 'p2'},
                         ('ValueError', 'Related async model must be saved before saving parent')),
 ('pydantic', 'from_id'): ('load raised', 'ValidationError'),
 ('pydantic', 'from_ids'): ({'bag': {},
                             'many': [('ATyC',
                                       1,
                                       '-',
                                       {'g': ('ATyG', 1, '-', {'name': 'g2'}), 'name': 'c1b'}),
                                      ('ATyC', None, '-', {'g': None, 'name': ''})],
                             'one': ('ATyC', None, '-', {'g': None, 'name': ''}),
                             'title': 'p2'},
                            ('ValueError',
                             'Related async model must be saved before saving parent')),
 ('pydantic', 'refresh'): ('load raised', 'ValidationError'),
 ('safe', 'all'): ('load raised', 'ValidationError'),
 ('safe', 'first'): ({'bag': {},
                      'many': [('ATysC',
                                1,
                                1,
                                {'g': ('ATysG', 1, 1, {'name': 'g2'}), 'name': 'c1b'}),
                               ('ATysC', None, 0, {'g': None, 'name': ''})],
                      'one': ('ATysC', None, 0, {'g': None, 'name': ''}),
                      'title': 'p2'},
                     ('ValueError', 'Related async model must be saved before saving parent')),
 ('safe', 'from_id'): ('load raised', 'ValidationError'),
 ('safe', 'from_ids'): ({'bag': {},
                         'many': [('ATysC',
                                   1,
                                   1,
                                   {'g': ('ATysG', 1, 1, {'name': 'g2'}), 'name': 'c1b'}),
                                  ('ATysC', None, 0, {'g': None, 'name': ''})],
                         'one': ('ATysC', None, 0, {'g': None, 'name': ''}),
                         'title': 'p2'},
                        ('ValueError', 'Related async model must be saved before saving parent')),
 ('safe', 'refresh'): ('load raised', 'ValidationError')}
UNFOLLOWED_EXPECTED: dict = {('id-str', 'lite', 'all'): ({'bag': {},
                              'many': [],
                              'one': ('ref', 'arep_lty_c', 'x'),
                              'title': 'h'},
                             {'bag': {},
                              'many': [],
                              'one': {'_id': 'x', '_table': 'arep_lty_c'},
                              'title': 'h'}),
 ('id-str', 'lite', 'first'): ({'bag': {},
                                'many': [],
                                'one': ('ref', 'arep_lty_c', 'x'),
                                'title': 'h'},
                               {'bag': {},
                                'many': [],
                                'one': {'_id': 'x', '_table': 'arep_lty_c'},
                                'title': 'h'}),
 ('id-str', 'lite', 'from_id'): ({'bag': {},
                                  'many': [],
                                  'one': ('ref', 'arep_lty_c', 'x'),
                                  'title': 'h'},
                                 {'bag': {},
                                  'many': [],
                                  'one': {'_id': 'x', '_table': 'arep_lty_c'},
                                  'title': 'h'}),
 ('id-str', 'lite', 'from_ids'): ({'bag': {},
                                   'many': [],
                                   'one': ('ref', 'arep_lty_c', 'x'),
                                   'title': 'h'},
                                  {'bag': {},
                                   'many': [],
                                   'one': {'_id': 'x', '_table': 'arep_lty_c'},
                                   'title': 'h'}),
 ('id-str', 'lite', 'refresh'): ({'bag': {},
                                  'many': [],
                                  'one': ('ref', 'arep_lty_c', 'x'),
                                  'title': 'h'},
                                 {'bag': {},
                                  'many': [],
                                  'one': {'_id': 'x', '_table': 'arep_lty_c'},
                                  'title': 'h'}),
 ('id-str', 'lite-safe', 'all'): ({'bag': {},
                                   'many': [],
                                   'one': ('ref', 'arep_ltys_c', 'x'),
                                   'title': 'h'},
                                  {'bag': {},
                                   'many': [],
                                   'one': {'_id': 'x', '_table': 'arep_ltys_c'},
                                   'title': 'h'}),
 ('id-str', 'lite-safe', 'first'): ({'bag': {},
                                     'many': [],
                                     'one': ('ref', 'arep_ltys_c', 'x'),
                                     'title': 'h'},
                                    {'bag': {},
                                     'many': [],
                                     'one': {'_id': 'x', '_table': 'arep_ltys_c'},
                                     'title': 'h'}),
 ('id-str', 'lite-safe', 'from_id'): ({'bag': {},
                                       'many': [],
                                       'one': ('ref', 'arep_ltys_c', 'x'),
                                       'title': 'h'},
                                      {'bag': {},
                                       'many': [],
                                       'one': {'_id': 'x', '_table': 'arep_ltys_c'},
                                       'title': 'h'}),
 ('id-str', 'lite-safe', 'from_ids'): ({'bag': {},
                                        'many': [],
                                        'one': ('ref', 'arep_ltys_c', 'x'),
                                        'title': 'h'},
                                       {'bag': {},
                                        'many': [],
                                        'one': {'_id': 'x', '_table': 'arep_ltys_c'},
                                        'title': 'h'}),
 ('id-str', 'lite-safe', 'refresh'): ({'bag': {},
                                       'many': [],
                                       'one': ('ref', 'arep_ltys_c', 'x'),
                                       'title': 'h'},
                                      {'bag': {},
                                       'many': [],
                                       'one': {'_id': 'x', '_table': 'arep_ltys_c'},
                                       'title': 'h'}),
 ('id-str', 'pydantic', 'all'): ({'bag': {},
                                  'many': [],
                                  'one': ('ATyC', None, '-', {'g': None, 'name': ''}),
                                  'title': 'h'},
                                 ('ValueError',
                                  'Related async model must be saved before saving parent')),
 ('id-str', 'pydantic', 'first'): ({'bag': {},
                                    'many': [],
                                    'one': ('ATyC', None, '-', {'g': None, 'name': ''}),
                                    'title': 'h'},
                                   ('ValueError',
                                    'Related async model must be saved before saving parent')),
 ('id-str', 'pydantic', 'from_id'): ({'bag': {},
                                      'many': [],
                                      'one': ('ATyC', None, '-', {'g': None, 'name': ''}),
                                      'title': 'h'},
                                     ('ValueError',
                                      'Related async model must be saved before saving parent')),
 ('id-str', 'pydantic', 'from_ids'): ({'bag': {},
                                       'many': [],
                                       'one': ('ATyC', None, '-', {'g': None, 'name': ''}),
                                       'title': 'h'},
                                      ('ValueError',
                                       'Related async model must be saved before saving parent')),
 ('id-str', 'pydantic', 'refresh'): ({'bag': {},
                                      'many': [],
                                      'one': ('ATyC', None, '-', {'g': None, 'name': ''}),
                                      'title': 'h'},
                                     ('ValueError',
                                      'Related async model must be saved before saving parent')),
 ('id-str', 'safe', 'all'): ({'bag': {},
                              'many': [],
                              'one': ('ATysC', None, 0, {'g': None, 'name': ''}),
                              'title': 'h'},
                             ('ValueError',
                              'Related async model must be saved before saving parent')),
 ('id-str', 'safe', 'first'): ({'bag': {},
                                'many': [],
                                'one': ('ATysC', None, 0, {'g': None, 'name': ''}),
                                'title': 'h'},
                               ('ValueError',
                                'Related async model must be saved before saving parent')),
 ('id-str', 'safe', 'from_id'): ({'bag': {},
                                  'many': [],
                                  'one': ('ATysC', None, 0, {'g': None, 'name': ''}),
                                  'title': 'h'},
                                 ('ValueError',
                                  'Related async model must be saved before saving parent')),
 ('id-str', 'safe', 'from_ids'): ({'bag': {},
                                   'many': [],
                                   'one': ('ATysC', None, 0, {'g': None, 'name': ''}),
                                   'title': 'h'},
                                  ('ValueError',
                                   'Related async model must be saved before saving parent')),
 ('id-str', 'safe', 'refresh'): ({'bag': {},
                                  'many': [],
                                  'one': ('ATysC', None, 0, {'g': None, 'name': ''}),
                                  'title': 'h'},
                                 ('ValueError',
                                  'Related async model must be saved before saving parent')),
 ('other-table', 'lite', 'all'): ({'bag': {},
                                   'many': [],
                                   'one': ('ALtyC', 1, '-', {'g': None, 'name': 'g2'}),
                                   'title': 'h'},
                                  {'bag': {},
                                   'many': [],
                                   'one': {'_id': 1, '_table': 'arep_lty_c'},
                                   'title': 'h'}),
 ('other-table', 'lite', 'first'): ({'bag': {},
                                     'many': [],
                                     'one': ('ALtyC', 1, '-', {'g': None, 'name': 'g2'}),
                                     'title': 'h'},
                                    {'bag': {},
                                     'many': [],
                                     'one': {'_id': 1, '_table': 'arep_lty_c'},
                                     'title': 'h'}),
 ('other-table', 'lite', 'from_id'): ({'bag': {},
                                       'many': [],
                                       'one': ('ALtyG', 1, '-', {'name': 'g2'}),
                                       'title': 'h'},
                                      {'bag': {},
                                       'many': [],
                                       'one': {'_id': 1, '_table': 'arep_lty_g'},
                                       'title': 'h'}),
 ('other-table', 'lite', 'from_ids'): ({'bag': {},
                                        'many': [],
                                        'one': ('ALtyC', 1, '-', {'g': None, 'name': 'g2'}),
                                        'title': 'h'},
                                       {'bag': {},
                                        'many': [],
                                        'one': {'_id': 1, '_table': 'arep_lty_c'},
                                        'title': 'h'}),
 ('other-table', 'lite', 'refresh'): ({'bag': {},
                                       'many': [],
                                       'one': ('ALtyG', 1, '-', {'name': 'g2'}),
                                       'title': 'h'},
                                      {'bag': {},
                                       'many': [],
                                       'one': {'_id': 1, '_table': 'arep_lty_g'},
                                       'title': 'h'}),
 ('other-table', 'lite-safe', 'all'): ({'bag': {},
                                        'many': [],
                                        'one': ('ALtysC', 1, 0, {'g': None, 'name': 'g2'}),
                                        'title': 'h'},
                                       {'bag': {},
                                        'many': [],
                                        'one': {'_id': 1, '_table': 'arep_ltys_c'},
                                        'title': 'h'}),
 ('other-table', 'lite-safe', 'first'): ({'bag': {},
                                          'many': [],
                                          'one': ('ALtysC', 1, 0, {'g': None, 'name': 'g2'}),
                                          'title': 'h'},
                                         {'bag': {},
                                          'many': [],
                                          'one': {'_id': 1, '_table': 'arep_ltys_c'},
                                          'title': 'h'}),
 ('other-table', 'lite-safe', 'from_id'): ({'bag': {},
                                            'many': [],
                                            'one': ('ALtysG', 1, 1, {'name': 'g2'}),
                                            'title': 'h'},
                                           {'bag': {},
                                            'many': [],
                                            'one': {'_id': 1, '_table': 'arep_ltys_g'},
                                            'title': 'h'}),
 ('other-table', 'lite-safe', 'from_ids'): ({'bag': {},
                                             'many': [],
                                             'one': ('ALtysC', 1, 0, {'g': None, 'name': 'g2'}),
                                             'title': 'h'},
                                            {'bag': {},
                                             'many': [],
                                             'one': {'_id': 1, '_table': 'arep_ltys_c'},
                                             'title': 'h'}),
 ('other-table', 'lite-safe', 'refresh'): ({'bag': {},
                                            'many': [],
                                            'one': ('ALtysG', 1, 1, {'name': 'g2'}),
                                            'title': 'h'},
                                           {'bag': {},
                                            'many': [],
                                            'one': {'_id': 1, '_table': 'arep_ltys_g'},
                                            'title': 'h'}),
 ('other-table', 'pydantic', 'all'): ({'bag': {},
                                       'many': [],
                                       'one': ('ATyC', None, '-', {'g': None, 'name': 'g2'}),
                                       'title': 'h'},
                                      ('ValueError',
                                       'Related async model must be saved before saving parent')),
 ('other-table', 'pydantic', 'first'): ({'bag': {},
                                         'many': [],
                                         'one': ('ATyC', None, '-', {'g': None, 'name': 'g2'}),
                                         'title': 'h'},
                                        ('ValueError',
                                         'Related async model must be saved before saving parent')),
 ('other-table', 'pydantic', 'from_id'): ('load raised', 'ValidationError'),
 ('other-table', 'pydantic', 'from_ids'): ({'bag': {},
                                            'many': [],
                                            'one': ('ATyC', None, '-', {'g': None, 'name': 'g2'}),
                                            'title': 'h'},
                                           ('ValueError',
                                            'Related async model must be saved before saving '
                                            'parent')),
 ('other-table', 'pydantic', 'refresh'): ('load raised', 'ValidationError'),
 ('other-table', 'safe', 'all'): ({'bag': {},
                                   'many': [],
                                   'one': ('ATysC', None, 0, {'g': None, 'name': 'g2'}),
                                   'title': 'h'},
                                  ('ValueError',
                                   'Related async model must be saved before saving parent')),
 ('other-table', 'safe', 'first'): ({'bag': {},
                                     'many': [],
                                     'one': ('ATysC', None, 0, {'g': None, 'name': 'g2'}),
                                     'title': 'h'},
                                    ('ValueError',
                                     'Related async model must be saved before saving parent')),
 ('other-table', 'safe', 'from_id'): ('load raised', 'ValidationError'),
 ('other-table', 'safe', 'from_ids'): ({'bag': {},
                                        'many': [],
                                        'one': ('ATysC', None, 0, {'g': None, 'name': 'g2'}),
                                        'title': 'h'},
                                       ('ValueError',
                                        'Related async model must be saved before saving parent')),
 ('other-table', 'safe', 'refresh'): ('load raised', 'ValidationError')}
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
    assert (got.one._id, got.one._version, got.loose) == (1, 0, {"name": "c", "_id": 1})
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
    assert first == {"one": (c_cls,)}
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
