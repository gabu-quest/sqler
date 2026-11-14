"""What each way into a row returns for its references, pinned on every sync base.

One fixture per base: a parent row holding a typed reference, a list of typed
references, an untyped field holding a reference, a reference two levels down, a
cycle of two rows, a row that points at itself, a deleted row and a reference into a
table no class owns. Every way in (``from_id``, ``refresh``, ``from_ids``,
``query().all()``, ``query().first()``) is described field by field (type, ``_id``,
``_version``) and compared with the answer the base gave, with the SELECTs it ran.

The ways in do not all agree; ``EXPECTED`` is the record of where they differ.
"""

from __future__ import annotations

import dataclasses
import warnings
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest
from pydantic import Field as PydField
from sqler import SQLerDB, SQLerModel
from sqler.models import SQLerSafeModel
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel

# ---- the classes, one pair per base -------------------------------------------------


class Pd_Child(SQLerModel):
    __tablename__ = "rep_pd_child"
    name: str = ""
    other: Any = None


class Pd_Parent(SQLerModel):
    __tablename__ = "rep_pd_parent"
    name: str = ""
    one: Optional[Pd_Child] = None
    many: list[Pd_Child] = PydField(default_factory=list)
    loose: Any = None
    deep: Any = None
    cyc: Any = None
    me: Any = None
    gone: Any = None
    nowhere: Any = None


class Sf_Child(SQLerSafeModel):
    __tablename__ = "rep_sf_child"
    name: str = ""
    other: Any = None


class Sf_Parent(SQLerSafeModel):
    __tablename__ = "rep_sf_parent"
    name: str = ""
    one: Optional[Sf_Child] = None
    many: list[Sf_Child] = PydField(default_factory=list)
    loose: Any = None
    deep: Any = None
    cyc: Any = None
    me: Any = None
    gone: Any = None
    nowhere: Any = None


@dataclass
class Lt_Child(SQLerLiteModel):
    __tablename__ = "rep_lt_child"
    name: str = ""
    other: Any = None


@dataclass
class Lt_Parent(SQLerLiteModel):
    __tablename__ = "rep_lt_parent"
    name: str = ""
    one: Optional[Lt_Child] = None
    many: list[Lt_Child] = field(default_factory=list)
    loose: Any = None
    deep: Any = None
    cyc: Any = None
    me: Any = None
    gone: Any = None
    nowhere: Any = None


@dataclass
class Ls_Child(SQLerLiteSafeModel):
    __tablename__ = "rep_ls_child"
    name: str = ""
    other: Any = None


@dataclass
class Ls_Parent(SQLerLiteSafeModel):
    __tablename__ = "rep_ls_parent"
    name: str = ""
    one: Optional[Ls_Child] = None
    many: list[Ls_Child] = field(default_factory=list)
    loose: Any = None
    deep: Any = None
    cyc: Any = None
    me: Any = None
    gone: Any = None
    nowhere: Any = None


BASES = {
    "pydantic": (Pd_Child, Pd_Parent),
    "safe": (Sf_Child, Sf_Parent),
    "lite": (Lt_Child, Lt_Parent),
    "lite-safe": (Ls_Child, Ls_Parent),
}
WAYS = ["from_id", "refresh", "from_ids", "all", "first"]
NOBODY = "rep_nobody"


def ref(table: str, rid: Any) -> dict:
    return {"_table": table, "_id": rid}


