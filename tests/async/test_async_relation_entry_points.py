"""What each async way into a row returns for its references, pinned on every async base.

The async twin of ``tests/test_relation_entry_points.py``: the same fixture, the same
five ways in (``from_id``, ``refresh``, ``from_ids``, ``query().all()``,
``query().first()``), described field by field and compared with the base's answer
and the SELECTs it ran.
"""

from __future__ import annotations

import dataclasses
import warnings
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

import pytest
import pytest_asyncio
from pydantic import Field as PydField
from sqler import AsyncSQLerDB
from sqler.adapter import AsyncSQLiteAdapter
from sqler.models import AsyncSQLerModel, AsyncSQLerSafeModel
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel


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
        # the pydantic safe base's from_id and refresh resolve no reference
        assert got.loose == ref(ct, c1)
    else:
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
                    'many': [('dict', 1, {'name': 'c1', 'other': None}),
                             ('dict',
                              2,
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
                      'many': [('dict', 1, {'name': 'c1', 'other': None}),
                               ('dict',
                                2,
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
                         'many': [('dict', 1, {'name': 'c1', 'other': None}),
                                  ('dict',
                                   2,
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
                         'many': [('dict', 1, {'name': 'c1', 'other': None}),
                                  ('dict',
                                   2,
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
                           'many': [('dict', 1, {'name': 'c1', 'other': None}),
                                    ('dict',
                                     2,
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
                              'many': [('dict', 1, {'name': 'c1', 'other': None}),
                                       ('dict',
                                        2,
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
                        'one': ('Child', None, '-', {'name': 'c1', 'other': None}),
                        'many': [('Child', None, '-', {'name': 'c1', 'other': None}),
                                 ('Child',
                                  None,
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
                          'one': ('Child', None, '-', {'name': 'c1', 'other': None}),
                          'many': [('Child', None, '-', {'name': 'c1', 'other': None}),
                                   ('Child',
                                    None,
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
                             'one': ('Child', None, '-', {'name': 'c1', 'other': None}),
                             'many': [('Child', None, '-', {'name': 'c1', 'other': None}),
                                      ('Child',
                                       None,
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
                    'one': ('Child', None, 0, {'name': 'c1', 'other': None}),
                    'many': [('Child', None, 0, {'name': 'c1', 'other': None}),
                             ('Child',
                              None,
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
                      'one': ('Child', None, 0, {'name': 'c1', 'other': None}),
                      'many': [('Child', None, 0, {'name': 'c1', 'other': None}),
                               ('Child',
                                None,
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
                        'one': ('Child', None, 0, {'name': '', 'other': None}),
                        'many': [('Child', None, 0, {'name': '', 'other': None}),
                                 ('Child', None, 0, {'name': '', 'other': None})],
                        'loose': ('ref', 'arep_sf_child', 1),
                        'deep': ('ref', 'arep_sf_child', 2),
                        'cyc': ('ref', 'arep_sf_child', 4),
                        'me': ('ref', 'arep_sf_child', 6),
                        'gone': ('ref', 'arep_sf_child', 3),
                        'nowhere': ('ref', 'arep_nobody', 1)},
                       1),
 ('safe', 'from_ids'): ({'name': 'p',
                         'one': ('Child', None, 0, {'name': 'c1', 'other': None}),
                         'many': [('Child', None, 0, {'name': 'c1', 'other': None}),
                                  ('Child',
                                   None,
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
                        'one': ('Child', None, 0, {'name': '', 'other': None}),
                        'many': [('Child', None, 0, {'name': '', 'other': None}),
                                 ('Child', None, 0, {'name': '', 'other': None})],
                        'loose': ('ref', 'arep_sf_child', 1),
                        'deep': ('ref', 'arep_sf_child', 2),
                        'cyc': ('ref', 'arep_sf_child', 4),
                        'me': ('ref', 'arep_sf_child', 6),
                        'gone': ('ref', 'arep_sf_child', 3),
                        'nowhere': ('ref', 'arep_nobody', 1)},
                       1)}


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