def build(db: SQLerDB, child: type, parent: type) -> int:
    """The fixture rows; returns the parent's id.

    c1 plain; c2 -> c1 (two levels down); c3 deleted; c4 <-> c5 (cycle of two);
    c6 -> c6 (itself); ``rep_nobody`` row 1 in a table no class owns.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        child.set_db(db)
        parent.set_db(db)
    ct = child.__tablename__
    c1 = db.insert_document(ct, {"name": "c1", "other": None})
    c2 = db.insert_document(ct, {"name": "c2", "other": ref(ct, c1)})
    c3 = db.insert_document(ct, {"name": "c3", "other": None})
    c4 = db.insert_document(ct, {"name": "c4", "other": None})
    c5 = db.insert_document(ct, {"name": "c5", "other": ref(ct, c4)})
    db.upsert_document(ct, c4, {"name": "c4", "other": ref(ct, c5)})
    c6 = db.insert_document(ct, {"name": "c6", "other": None})
    db.upsert_document(ct, c6, {"name": "c6", "other": ref(ct, c6)})
    db.delete_document(ct, c3)
    nb = db.insert_document(NOBODY, {"name": "nobody"})
    return db.insert_document(
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


def selects_of(db: SQLerDB, action):
    traced: list[str] = []
    conn = db.adapter._conn()
    conn.set_trace_callback(traced.append)
    try:
        result = action()
    finally:
        conn.set_trace_callback(None)
    return result, [s for s in traced if s.lstrip().upper().startswith("SELECT")]


def run_way(db: SQLerDB, parent: type, pid: int, way: str):
    """``(instance, selects)`` of reading the parent row one way."""
    if way == "refresh":
        inst = parent.query().resolve(False).first()
        return selects_of(db, inst.refresh)
    action = {
        "from_id": lambda: parent.from_id(pid),
        "from_ids": lambda: parent.from_ids([pid])[0],
        "all": lambda: parent.query().all()[0],
        "first": lambda: parent.query().first(),
    }[way]
    return selects_of(db, action)


@pytest.fixture
def make_db(tmp_path):
    dbs: list[SQLerDB] = []

    def make(name: str = "r.db") -> SQLerDB:
        db = SQLerDB.on_disk(str(tmp_path / name))
        dbs.append(db)
        return db

    yield make
    for db in dbs:
        db.close()


# ---- item 1: every way in, every base -----------------------------------------------

@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
def test_each_way_in_returns_what_the_base_returned(make_db, base, way):
    child, parent = BASES[base]
    db = make_db()
    pid = build(db, child, parent)
    got, sel = run_way(db, parent, pid, way)
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


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("bad", BAD_REFS)
@pytest.mark.parametrize("base", sorted(BASES))
def test_unusable_reference_is_left_as_it_is_and_never_sent_to_sql(make_db, base, bad, way):
    child, parent = BASES[base]
    db = make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        child.set_db(db)
        parent.set_db(db)
    ct = child.__tablename__
    db.insert_document(ct, {"name": "c1"})
    value = {k: (ct if v == "TBL" else [ct] if v == ["TBL"] else v) for k, v in bad.items()}
    pid = db.insert_document(parent.__tablename__, {"name": "p", "loose": value})
    got, sel = run_way(db, parent, pid, way)
    assert got.loose == value
    assert [s for s in sel if ct in s] == []


def _ids_in(statement: str) -> list[str]:
    inside = statement[statement.index("IN (") + 4 : statement.index(")", statement.index("IN ("))]
    return [p.strip() for p in inside.split(",")]


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
def test_the_same_reference_twice_in_one_row_is_read_once(make_db, base, way):
    child, parent = BASES[base]
    db = make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        child.set_db(db)
        parent.set_db(db)
    ct = child.__tablename__
    c1 = db.insert_document(ct, {"name": "c1"})
    pid = db.insert_document(
        parent.__tablename__, {"name": "p", "loose": ref(ct, c1), "deep": [ref(ct, c1)]}
    )
    got, sel = run_way(db, parent, pid, way)
    reads = [s for s in sel if f"FROM {ct} " in s and " IN (" in s]
    expected = 0 if (base == "safe" and way in ("from_id", "refresh")) else 1
    assert len(reads) == expected
    for s in reads:
        assert _ids_in(s) == [str(c1)]


@pytest.mark.parametrize("base", sorted(BASES))
def test_the_same_reference_in_two_rows_of_one_all_is_read_once(make_db, base):
    child, parent = BASES[base]
    db = make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        child.set_db(db)
        parent.set_db(db)
    ct = child.__tablename__
    c1 = db.insert_document(ct, {"name": "c1"})
    for n in ("p1", "p2"):
        db.insert_document(parent.__tablename__, {"name": n, "loose": ref(ct, c1)})
    got, sel = selects_of(db, lambda: parent.query().all())
    reads = [s for s in sel if f"FROM {ct} " in s]
    assert len(reads) == 1
    assert _ids_in(reads[0]) == [str(c1)]
    assert [g.loose["name"] for g in got] == ["c1", "c1"]


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
def test_a_thousand_references_into_one_table_are_one_statement(make_db, base, way):
    child, parent = BASES[base]
    db = make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        child.set_db(db)
        parent.set_db(db)
    ct = child.__tablename__
    ids = [db.insert_document(ct, {"name": f"c{i}"}) for i in range(1000)]
    pid = db.insert_document(parent.__tablename__, {"name": "p", "loose": [ref(ct, i) for i in ids]})
    got, sel = run_way(db, parent, pid, way)
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


@pytest.mark.parametrize("base", sorted(BASES))
def test_a_chain_six_levels_deep_through_all_stops_at_the_base_depth(make_db, base):
    child, parent = BASES[base]
    db = make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        child.set_db(db)
        parent.set_db(db)
    ct = child.__tablename__
    prev = None
    ids = []
    for i in range(7):
        prev = db.insert_document(
            ct, {"name": f"c{i}", "other": None if prev is None else ref(ct, prev)}
        )
        ids.append(prev)
    # parent -> c6 -> c5 -> c4 -> c3 -> c2 -> c1 -> c0
    db.insert_document(parent.__tablename__, {"name": "p", "loose": ref(ct, ids[6])})
    got = parent.query().all()[0]
    node = got.loose
    names = []
    for _ in range(5):
        names.append(node["name"])
        node = node["other"]
    assert names == ["c6", "c5", "c4", "c3", "c2"]
    # the sixth level is left as the stored reference
    assert node == ref(ct, ids[1])


def _tables(db: SQLerDB) -> set[str]:
    cur = db.adapter.execute("SELECT name FROM sqlite_master WHERE type='table';")
    return {r[0] for r in cur.fetchall()}


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
def test_a_reference_into_a_dropped_table_of_a_bound_class_reads_as_none(make_db, base, way):
    child, parent = BASES[base]
    db = make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        child.set_db(db)
        parent.set_db(db)
    ct = child.__tablename__
    c1 = db.insert_document(ct, {"name": "c1"})
    pid = db.insert_document(parent.__tablename__, {"name": "p", "loose": ref(ct, c1)})
    db.adapter.execute(f"DROP TABLE {ct};")
    db.adapter.commit()
    got, _ = run_way(db, parent, pid, way)
    if base == "safe" and way in ("from_id", "refresh"):
        # the pydantic safe base's from_id and refresh resolve no reference
        assert got.loose == ref(ct, c1)
    else:
        assert got.loose is None
    assert ct not in _tables(db)


# ---- the record: what the base returned ---------------------------------------------

# (base, way) -> (fields of the parent row, SELECTs run)
EXPECTED: dict = {('lite', 'from_id'): ({'name': 'p',
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
                                           {'name': 'c5', 'other': ('ref', 'rep_lt_child', 4)})}),
                        'me': ('Child',
                               6,
                               '-',
                               {'name': 'c6', 'other': ('ref', 'rep_lt_child', 6)}),
                        'gone': None,
                        'nowhere': ('ref', 'rep_nobody', 1)},
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
                                           {'name': 'c5', 'other': ('ref', 'rep_lt_child', 4)})}),
                        'me': ('Child',
                               6,
                               '-',
                               {'name': 'c6', 'other': ('ref', 'rep_lt_child', 6)}),
                        'gone': None,
                        'nowhere': ('ref', 'rep_nobody', 1)},
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
                                            {'name': 'c5', 'other': ('ref', 'rep_lt_child', 4)})}),
                         'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'rep_lt_child', 6)}),
                         'gone': ('ref', 'rep_lt_child', 3),
                         'nowhere': ('dict', 1, {'name': 'nobody'})},
                        4),
 ('lite', 'all'): ({'name': 'p',
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
                                       {'name': 'c5', 'other': ('ref', 'rep_lt_child', 4)})}),
                    'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'rep_lt_child', 6)}),
                    'gone': ('ref', 'rep_lt_child', 3),
                    'nowhere': ('dict', 1, {'name': 'nobody'})},
                   4),
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
                                         {'name': 'c5', 'other': ('ref', 'rep_lt_child', 4)})}),
                      'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'rep_lt_child', 6)}),
                      'gone': ('ref', 'rep_lt_child', 3),
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
                                                 'other': ('ref', 'rep_ls_child', 4)})}),
                             'me': ('Child',
                                    6,
                                    0,
                                    {'name': 'c6', 'other': ('ref', 'rep_ls_child', 6)}),
                             'gone': None,
                             'nowhere': ('ref', 'rep_nobody', 1)},
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
                                                 'other': ('ref', 'rep_ls_child', 4)})}),
                             'me': ('Child',
                                    6,
                                    0,
                                    {'name': 'c6', 'other': ('ref', 'rep_ls_child', 6)}),
                             'gone': None,
                             'nowhere': ('ref', 'rep_nobody', 1)},
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
                                                  'other': ('ref', 'rep_ls_child', 4)})}),
                              'me': ('dict',
                                     6,
                                     {'name': 'c6', 'other': ('ref', 'rep_ls_child', 6)}),
                              'gone': ('ref', 'rep_ls_child', 3),
                              'nowhere': ('dict', 1, {'name': 'nobody'})},
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
                                            {'name': 'c5', 'other': ('ref', 'rep_ls_child', 4)})}),
                         'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'rep_ls_child', 6)}),
                         'gone': ('ref', 'rep_ls_child', 3),
                         'nowhere': ('dict', 1, {'name': 'nobody'})},
                        4),
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
                                               'other': ('ref', 'rep_ls_child', 4)})}),
                           'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'rep_ls_child', 6)}),
                           'gone': ('ref', 'rep_ls_child', 3),
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
                                                'other': ('ref', 'rep_pd_child', 4)})}),
                            'me': ('Child',
                                   6,
                                   '-',
                                   {'name': 'c6', 'other': ('ref', 'rep_pd_child', 6)}),
                            'gone': None,
                            'nowhere': ('ref', 'rep_nobody', 1)},
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
                                                'other': ('ref', 'rep_pd_child', 4)})}),
                            'me': ('Child',
                                   6,
                                   '-',
                                   {'name': 'c6', 'other': ('ref', 'rep_pd_child', 6)}),
                            'gone': None,
                            'nowhere': ('ref', 'rep_nobody', 1)},
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
                                                 'other': ('ref', 'rep_pd_child', 4)})}),
                             'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'rep_pd_child', 6)}),
                             'gone': ('ref', 'rep_pd_child', 3),
                             'nowhere': ('dict', 1, {'name': 'nobody'})},
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
                                           {'name': 'c5', 'other': ('ref', 'rep_pd_child', 4)})}),
                        'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'rep_pd_child', 6)}),
                        'gone': ('ref', 'rep_pd_child', 3),
                        'nowhere': ('dict', 1, {'name': 'nobody'})},
                       4),
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
                                             {'name': 'c5', 'other': ('ref', 'rep_pd_child', 4)})}),
                          'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'rep_pd_child', 6)}),
                          'gone': ('ref', 'rep_pd_child', 3),
                          'nowhere': ('dict', 1, {'name': 'nobody'})},
                         4),
 ('safe', 'from_id'): ({'name': 'p',
                        'one': ('Child', None, 0, {'name': '', 'other': None}),
                        'many': [('Child', None, 0, {'name': '', 'other': None}),
                                 ('Child', None, 0, {'name': '', 'other': None})],
                        'loose': ('ref', 'rep_sf_child', 1),
                        'deep': ('ref', 'rep_sf_child', 2),
                        'cyc': ('ref', 'rep_sf_child', 4),
                        'me': ('ref', 'rep_sf_child', 6),
                        'gone': ('ref', 'rep_sf_child', 3),
                        'nowhere': ('ref', 'rep_nobody', 1)},
                       1),
 ('safe', 'refresh'): ({'name': 'p',
                        'one': ('Child', None, 0, {'name': '', 'other': None}),
                        'many': [('Child', None, 0, {'name': '', 'other': None}),
                                 ('Child', None, 0, {'name': '', 'other': None})],
                        'loose': ('ref', 'rep_sf_child', 1),
                        'deep': ('ref', 'rep_sf_child', 2),
                        'cyc': ('ref', 'rep_sf_child', 4),
                        'me': ('ref', 'rep_sf_child', 6),
                        'gone': ('ref', 'rep_sf_child', 3),
                        'nowhere': ('ref', 'rep_nobody', 1)},
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
                                            {'name': 'c5', 'other': ('ref', 'rep_sf_child', 4)})}),
                         'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'rep_sf_child', 6)}),
                         'gone': ('ref', 'rep_sf_child', 3),
                         'nowhere': ('dict', 1, {'name': 'nobody'})},
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
                                       {'name': 'c5', 'other': ('ref', 'rep_sf_child', 4)})}),
                    'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'rep_sf_child', 6)}),
                    'gone': ('ref', 'rep_sf_child', 3),
                    'nowhere': ('dict', 1, {'name': 'nobody'})},
                   4),
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
                                         {'name': 'c5', 'other': ('ref', 'rep_sf_child', 4)})}),
                      'me': ('dict', 6, {'name': 'c6', 'other': ('ref', 'rep_sf_child', 6)}),
                      'gone': ('ref', 'rep_sf_child', 3),
                      'nowhere': ('dict', 1, {'name': 'nobody'})},
                     4)}


# ---- hostile inputs whose answer this stream sets ----------------------------------


def _bound(make_db, base: str):
    child, parent = BASES[base]
    db = make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        child.set_db(db)
        parent.set_db(db)
    return db, child, parent, child.__tablename__


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
def test_a_reference_shaped_dict_that_is_not_usable_is_not_walked_into(make_db, base, way):
    """``{"_table", "_id"}`` with an id that is not one is left as it is, whole: the
    reference inside it is not loaded either, by any way in (on the base ``from_id`` and
    ``refresh`` loaded it, the query did not)."""
    db, child, parent, ct = _bound(make_db, base)
    c1 = db.insert_document(ct, {"name": "c1"})
    shell = {"_table": ct, "_id": "x", "inner": ref(ct, c1)}
    pid = db.insert_document(parent.__tablename__, {"name": "p", "loose": shell})
    got, _ = run_way(db, parent, pid, way)
    if base.startswith("lite") and way != "refresh":
        # the lite base turns a dict naming a known table into that class
        version = 0 if base == "lite-safe" else "-"
        assert describe(got.loose) == ("Child", "x", version, {"name": "", "other": None})
    else:
        assert got.loose == shell


@pytest.mark.parametrize("way", WAYS)
@pytest.mark.parametrize("base", sorted(BASES))
def test_an_id_past_sqlite_integer_range_is_left_as_it_is(make_db, base, way):
    """On the base ``from_id`` and ``refresh`` raised OverflowError; the query left it."""
    db, child, parent, ct = _bound(make_db, base)
    pid = db.insert_document(parent.__tablename__, {"name": "p", "deep": ref(ct, 2**63)})
    got, sel = run_way(db, parent, pid, way)
    assert got.deep == ref(ct, 2**63)
    assert [s for s in sel if ct in s] == []
