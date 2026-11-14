"""What each way into a row returns for its references, pinned on every sync base.

One fixture per base: a parent row holding a typed reference, a list of typed
references, an untyped field holding a reference, a reference two levels down, a
cycle of two rows, a row that points at itself, a deleted row and a reference into a
table no class owns. Every way in (``from_id``, ``refresh``, ``from_ids``,
``query().all()``, ``query().first()``) is described field by field (type, ``_id``,
``_version``) and compared with the answer the base gave, with the SELECTs it ran.

Only ``one`` and ``many`` are typed: every other field holds its reference in a field with
no model type, which no way in follows, so it reads back as the stored reference (the
typed versions of the deeper cases are pinned further down, on the ``Ty*`` classes).
``EXPECTED`` is the record.
"""

from __future__ import annotations

import dataclasses
import json
import typing
import warnings
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest
from pydantic import Field as PydField
from pydantic import field_validator
from sqler import F, SQLerDB, SQLerModel, SQLerMsgspecModel
from sqler.exceptions import StaleVersionError
from sqler.logging import query_logger
from sqler.models import SQLerQuerySet, SQLerSafeModel, _refs
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel
from sqler.pool import PooledSQLerDB
from sqler.query import SQLerQuery

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
        parent.__tablename__, {"name": "p", "one": ref(ct, c1), "many": [ref(ct, c1)]}
    )
    got, sel = run_way(db, parent, pid, way)
    reads = [s for s in sel if f"FROM {ct} " in s and " IN (" in s]
    assert len(reads) == 1
    assert _ids_in(reads[0]) == [str(c1)]
    assert (got.one.name, [c.name for c in got.many]) == ("c1", ["c1"])


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
        db.insert_document(parent.__tablename__, {"name": n, "one": ref(ct, c1)})
    got, sel = selects_of(db, lambda: parent.query().all())
    reads = [s for s in sel if f"FROM {ct} " in s]
    assert len(reads) == 1
    assert _ids_in(reads[0]) == [str(c1)]
    assert [g.one.name for g in got] == ["c1", "c1"]


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
    pid = db.insert_document(parent.__tablename__, {"name": "p", "many": [ref(ct, i) for i in ids]})
    got, sel = run_way(db, parent, pid, way)
    reads = [s for s in sel if f"FROM {ct} " in s]
    assert len(reads) == 1
    assert len(got.many) == 1000
    assert (got.many[999].name, got.many[999]._id) == ("c999", ids[999])


@pytest.mark.parametrize("base", sorted(BASES))
def test_a_chain_six_levels_deep_through_all_stops_at_the_base_depth(make_db, base):
    n_cls = TYPED[base][3]
    db = make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        n_cls.set_db(db)
    nt = n_cls.__tablename__
    prev = None
    ids = []
    for i in range(7):
        prev = db.insert_document(nt, {"name": f"n{i}", "one": None if prev is None else ref(nt, prev)})
        ids.append(prev)
    # n6 -> n5 -> n4 -> n3 -> n2 -> n1 -> n0
    node = n_cls.query().filter(F("_id") == ids[6]).all()[0]
    names = []
    for _ in range(6):
        names.append(node.name)
        node = node.one
    assert names == ["n6", "n5", "n4", "n3", "n2", "n1"]
    # the sixth level is left as the stored reference; the parent's validation says what it
    # says about it (the pydantic bases build an empty model from it)
    assert pic(node) == CHAIN_CUT[base]


CHAIN_CUT: dict = {
    "lite": ("ref", "rep_lty_n", 1),
    "lite-safe": ("ref", "rep_ltys_n", 1),
    "pydantic": ("TyN", None, "-", {"name": "", "one": None}),
    "safe": ("TysN", None, 0, {"name": "", "one": None}),
}


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
    pid = db.insert_document(parent.__tablename__, {"name": "p", "one": ref(ct, c1)})
    db.adapter.execute(f"DROP TABLE {ct};")
    db.adapter.commit()
    got, _ = run_way(db, parent, pid, way)
    assert got.one is None
    assert ct not in _tables(db)


@pytest.mark.parametrize("way", ["from_ids", "all", "first"])
@pytest.mark.parametrize("base", ["safe", "lite-safe"])
def test_a_reference_into_a_versioned_table_the_database_never_kept_reads_as_none(
    make_db, base, way
):
    """The child class is bound inside a transaction that rolls back: its table is gone and
    the database remembers nothing about it, where a dropped table is still remembered."""
    child, parent = BASES[base]
    db = make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        parent.set_db(db)
        with pytest.raises(RuntimeError, match="undo"):
            with db.transaction():
                child.set_db(db)
                raise RuntimeError("undo")
    ct = child.__tablename__
    assert ct not in _tables(db)
    pid = db.insert_document(parent.__tablename__, {"name": "p", "one": ref(ct, 1)})
    got, _ = run_way(db, parent, pid, way)
    assert got.one is None
    assert ct not in _tables(db)


# ---- the record: what the base returned ---------------------------------------------

# (base, way) -> (fields of the parent row, SELECTs run)
EXPECTED: dict = {('lite', 'from_id'): ({'name': 'p',
                        'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                        'many': [('Child',
                                  1,
                                  '-',
                                  {'name': 'c1', 'other': None}),
                                 ('Child',
                                  2,
                                  '-',
                                  {'name': 'c2',
                                   'other': ('ref', 'rep_lt_child', 1)})],
                        'loose': ('ref', 'rep_lt_child', 1),
                        'deep': ('ref', 'rep_lt_child', 2),
                        'cyc': ('ref', 'rep_lt_child', 4),
                        'me': ('ref', 'rep_lt_child', 6),
                        'gone': ('ref', 'rep_lt_child', 3),
                        'nowhere': ('ref', 'rep_nobody', 1)},
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
                                   'other': ('ref', 'rep_lt_child', 1)})],
                        'loose': ('ref', 'rep_lt_child', 1),
                        'deep': ('ref', 'rep_lt_child', 2),
                        'cyc': ('ref', 'rep_lt_child', 4),
                        'me': ('ref', 'rep_lt_child', 6),
                        'gone': ('ref', 'rep_lt_child', 3),
                        'nowhere': ('ref', 'rep_nobody', 1)},
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
                                    'other': ('ref', 'rep_lt_child', 1)})],
                         'loose': ('ref', 'rep_lt_child', 1),
                         'deep': ('ref', 'rep_lt_child', 2),
                         'cyc': ('ref', 'rep_lt_child', 4),
                         'me': ('ref', 'rep_lt_child', 6),
                         'gone': ('ref', 'rep_lt_child', 3),
                         'nowhere': ('ref', 'rep_nobody', 1)},
                        2),
 ('lite', 'all'): ({'name': 'p',
                    'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                    'many': [('Child', 1, '-', {'name': 'c1', 'other': None}),
                             ('Child',
                              2,
                              '-',
                              {'name': 'c2',
                               'other': ('ref', 'rep_lt_child', 1)})],
                    'loose': ('ref', 'rep_lt_child', 1),
                    'deep': ('ref', 'rep_lt_child', 2),
                    'cyc': ('ref', 'rep_lt_child', 4),
                    'me': ('ref', 'rep_lt_child', 6),
                    'gone': ('ref', 'rep_lt_child', 3),
                    'nowhere': ('ref', 'rep_nobody', 1)},
                   2),
 ('lite', 'first'): ({'name': 'p',
                      'one': ('Child', 1, '-', {'name': 'c1', 'other': None}),
                      'many': [('Child', 1, '-', {'name': 'c1', 'other': None}),
                               ('Child',
                                2,
                                '-',
                                {'name': 'c2',
                                 'other': ('ref', 'rep_lt_child', 1)})],
                      'loose': ('ref', 'rep_lt_child', 1),
                      'deep': ('ref', 'rep_lt_child', 2),
                      'cyc': ('ref', 'rep_lt_child', 4),
                      'me': ('ref', 'rep_lt_child', 6),
                      'gone': ('ref', 'rep_lt_child', 3),
                      'nowhere': ('ref', 'rep_nobody', 1)},
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
                                        'other': ('ref', 'rep_ls_child', 1)})],
                             'loose': ('ref', 'rep_ls_child', 1),
                             'deep': ('ref', 'rep_ls_child', 2),
                             'cyc': ('ref', 'rep_ls_child', 4),
                             'me': ('ref', 'rep_ls_child', 6),
                             'gone': ('ref', 'rep_ls_child', 3),
                             'nowhere': ('ref', 'rep_nobody', 1)},
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
                                        'other': ('ref', 'rep_ls_child', 1)})],
                             'loose': ('ref', 'rep_ls_child', 1),
                             'deep': ('ref', 'rep_ls_child', 2),
                             'cyc': ('ref', 'rep_ls_child', 4),
                             'me': ('ref', 'rep_ls_child', 6),
                             'gone': ('ref', 'rep_ls_child', 3),
                             'nowhere': ('ref', 'rep_nobody', 1)},
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
                                         'other': ('ref', 'rep_ls_child', 1)})],
                              'loose': ('ref', 'rep_ls_child', 1),
                              'deep': ('ref', 'rep_ls_child', 2),
                              'cyc': ('ref', 'rep_ls_child', 4),
                              'me': ('ref', 'rep_ls_child', 6),
                              'gone': ('ref', 'rep_ls_child', 3),
                              'nowhere': ('ref', 'rep_nobody', 1)},
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
                                    'other': ('ref', 'rep_ls_child', 1)})],
                         'loose': ('ref', 'rep_ls_child', 1),
                         'deep': ('ref', 'rep_ls_child', 2),
                         'cyc': ('ref', 'rep_ls_child', 4),
                         'me': ('ref', 'rep_ls_child', 6),
                         'gone': ('ref', 'rep_ls_child', 3),
                         'nowhere': ('ref', 'rep_nobody', 1)},
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
                                      'other': ('ref', 'rep_ls_child', 1)})],
                           'loose': ('ref', 'rep_ls_child', 1),
                           'deep': ('ref', 'rep_ls_child', 2),
                           'cyc': ('ref', 'rep_ls_child', 4),
                           'me': ('ref', 'rep_ls_child', 6),
                           'gone': ('ref', 'rep_ls_child', 3),
                           'nowhere': ('ref', 'rep_nobody', 1)},
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
                                       'other': ('ref', 'rep_pd_child', 1)})],
                            'loose': ('ref', 'rep_pd_child', 1),
                            'deep': ('ref', 'rep_pd_child', 2),
                            'cyc': ('ref', 'rep_pd_child', 4),
                            'me': ('ref', 'rep_pd_child', 6),
                            'gone': ('ref', 'rep_pd_child', 3),
                            'nowhere': ('ref', 'rep_nobody', 1)},
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
                                       'other': ('ref', 'rep_pd_child', 1)})],
                            'loose': ('ref', 'rep_pd_child', 1),
                            'deep': ('ref', 'rep_pd_child', 2),
                            'cyc': ('ref', 'rep_pd_child', 4),
                            'me': ('ref', 'rep_pd_child', 6),
                            'gone': ('ref', 'rep_pd_child', 3),
                            'nowhere': ('ref', 'rep_nobody', 1)},
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
                                        'other': ('ref', 'rep_pd_child', 1)})],
                             'loose': ('ref', 'rep_pd_child', 1),
                             'deep': ('ref', 'rep_pd_child', 2),
                             'cyc': ('ref', 'rep_pd_child', 4),
                             'me': ('ref', 'rep_pd_child', 6),
                             'gone': ('ref', 'rep_pd_child', 3),
                             'nowhere': ('ref', 'rep_nobody', 1)},
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
                                   'other': ('ref', 'rep_pd_child', 1)})],
                        'loose': ('ref', 'rep_pd_child', 1),
                        'deep': ('ref', 'rep_pd_child', 2),
                        'cyc': ('ref', 'rep_pd_child', 4),
                        'me': ('ref', 'rep_pd_child', 6),
                        'gone': ('ref', 'rep_pd_child', 3),
                        'nowhere': ('ref', 'rep_nobody', 1)},
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
                                     'other': ('ref', 'rep_pd_child', 1)})],
                          'loose': ('ref', 'rep_pd_child', 1),
                          'deep': ('ref', 'rep_pd_child', 2),
                          'cyc': ('ref', 'rep_pd_child', 4),
                          'me': ('ref', 'rep_pd_child', 6),
                          'gone': ('ref', 'rep_pd_child', 3),
                          'nowhere': ('ref', 'rep_nobody', 1)},
                         2),
 ('safe', 'from_id'): ({'name': 'p',
                        'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                        'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                                 ('Child',
                                  2,
                                  0,
                                  {'name': 'c2',
                                   'other': ('ref', 'rep_sf_child', 1)})],
                        'loose': ('ref', 'rep_sf_child', 1),
                        'deep': ('ref', 'rep_sf_child', 2),
                        'cyc': ('ref', 'rep_sf_child', 4),
                        'me': ('ref', 'rep_sf_child', 6),
                        'gone': ('ref', 'rep_sf_child', 3),
                        'nowhere': ('ref', 'rep_nobody', 1)},
                       2),
 ('safe', 'refresh'): ({'name': 'p',
                        'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                        'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                                 ('Child',
                                  2,
                                  0,
                                  {'name': 'c2',
                                   'other': ('ref', 'rep_sf_child', 1)})],
                        'loose': ('ref', 'rep_sf_child', 1),
                        'deep': ('ref', 'rep_sf_child', 2),
                        'cyc': ('ref', 'rep_sf_child', 4),
                        'me': ('ref', 'rep_sf_child', 6),
                        'gone': ('ref', 'rep_sf_child', 3),
                        'nowhere': ('ref', 'rep_nobody', 1)},
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
                                    'other': ('ref', 'rep_sf_child', 1)})],
                         'loose': ('ref', 'rep_sf_child', 1),
                         'deep': ('ref', 'rep_sf_child', 2),
                         'cyc': ('ref', 'rep_sf_child', 4),
                         'me': ('ref', 'rep_sf_child', 6),
                         'gone': ('ref', 'rep_sf_child', 3),
                         'nowhere': ('ref', 'rep_nobody', 1)},
                        2),
 ('safe', 'all'): ({'name': 'p',
                    'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                    'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                             ('Child',
                              2,
                              0,
                              {'name': 'c2',
                               'other': ('ref', 'rep_sf_child', 1)})],
                    'loose': ('ref', 'rep_sf_child', 1),
                    'deep': ('ref', 'rep_sf_child', 2),
                    'cyc': ('ref', 'rep_sf_child', 4),
                    'me': ('ref', 'rep_sf_child', 6),
                    'gone': ('ref', 'rep_sf_child', 3),
                    'nowhere': ('ref', 'rep_nobody', 1)},
                   2),
 ('safe', 'first'): ({'name': 'p',
                      'one': ('Child', 1, 0, {'name': 'c1', 'other': None}),
                      'many': [('Child', 1, 0, {'name': 'c1', 'other': None}),
                               ('Child',
                                2,
                                0,
                                {'name': 'c2',
                                 'other': ('ref', 'rep_sf_child', 1)})],
                      'loose': ('ref', 'rep_sf_child', 1),
                      'deep': ('ref', 'rep_sf_child', 2),
                      'cyc': ('ref', 'rep_sf_child', 4),
                      'me': ('ref', 'rep_sf_child', 6),
                      'gone': ('ref', 'rep_sf_child', 3),
                      'nowhere': ('ref', 'rep_nobody', 1)},
                     2)}


# ---- hostile inputs at a typed position ---------------------------------------------


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
    reference inside it is not loaded either, by any way in, and no base turns it into a
    model."""
    db, child, parent, ct = _bound(make_db, base)
    c1 = db.insert_document(ct, {"name": "c1"})
    shell = {"_table": ct, "_id": "x", "inner": ref(ct, c1)}
    pid = db.insert_document(parent.__tablename__, {"name": "p", "loose": shell})
    got, _ = run_way(db, parent, pid, way)
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


# ---- typed references keep their row ------------------------------------------------
#
# A field annotated with a model class holds a model of that row on every way in: its
# ``_id`` is the row's id and, for a safe class, its ``_version`` the row's version. On
# the base the batch ways gave such a child ``_id`` None and the safe ``from_id`` built
# an empty one, and ``parent.save()`` then raised "Related model must be saved before
# saving parent". These tests load a parent and save it again: it must NOT raise that.


class TyG(SQLerModel):
    __tablename__ = "rep_ty_g"
    name: str = ""


class TyC(SQLerModel):
    __tablename__ = "rep_ty_c"
    name: str = ""
    g: Optional[TyG] = None


class TyP(SQLerModel):
    __tablename__ = "rep_ty_p"
    title: str = ""
    one: Optional[TyC] = None
    many: list[TyC] = PydField(default_factory=list)
    bag: dict[str, TyC] = PydField(default_factory=dict)


class TyN(SQLerModel):
    __tablename__ = "rep_ty_n"
    name: str = ""
    one: Optional["TyN"] = None


class TysG(SQLerSafeModel):
    __tablename__ = "rep_tys_g"
    name: str = ""


class TysC(SQLerSafeModel):
    __tablename__ = "rep_tys_c"
    name: str = ""
    g: Optional[TysG] = None


class TysP(SQLerSafeModel):
    __tablename__ = "rep_tys_p"
    title: str = ""
    one: Optional[TysC] = None
    many: list[TysC] = PydField(default_factory=list)
    bag: dict[str, TysC] = PydField(default_factory=dict)


class TysN(SQLerSafeModel):
    __tablename__ = "rep_tys_n"
    name: str = ""
    one: Optional["TysN"] = None


@dataclass
class LtyG(SQLerLiteModel):
    __tablename__ = "rep_lty_g"
    name: str = ""


@dataclass
class LtyC(SQLerLiteModel):
    __tablename__ = "rep_lty_c"
    name: str = ""
    g: Optional[LtyG] = None


@dataclass
class LtyP(SQLerLiteModel):
    __tablename__ = "rep_lty_p"
    title: str = ""
    one: Optional[LtyC] = None
    many: list[LtyC] = field(default_factory=list)
    bag: dict[str, LtyC] = field(default_factory=dict)


@dataclass
class LtyN(SQLerLiteModel):
    __tablename__ = "rep_lty_n"
    name: str = ""
    one: Optional["LtyN"] = None


@dataclass
class LtysG(SQLerLiteSafeModel):
    __tablename__ = "rep_ltys_g"
    name: str = ""


@dataclass
class LtysC(SQLerLiteSafeModel):
    __tablename__ = "rep_ltys_c"
    name: str = ""
    g: Optional[LtysG] = None


@dataclass
class LtysP(SQLerLiteSafeModel):
    __tablename__ = "rep_ltys_p"
    title: str = ""
    one: Optional[LtysC] = None
    many: list[LtysC] = field(default_factory=list)
    bag: dict[str, LtysC] = field(default_factory=dict)


@dataclass
class LtysN(SQLerLiteSafeModel):
    __tablename__ = "rep_ltys_n"
    name: str = ""
    one: Optional["LtysN"] = None


TYPED = {
    "pydantic": (TyG, TyC, TyP, TyN),
    "safe": (TysG, TysC, TysP, TysN),
    "lite": (LtyG, LtyC, LtyP, LtyN),
    "lite-safe": (LtysG, LtysC, LtysP, LtysN),
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


def load(cls: type, rid: int, way: str):
    if way == "refresh":
        return cls.query().filter(F("_id") == rid).resolve(False).first().refresh()
    return {
        "from_id": lambda: cls.from_id(rid),
        "from_ids": lambda: cls.from_ids([rid])[0],
        "all": lambda: cls.query().filter(F("_id") == rid).all()[0],
        "first": lambda: cls.query().filter(F("_id") == rid).first(),
    }[way]()


def rows_of(db: Any, table: str) -> list:
    cur = db.adapter.execute(f"SELECT _id, data FROM {table} ORDER BY _id")
    return [(r[0], json.loads(r[1])) for r in cur.fetchall()]


def version_of(db: SQLerDB, table: str, rid: int) -> int:
    return db.adapter.execute(f"SELECT _version FROM {table} WHERE _id = ?", [rid]).fetchone()[0]


def sent_to(sent: list, table: str) -> int:
    return len([q for q in sent if f"FROM {table} " in q.sql or f"FROM {table};" in q.sql])


@pytest.fixture
def typed(make_db):
    """g (saved twice), c1 -> g (saved twice), c2 (once); p holds c1, [c1, c2, c1], {a: c2}."""

    def make(base: str):
        g_cls, c_cls, p_cls, n_cls = TYPED[base]
        db = make_db()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            for cls in (g_cls, c_cls, p_cls, n_cls):
                cls.set_db(db)
        g = g_cls(name="g").save()
        g.name = "g2"
        g.save()
        c1 = c_cls(name="c1", g=g).save()
        c1.name = "c1b"
        c1.save()
        c2 = c_cls(name="c2").save()
        p = p_cls(title="p", one=c1, many=[c1, c2, c1], bag={"a": c2}).save()
        return db, (g_cls, c_cls, p_cls, n_cls), p._id

    return make


def _want_typed(base: str) -> dict:
    v1, v0 = (1, 0) if base in VERSIONED else ("-", "-")
    g_cls, c_cls, _, _ = TYPED[base]
    g = (g_cls.__name__, 1, v1, {"name": "g2"})
    c1 = (c_cls.__name__, 1, v1, {"name": "c1b", "g": g})
    c2 = (c_cls.__name__, 2, v0, {"name": "c2", "g": None})
    return {"title": "p", "one": c1, "many": [c1, c2, c1], "bag": {"a": c2}}


@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(TYPED))
def test_a_typed_reference_holds_its_row_and_the_parent_saves_unchanged(typed, base, way):
    """Item 1 and 2 (``p2_ref_save.py``, ``p7_lite_list_save.py``): every child, grandchild,
    list entry and dict value is a model of its row; saving the parent unchanged keeps
    its stored references (on the base the batch ways raised "Related model must be
    saved before saving parent", and the lite ones wrote the list as embedded copies)."""
    db, (g_cls, c_cls, p_cls, _), pid = typed(base)
    pt, ct, gt = p_cls.__tablename__, c_cls.__tablename__, g_cls.__tablename__
    stored, children, grand = rows_of(db, pt), rows_of(db, ct), rows_of(db, gt)
    got = load(p_cls, pid, way)
    assert pic(got)[3] == _want_typed(base)
    got.save()  # must NOT raise "Related model must be saved before saving parent"
    assert rows_of(db, pt)[0][1]["one"] == {"_table": ct, "_id": 1}
    assert rows_of(db, pt)[0][1]["many"] == [
        {"_table": ct, "_id": 1}, {"_table": ct, "_id": 2}, {"_table": ct, "_id": 1}
    ]
    assert rows_of(db, pt)[0][1]["bag"] == {"a": {"_table": ct, "_id": 2}}
    assert [r[1] for r in rows_of(db, pt)] == [
        {**stored[0][1], "_version": 1} if base in VERSIONED else stored[0][1]
    ]
    assert rows_of(db, ct) == children
    assert len(children) == 2
    assert rows_of(db, gt) == grand
    assert len(grand) == 1


@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(TYPED))
def test_a_loaded_child_saves_its_own_row(typed, base, way):
    """``p6_child_version.py``: a child loaded through its parent saves its own row (a safe
    child with the row's version, so the save is not refused as stale)."""
    db, (_, c_cls, p_cls, _), pid = typed(base)
    ct = c_cls.__tablename__
    got = load(p_cls, pid, way)
    got.many[1].name = "c2x"
    got.many[1].save()
    assert rows_of(db, ct)[1][0] == 2
    assert rows_of(db, ct)[1][1]["name"] == "c2x"
    assert len(rows_of(db, ct)) == 2
    if base in VERSIONED:
        assert version_of(db, ct, 2) == 1
        assert got.many[1]._version == 1


@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(TYPED))
def test_a_list_holding_one_child_twice_reads_the_child_table_once(typed, base, way):
    db, (_, c_cls, p_cls, _), pid = typed(base)
    with query_logger.capture() as sent:
        got = load(p_cls, pid, way)
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


# SELECTs of the cycle's table to load n1 (n1 -> n2 -> n1) and n3 (n3 -> n3), every way in,
# as the base sends them (refresh includes the first() that loads the row to refresh)
CYCLE_SELECTS = {"from_id": 3, "refresh": 4, "from_ids": 3, "all": 3, "first": 3}
SELF_SELECTS = {"from_id": 2, "refresh": 3, "from_ids": 2, "all": 2, "first": 2}
# where every way cuts the cycle: at the second time a row is reached below the row
# loaded; the reference cut there is left to the class, which builds an empty model of
# it (``_id`` None) on the pydantic bases and keeps the reference on the lite ones
_REFUSE = (ValueError, "Related model must be saved before saving parent")
_STALE = (StaleVersionError, None)
# per base: (where the n1 cycle is cut, where the n3 self-reference is cut, what saving the
# level below each cut does). The cut reference is left to the class, which builds an
# empty model of it (``_id`` None) on the pydantic bases, whose save() refuses, as
# SQLerModel.from_id's answer always did; the lite bases keep the reference, and the lite
# safe one's level below was read at the version a.save() / b.save() moved past.
CYCLE = {
    "pydantic": ((None, ""), (None, ""), _REFUSE),
    "safe": ((None, ""), (None, ""), _REFUSE),
    "lite": (("ref", 2), ("ref", 3), None),
    "lite-safe": (("ref", 2), ("ref", 3), _STALE),
}


@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(TYPED))
def test_a_cycle_through_typed_fields_ends_and_saves(typed, base, way):
    db, (_, _, _, n_cls), _ = typed(base)
    cut_a, cut_b, below_cut = CYCLE[base]
    nt = n_cls.__tablename__
    n1 = db.insert_document(nt, {"name": "n1", "one": None})
    n2 = db.insert_document(nt, {"name": "n2", "one": {"_table": nt, "_id": n1}})
    db.upsert_document(nt, n1, {"name": "n1", "one": {"_table": nt, "_id": n2}})
    n3 = db.insert_document(nt, {"name": "n3", "one": None})
    db.upsert_document(nt, n3, {"name": "n3", "one": {"_table": nt, "_id": n3}})
    before = rows_of(db, nt)
    with query_logger.capture() as sent:
        a = load(n_cls, n1, way)
    assert sent_to(sent, nt) == CYCLE_SELECTS[way]
    with query_logger.capture() as sent:
        b = load(n_cls, n3, way)
    assert sent_to(sent, nt) == SELF_SELECTS[way]
    assert chain(a, 5) == [(1, "n1"), (2, "n2"), (1, "n1"), cut_a]
    assert chain(b, 5) == [(3, "n3"), (3, "n3"), cut_b]
    a.save()
    a.one.save()
    b.save()
    for level in (a.one.one, b.one):
        if below_cut is None:
            level.save()
        else:
            with pytest.raises(below_cut[0], match=below_cut[1]):
                level.save()
    after = rows_of(db, nt)
    assert [r[1]["one"] for r in after] == [r[1]["one"] for r in before]
    assert [r[1]["one"] for r in after] == [
        {"_table": nt, "_id": 2}, {"_table": nt, "_id": 1}, {"_table": nt, "_id": 3}
    ]


def _try_save(db: Any, inst: Any, table: str) -> Any:
    """What ``save()`` did: the error it raised, or the parent's stored fields after it."""
    try:
        inst.save()
    except Exception as exc:  # the outcome is pinned, whatever it is
        return (type(exc).__name__, str(exc).splitlines()[0])
    data = rows_of(db, table)[-1][1]
    data.pop("_version", None)
    return data


def _observe(db: Any, cls: type, rid: int, way: str, table: str) -> Any:
    try:
        got = load(cls, rid, way)
    except Exception as exc:
        return ("load raised", type(exc).__name__)
    return pic(got)[3], _try_save(db, got, table)


@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(TYPED))
def test_a_typed_field_whose_row_is_gone(typed, base, way):
    """A reference to a row that is gone gives one answer on every way in
    (``gone_expected``)."""
    db, (_, c_cls, p_cls, _), _ = typed(base)
    pt = p_cls.__tablename__
    p2 = p_cls(title="p2", one=c_cls.from_id(2), many=[c_cls.from_id(1), c_cls.from_id(2)]).save()
    db.delete_document(c_cls.__tablename__, 2)
    assert _observe(db, p_cls, p2._id, way, pt) == gone_expected(base)


@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(TYPED))
@pytest.mark.parametrize("shape", ["id-str", "other-table"])
def test_a_typed_field_holding_a_reference_that_cannot_be_followed(typed, base, way, shape):
    db, (g_cls, c_cls, p_cls, _), _ = typed(base)
    pt = p_cls.__tablename__
    held = (
        {"_table": c_cls.__tablename__, "_id": "x"}
        if shape == "id-str"
        else {"_table": g_cls.__tablename__, "_id": 1}
    )
    rid = db.insert_document(pt, {"title": "h", "one": held})
    assert _observe(db, p_cls, rid, way, pt) == UNFOLLOWED_EXPECTED


@pytest.mark.parametrize("base", sorted(TYPED))
def test_a_thousand_parents_sharing_one_child_read_it_once(typed, base):
    db, (_, c_cls, p_cls, _), _ = typed(base)
    pt, ct = p_cls.__tablename__, c_cls.__tablename__
    for i in range(1000):
        db.insert_document(pt, {"title": f"q{i}", "one": {"_table": ct, "_id": 2}})
    with query_logger.capture() as sent:
        got = p_cls.query().filter(F("title") != "p").all()
    assert sent_to(sent, ct) == 1
    assert len(got) == 1000
    assert {(type(g.one).__name__, g.one._id, g.one.name) for g in got} == {
        (c_cls.__name__, 2, "c2")
    }
    assert got[0].one is not got[1].one  # each parent its own model, as on the base


@pytest.mark.parametrize("base", ["pydantic", "safe"])
@pytest.mark.parametrize("way", ["resolve-false", "iter"])
def test_a_load_without_resolution_is_left_as_it_was(typed, base, way):
    db, (_, _, p_cls, _), pid = typed(base)
    if way == "iter":
        got = next(p_cls.query().filter(F("_id") == pid).iter())
    else:
        got = p_cls.query().filter(F("_id") == pid).resolve(False).first()
    kind = "TysC" if base == "safe" else "TyC"
    v = 0 if base == "safe" else "-"
    empty = (kind, None, v, {"name": "", "g": None})
    assert pic(got)[3] == {"title": "p", "one": empty, "many": [empty] * 3, "bag": {"a": empty}}
    with pytest.raises(ValueError, match="Related model must be saved before saving parent"):
        got.save()


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


class VdC(SQLerModel):
    __tablename__ = "rep_vd_c"
    name: str = ""


class VdP(SQLerModel):
    __tablename__ = "rep_vd_p"
    title: str = ""
    many: list[VdC] = PydField(default_factory=list)
    check_many = field_validator("many")(_reshape)


class VdsC(SQLerSafeModel):
    __tablename__ = "rep_vds_c"
    name: str = ""


class VdsP(SQLerSafeModel):
    __tablename__ = "rep_vds_p"
    title: str = ""
    many: list[VdsC] = PydField(default_factory=list)
    check_many = field_validator("many")(_reshape)


VALIDATED = {"pydantic": (VdC, VdP), "safe": (VdsC, VdsP)}
RESHAPED = {"drop": [2, 3], "reverse": [3, 2, 1], "new": [None, None, None]}


@pytest.mark.parametrize("mode", sorted(RESHAPED))
@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(VALIDATED))
def test_a_validator_that_reshapes_the_children_keeps_each_child_its_row(make_db, base, way, mode):
    """Item 4: each child the validator leaves is the row it says it is; nothing is
    paired by position, so ``save()`` overwrites no other row."""
    c_cls, p_cls = VALIDATED[base]
    db = make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        c_cls.set_db(db)
        p_cls.set_db(db)
    ct, pt = c_cls.__tablename__, p_cls.__tablename__
    ids = [db.insert_document(ct, {"name": f"c{i}"}) for i in (1, 2, 3)]
    pid = db.insert_document(pt, {"title": mode, "many": [{"_table": ct, "_id": i} for i in ids]})
    children, stored = rows_of(db, ct), rows_of(db, pt)
    got = load(p_cls, pid, way)
    assert [c._id for c in got.many] == RESHAPED[mode]
    assert [c.name for c in got.many] == [
        f"c{i}" for i in (RESHAPED[mode] if mode != "new" else (1, 2, 3))
    ]
    if mode == "new":
        with pytest.raises(ValueError, match="Related model must be saved before saving parent"):
            got.save()
        assert rows_of(db, pt) == stored
    else:
        got.save()
        assert rows_of(db, pt)[0][1]["many"] == [{"_table": ct, "_id": i} for i in RESHAPED[mode]]
    assert rows_of(db, ct) == children
    assert len(children) == 3


# ---- the version is the column's ------------------------------------------------------


@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", VERSIONED)
def test_a_child_holds_the_version_of_its_column_not_of_its_json(typed, base, way):
    """A queryset ``update_one()`` bumps the ``_version`` column only: the JSON keeps the
    version the last ``save()`` wrote. The child holds the column's, as the row loaded on
    its own does (and, like it, cannot be saved: the versioned update checks both)."""
    db, (_, c_cls, p_cls, _), pid = typed(base)
    ct = c_cls.__tablename__
    c_cls.query().filter(F("_id") == 2).update_one(name="u")
    c_cls.query().filter(F("_id") == 2).update_one(name="c2")
    assert version_of(db, ct, 2) == 2
    assert rows_of(db, ct)[1][1]["_version"] == 0
    got = load(p_cls, pid, way)
    alone = c_cls.from_id(2)
    assert (got.bag["a"]._version, alone._version) == (2, 2)
    for inst in (got.bag["a"], alone):
        with pytest.raises(StaleVersionError):
            inst.save()
    assert version_of(db, ct, 2) == 2


# ---- a child class bound to another database, and one never bound ---------------------


class TyoC(SQLerModel):
    __tablename__ = "rep_tyo_c"
    name: str = ""


class TyuC(SQLerModel):
    __tablename__ = "rep_tyu_c"
    name: str = ""


class TyoP(SQLerModel):
    __tablename__ = "rep_tyo_p"
    title: str = ""
    other: Optional[TyoC] = None
    never: Optional[TyuC] = None


class TysoC(SQLerSafeModel):
    __tablename__ = "rep_tyso_c"
    name: str = ""


class TysuC(SQLerSafeModel):
    __tablename__ = "rep_tysu_c"
    name: str = ""


class TysoP(SQLerSafeModel):
    __tablename__ = "rep_tyso_p"
    title: str = ""
    other: Optional[TysoC] = None
    never: Optional[TysuC] = None


@dataclass
class LtyoC(SQLerLiteModel):
    __tablename__ = "rep_ltyo_c"
    name: str = ""


@dataclass
class LtyuC(SQLerLiteModel):
    __tablename__ = "rep_ltyu_c"
    name: str = ""


@dataclass
class LtyoP(SQLerLiteModel):
    __tablename__ = "rep_ltyo_p"
    title: str = ""
    other: Optional[LtyoC] = None
    never: Optional[LtyuC] = None


ELSEWHERE = {
    "pydantic": (TyoC, TyuC, TyoP),
    "safe": (TysoC, TysuC, TysoP),
    "lite": (LtyoC, LtyuC, LtyoP),
}


def _elsewhere(make_db, base: str):
    o_cls, u_cls, p_cls = ELSEWHERE[base]
    db, other_db = make_db("parent.db"), make_db("child.db")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        p_cls.set_db(db)
        o_cls.set_db(other_db)
    ot, ut = o_cls.__tablename__, u_cls.__tablename__
    # the same id in both databases, with different names, shows which one was read
    db.insert_document(ot, {"name": "in-parent-db"})
    other_db.insert_document(ot, {"name": "in-child-db"})
    db.insert_document(ut, {"name": "never-bound"})
    rid = db.insert_document(
        p_cls.__tablename__,
        {"title": "e", "other": {"_table": ot, "_id": 1}, "never": {"_table": ut, "_id": 1}},
    )
    return db, other_db, rid


@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(ELSEWHERE))
def test_a_child_class_bound_elsewhere_or_never_bound(make_db, base, way):
    """A child whose class is bound to another database than the parent's, and one whose
    class is never bound. On the pydantic bases the batch builds no child of a class bound
    elsewhere: it stays what the base gave (``_id`` None). On the lite bases the batch
    gives that child the ``_id`` of the row in the parent's database, and its ``save()``
    writes the row of that number in its own class's database, as the released version
    does. A class never bound reads the parent's database, so its child is built with its
    row."""
    o_cls, _, p_cls = ELSEWHERE[base]
    db, other_db, rid = _elsewhere(make_db, base)
    assert _observe(db, p_cls, rid, way, p_cls.__tablename__) == ELSEWHERE_EXPECTED[(base, way)]


@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", ["pydantic", "safe"])
def test_a_child_bound_elsewhere_saves_and_deletes_in_its_own_database(make_db, base, way):
    """The child of a class bound to another database, loaded through the parent: what its
    ``save()`` and ``delete()`` do, and the rows of both databases after them."""
    o_cls, _, p_cls = ELSEWHERE[base]
    db, other_db, rid = _elsewhere(make_db, base)
    ot = o_cls.__tablename__
    child = load(p_cls, rid, way).other
    if way in ("from_id", "refresh"):
        # the per-row ways read the child from its own class's database: its row there
        assert (child._id, child.name) == (1, "in-child-db")
        child.name = "saved"
        child.save()
        assert [r[1]["name"] for r in rows_of(other_db, ot)] == ["saved"]
        child.delete()
        assert rows_of(other_db, ot) == []
    else:
        # the batch read the parent's database: the child is not given that row's id
        assert (child._id, child.name) == (None, "in-parent-db")
        with pytest.raises(ValueError, match="Cannot delete unsaved model"):
            child.delete()
        child.save()  # a new row in its own database
        assert [r[1]["name"] for r in rows_of(other_db, ot)] == ["in-child-db", "in-parent-db"]
    assert [r[1]["name"] for r in rows_of(db, ot)] == ["in-parent-db"]


@pytest.mark.parametrize("way", ["from_ids", "all", "first"])
@pytest.mark.parametrize("base", sorted(ELSEWHERE))
def test_a_child_of_a_class_never_bound_read_through_using_saves_its_row(make_db, base, way):
    _, u_cls, p_cls = ELSEWHERE[base]
    db, _, rid = _elsewhere(make_db, base)
    ut = u_cls.__tablename__
    got = {
        "from_ids": lambda: p_cls.from_ids([rid], db=db)[0],
        "all": lambda: p_cls.using(db).filter(F("_id") == rid).all()[0],
        "first": lambda: p_cls.using(db).filter(F("_id") == rid).first(),
    }[way]()
    assert (got.never._id, got.never.name) == (1, "never-bound")
    got.never.name = "renamed"
    got.never.save(db=db)
    # a safe class writes its version into the JSON too
    want = {"name": "renamed", "_version": 1} if base == "safe" else {"name": "renamed"}
    assert rows_of(db, ut) == [(1, want)]


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


def test_typed_fields_of_a_class_defined_before_the_class_it_refers_to(make_db):
    """Item 5: asked while the child class does not exist, the answer is not kept; asked
    after, it names the child, and a load builds the child from its row."""

    class FwdP(SQLerModel):
        __tablename__ = "rep_fwd_p"
        title: str = ""
        one: Optional["FwdC"] = None

    @dataclass
    class LFwdP(SQLerLiteModel):
        __tablename__ = "rep_lfwd_p"
        title: str = ""
        one: Optional["LFwdC"] = None

    assert _refs.typed_fields(FwdP) == {}
    assert _refs.typed_fields(LFwdP) == {}

    class FwdC(SQLerModel):
        __tablename__ = "rep_fwd_c"
        name: str = ""

    @dataclass
    class LFwdC(SQLerLiteModel):
        __tablename__ = "rep_lfwd_c"
        name: str = ""

    FwdP.model_rebuild()
    assert _refs.typed_fields(FwdP) == {"one": _at(FwdC)}
    assert _refs.typed_fields(LFwdP) == {"one": _at(LFwdC)}
    db = make_db()
    for c_cls, p_cls in ((FwdC, FwdP), (LFwdC, LFwdP)):
        cid = db.insert_document(c_cls.__tablename__, {"name": "c"})
        db.insert_document(p_cls.__tablename__, {"title": "p", "one": {"_table": c_cls.__tablename__, "_id": cid}})
        got = p_cls.using(db).first()
        assert (type(got.one).__name__, got.one._id, got.one.name) == (c_cls.__name__, cid, "c")


def test_typed_fields_names_every_model_class_a_field_admits():
    assert _refs.typed_fields(TyP) == {"one": _at(TyC), "many": _in_list(TyC), "bag": _in_dict(TyC)}
    assert _refs.typed_fields(LtyP) == {"one": _at(LtyC), "many": _in_list(LtyC), "bag": _in_dict(LtyC)}
    assert _refs.typed_fields(Pd_Parent) == {"one": _at(Pd_Child), "many": _in_list(Pd_Child)}
    assert _refs.typed_fields(TyG) == {}


# ---- a child document its class refuses ----------------------------------------------


class RqC(SQLerModel):
    __tablename__ = "rep_rq_c"
    name: str


class RqP(SQLerModel):
    __tablename__ = "rep_rq_p"
    one: Optional[RqC] = None


@dataclass
class LRqC(SQLerLiteModel):
    __tablename__ = "rep_lrq_c"
    name: str


@dataclass
class LRqP(SQLerLiteModel):
    __tablename__ = "rep_lrq_p"
    one: Optional[LRqC] = None


REFUSED = {"pydantic": (RqC, RqP), "lite": (LRqC, LRqP)}
REFUSED_EXPECTED = {
    ("pydantic", "from_id"): "ValidationError",
    ("pydantic", "refresh"): "ValidationError",
    ("pydantic", "from_ids"): "ValidationError",
    ("pydantic", "all"): "ValidationError",
    ("pydantic", "first"): "ValidationError",
    ("lite", "from_id"): "TypeError",
    ("lite", "refresh"): "TypeError",
    ("lite", "from_ids"): "TypeError",
    ("lite", "all"): "TypeError",
    ("lite", "first"): "TypeError",
}


@pytest.mark.parametrize("way", TYPED_WAYS)
@pytest.mark.parametrize("base", sorted(REFUSED))
def test_a_child_document_its_class_refuses_fails_the_load_as_on_the_base(make_db, base, way):
    c_cls, p_cls = REFUSED[base]
    db = make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        c_cls.set_db(db)
        p_cls.set_db(db)
    ct = c_cls.__tablename__
    cid = db.insert_document(ct, {"other": 1})
    pid = db.insert_document(p_cls.__tablename__, {"one": {"_table": ct, "_id": cid}})
    try:
        load(p_cls, pid, way)
        outcome = "loaded"
    except Exception as exc:  # the outcome is pinned, whatever it is
        outcome = type(exc).__name__
    assert outcome == REFUSED_EXPECTED[(base, way)]


# ---- a queryset without a database, and PooledSQLerDB -------------------------------


def test_a_queryset_built_by_hand_without_a_database_builds_no_child(typed):
    """Without a database the batch does not know which database a child's row is in: it
    builds no child, as on the base."""
    db, (_, _, p_cls, _), pid = typed("pydantic")
    qs = SQLerQuerySet(p_cls, SQLerQuery(table=p_cls.__tablename__, adapter=db.adapter))
    got = qs.first()
    assert (type(got.one).__name__, got.one._id, got.one.name) == ("TyC", None, "c1b")


def test_a_class_bound_to_a_pooled_database_gets_children_with_their_rows(tmp_path):
    """On the base the child had ``_id`` None; now it is its row, read through the pool's
    adapter (``_id`` and ``data`` only)."""
    pdb = PooledSQLerDB.on_disk(str(tmp_path / "pool.db"), max_connections=2)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            for cls in (TyG, TyC, TyP):
                cls.set_db(pdb)
        cid = pdb.insert_document(TyC.__tablename__, {"name": "c", "g": None})
        pdb.insert_document(TyP.__tablename__, {"title": "p", "one": {"_table": TyC.__tablename__, "_id": cid}})
        got = TyP.query().first()
        assert (got.one._id, got.one.name) == (cid, "c")
        got.save()
        assert rows_of(pdb, TyP.__tablename__)[0][1]["one"] == {"_table": TyC.__tablename__, "_id": cid}
    finally:
        pdb.close()


# ---- classes never bound, read through using(db) ------------------------------------


class UbC(SQLerSafeModel):
    __tablename__ = "rep_ub_c"
    name: str = ""


class UbP(SQLerSafeModel):
    __tablename__ = "rep_ub_p"
    name: str = ""
    one: Optional[UbC] = None
    many: list[UbC] = PydField(default_factory=list)


@dataclass
class LUbC(SQLerLiteSafeModel):
    __tablename__ = "rep_lub_c"
    name: str = ""


@dataclass
class LUbP(SQLerLiteSafeModel):
    __tablename__ = "rep_lub_p"
    name: str = ""
    one: Optional[LUbC] = None
    many: list[LUbC] = field(default_factory=list)


UNBOUND = {"safe": (UbC, UbP), "lite-safe": (LUbC, LUbP)}


@pytest.mark.parametrize("way", ["from_ids", "all", "first"])
@pytest.mark.parametrize("base", sorted(UNBOUND))
def test_a_child_of_a_class_never_bound_holds_its_column_version(make_db, base, way):
    """``p6_child_version.py``: no class bound, everything through ``db=`` / ``using(db)``;
    the child's row is at version 2, and the child loaded through the parent saves."""
    c_cls, p_cls = UNBOUND[base]
    db = make_db()
    child = c_cls(name="c")
    for name in ("c", "c1", "c2"):
        child.name = name
        child.save(db=db)
    p_cls(name="p", one=child, many=[child]).save(db=db)
    ct, pt = c_cls.__tablename__, p_cls.__tablename__
    stored = rows_of(db, pt)
    got = {
        "from_ids": lambda: p_cls.from_ids([1], db=db)[0],
        "all": lambda: p_cls.using(db).all()[0],
        "first": lambda: p_cls.using(db).first(),
    }[way]()
    assert (got.one._id, got.one._version, got.many[0]._id, got.many[0]._version) == (1, 2, 1, 2)
    got.one.name = "again"
    got.one.save(db=db)  # on the base: StaleVersionError (the child held version 0)
    assert version_of(db, ct, 1) == 3
    assert rows_of(db, ct)[0][1]["name"] == "again"
    got.save(db=db)
    assert [r[1]["one"] for r in rows_of(db, pt)] == [r[1]["one"] for r in stored]
    assert [r[1]["many"] for r in rows_of(db, pt)] == [[{"_table": ct, "_id": 1}]]


# ---- msgspec models, pinned as they are ------------------------------------------------


class MsC(SQLerMsgspecModel):
    __tablename__ = "rep_ms_c"
    name: str = ""


class MsP(SQLerMsgspecModel):
    __tablename__ = "rep_ms_p"
    title: str = ""
    one: Optional[MsC] = None
    many: list[MsC] = []


@pytest.mark.parametrize("way", ["from_id", "from_ids", "all", "first"])
def test_a_msgspec_typed_field_gives_what_it_gave(make_db, way):
    """A msgspec class resolves nothing per row (``from_id`` / ``from_ids`` give empty
    children holding the row's ``_id``); ``all()`` / ``first()`` go through the queryset's
    batch, which follows its typed fields and gives the stored child. Saving writes the
    references back unchanged."""
    db = make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        MsC.set_db(db)
        MsP.set_db(db)
    cid = db.insert_document(MsC.__tablename__, {"name": "c"})
    ref = {"_table": MsC.__tablename__, "_id": cid}
    pid = db.insert_document(MsP.__tablename__, {"title": "p", "one": ref, "many": [ref]})
    stored = rows_of(db, MsP.__tablename__)
    got = {
        "from_id": lambda: MsP.from_id(pid),
        "from_ids": lambda: MsP.from_ids([pid])[0],
        "all": lambda: MsP.query().all()[0],
        "first": lambda: MsP.query().first(),
    }[way]()
    name = "" if way in ("from_id", "from_ids") else "c"
    assert [(type(c).__name__, c._id, c.name) for c in (got.one, *got.many)] == [
        ("MsC", cid, name), ("MsC", cid, name)
    ]
    got.save()
    assert rows_of(db, MsP.__tablename__) == stored


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
ELSEWHERE_EXPECTED: dict = {('lite', 'all'): ({'never': ('LtyuC', 1, '-', {'name': 'never-bound'}),
                    'other': ('LtyoC', 1, '-', {'name': 'in-parent-db'}),
                    'title': 'e'},
                   {'never': {'_id': 1, '_table': 'rep_ltyu_c'},
                    'other': {'_id': 1, '_table': 'rep_ltyo_c'},
                    'title': 'e'}),
 ('lite', 'first'): ({'never': ('LtyuC', 1, '-', {'name': 'never-bound'}),
                      'other': ('LtyoC', 1, '-', {'name': 'in-parent-db'}),
                      'title': 'e'},
                     {'never': {'_id': 1, '_table': 'rep_ltyu_c'},
                      'other': {'_id': 1, '_table': 'rep_ltyo_c'},
                      'title': 'e'}),
 ('lite', 'from_id'): ({'never': ('LtyuC', 1, '-', {'name': 'never-bound'}),
                        'other': ('LtyoC', 1, '-', {'name': 'in-child-db'}),
                        'title': 'e'},
                       {'never': {'_id': 1, '_table': 'rep_ltyu_c'},
                        'other': {'_id': 1, '_table': 'rep_ltyo_c'},
                        'title': 'e'}),
 ('lite', 'from_ids'): ({'never': ('LtyuC', 1, '-', {'name': 'never-bound'}),
                         'other': ('LtyoC', 1, '-', {'name': 'in-parent-db'}),
                         'title': 'e'},
                        {'never': {'_id': 1, '_table': 'rep_ltyu_c'},
                         'other': {'_id': 1, '_table': 'rep_ltyo_c'},
                         'title': 'e'}),
 ('lite', 'refresh'): ({'never': ('LtyuC', 1, '-', {'name': 'never-bound'}),
                        'other': ('LtyoC', 1, '-', {'name': 'in-child-db'}),
                        'title': 'e'},
                       {'never': {'_id': 1, '_table': 'rep_ltyu_c'},
                        'other': {'_id': 1, '_table': 'rep_ltyo_c'},
                        'title': 'e'}),
 ('pydantic', 'all'): ({'never': ('TyuC', 1, '-', {'name': 'never-bound'}),
                        'other': ('TyoC', None, '-', {'name': 'in-parent-db'}),
                        'title': 'e'},
                       ('ValueError', 'Related model must be saved before saving parent')),
 ('pydantic', 'first'): ({'never': ('TyuC', 1, '-', {'name': 'never-bound'}),
                          'other': ('TyoC', None, '-', {'name': 'in-parent-db'}),
                          'title': 'e'},
                         ('ValueError', 'Related model must be saved before saving parent')),
 ('pydantic', 'from_id'): ({'never': ('TyuC', 1, '-', {'name': 'never-bound'}),
                            'other': ('TyoC', 1, '-', {'name': 'in-child-db'}),
                            'title': 'e'},
                           {'never': {'_id': 1, '_table': 'rep_tyu_c'},
                            'other': {'_id': 1, '_table': 'rep_tyo_c'},
                            'title': 'e'}),
 ('pydantic', 'from_ids'): ({'never': ('TyuC', 1, '-', {'name': 'never-bound'}),
                             'other': ('TyoC', None, '-', {'name': 'in-parent-db'}),
                             'title': 'e'},
                            ('ValueError', 'Related model must be saved before saving parent')),
 ('pydantic', 'refresh'): ({'never': ('TyuC', 1, '-', {'name': 'never-bound'}),
                            'other': ('TyoC', 1, '-', {'name': 'in-child-db'}),
                            'title': 'e'},
                           {'never': {'_id': 1, '_table': 'rep_tyu_c'},
                            'other': {'_id': 1, '_table': 'rep_tyo_c'},
                            'title': 'e'}),
 ('safe', 'all'): ({'never': ('TysuC', 1, 0, {'name': 'never-bound'}),
                    'other': ('TysoC', None, 0, {'name': 'in-parent-db'}),
                    'title': 'e'},
                   ('ValueError', 'Related model must be saved before saving parent')),
 ('safe', 'first'): ({'never': ('TysuC', 1, 0, {'name': 'never-bound'}),
                      'other': ('TysoC', None, 0, {'name': 'in-parent-db'}),
                      'title': 'e'},
                     ('ValueError', 'Related model must be saved before saving parent')),
 ('safe', 'from_id'): ({'never': ('TysuC', 1, 0, {'name': 'never-bound'}),
                        'other': ('TysoC', 1, 0, {'name': 'in-child-db'}),
                        'title': 'e'},
                       {'never': {'_id': 1, '_table': 'rep_tysu_c'},
                        'other': {'_id': 1, '_table': 'rep_tyso_c'},
                        'title': 'e'}),
 ('safe', 'from_ids'): ({'never': ('TysuC', 1, 0, {'name': 'never-bound'}),
                         'other': ('TysoC', None, 0, {'name': 'in-parent-db'}),
                         'title': 'e'},
                        ('ValueError', 'Related model must be saved before saving parent')),
 ('safe', 'refresh'): ({'never': ('TysuC', 1, 0, {'name': 'never-bound'}),
                        'other': ('TysoC', 1, 0, {'name': 'in-child-db'}),
                        'title': 'e'},
                       {'never': {'_id': 1, '_table': 'rep_tysu_c'},
                        'other': {'_id': 1, '_table': 'rep_tyso_c'},
                        'title': 'e'})}


# ---- a batch read changes no schema -------------------------------------------------


class NcC(SQLerSafeModel):
    __tablename__ = "rep_nc_c"
    name: str = ""


class NcP(SQLerModel):
    __tablename__ = "rep_nc_p"
    name: str = ""
    one: Optional[NcC] = None
    loose: Any = None


def _columns(db: Any, table: str) -> list:
    return [r[1] for r in db.adapter.execute(f"PRAGMA table_info({table})").fetchall()]


def _plain_child_table(db: SQLerDB) -> None:
    """A child table written as plain documents (no ``_version`` column), referenced by a
    typed and an untyped field of one parent row."""
    db.insert_document(NcC.__tablename__, {"name": "c"})
    ref = {"_table": NcC.__tablename__, "_id": 1}
    db.insert_document(NcP.__tablename__, {"name": "p", "one": ref, "loose": ref})


BATCH = {
    "from_ids": lambda db: NcP.from_ids([1], db=db)[0],
    "all": lambda db: NcP.using(db).all()[0],
    "first": lambda db: NcP.using(db).first(),
}


@pytest.mark.parametrize("way", sorted(BATCH))
def test_a_batch_read_adds_no_version_column_and_the_child_saves(make_db, way):
    """``p14_read_adds_column.py``: the child's table keeps its columns; the child holds
    version 0, and its ``save()`` (a write) adds the column and stores version 1."""
    db = make_db()
    _plain_child_table(db)
    before = _columns(db, NcC.__tablename__)
    got = BATCH[way](db)
    assert _columns(db, NcC.__tablename__) == before == ["_id", "data"]
    assert (got.one._id, got.one._version, got.loose) == (1, 0, {"_table": NcC.__tablename__, "_id": 1})
    got.one.name = "c2"
    got.one.save(db=db)
    assert version_of(db, NcC.__tablename__, 1) == 1
    assert rows_of(db, NcC.__tablename__) == [(1, {"name": "c2", "_version": 1})]


@pytest.mark.parametrize("way", sorted(BATCH))
def test_a_batch_read_works_on_a_read_only_database(tmp_path, way):
    """Opened with SQLite's ``file:...?mode=ro`` URI: the batch reads, sends no write."""
    path = tmp_path / "ro.db"
    rw = SQLerDB.on_disk(str(path))
    _plain_child_table(rw)
    rw.close()
    ro = SQLerDB.on_disk(f"file:{path}?mode=ro")
    try:
        got = BATCH[way](ro)
        assert (got.one._id, got.one._version, got.one.name) == (1, 0, "c")
        assert _columns(ro, NcC.__tablename__) == ["_id", "data"]
    finally:
        ro.close()


def test_a_table_that_gets_its_version_column_between_two_reads(make_db):
    """A safe ``save()`` between two batch reads of one database object adds the column;
    the second read holds the row's version."""
    db = make_db()
    _plain_child_table(db)
    first = NcP.from_ids([1], db=db)[0]
    assert first.one._version == 0
    first.one.save(db=db)
    second = NcP.from_ids([1], db=db)[0]
    assert (second.one._id, second.one._version) == (1, 1)


@pytest.mark.parametrize("way", sorted(BATCH))
def test_a_batch_read_keeps_a_raw_statement_left_uncommitted(make_db, way):
    """``p16_read_keeps_uncommitted.py``: a row inserted raw outside a transaction and not
    committed is still there after the batch read of a version-less child table."""
    db = make_db()
    _plain_child_table(db)
    db.adapter.execute("CREATE TABLE rep_nc_raw (n INTEGER)")
    db.adapter.auto_commit()
    db.adapter.execute("INSERT INTO rep_nc_raw VALUES (1)")
    got = BATCH[way](db)
    assert (got.one._id, got.one._version, got.one.name) == (1, 0, "c")
    assert db.adapter.execute("SELECT count(*) FROM rep_nc_raw").fetchone()[0] == 1


def _pragmas(sent: list, table: str) -> int:
    return len([q for q in sent if q.sql == f'PRAGMA table_info("{table}");'])


def test_a_second_read_of_a_version_less_table_asks_the_table_once(make_db):
    """The second batch read by the same object sends one ``PRAGMA`` and one ``SELECT`` on
    the child table: nothing remembered, no statement that fails."""
    db = make_db()
    _plain_child_table(db)
    NcP.from_ids([1], db=db)
    with query_logger.capture() as sent:
        got = NcP.from_ids([1], db=db)[0]
    assert got.one._version == 0
    assert (_pragmas(sent, NcC.__tablename__), sent_to(sent, NcC.__tablename__)) == (1, 1)


def test_a_second_read_of_a_versioned_table_sends_no_pragma(make_db):
    db = make_db()
    NcC(name="c").save(db=db)
    db.insert_document(NcP.__tablename__, {"name": "p", "one": {"_table": NcC.__tablename__, "_id": 1}})
    NcP.from_ids([1], db=db)
    with query_logger.capture() as sent:
        got = NcP.from_ids([1], db=db)[0]
    assert (got.one._id, got.one._version) == (1, 0)
    assert (_pragmas(sent, NcC.__tablename__), sent_to(sent, NcC.__tablename__)) == (0, 1)


def test_a_read_in_a_rolled_back_transaction_remembers_nothing(make_db):
    """The child table gets its column inside a transaction that is rolled back; the table
    is then made again without it: the next read gives version 0."""
    db = make_db()
    _plain_child_table(db)
    with pytest.raises(RuntimeError, match="undo"):
        with db.transaction():
            db.adapter.execute(f'ALTER TABLE {NcC.__tablename__} ADD COLUMN _version INTEGER NOT NULL DEFAULT 3')
            assert NcP.from_ids([1], db=db)[0].one._version == 3
            raise RuntimeError("undo")
    db.adapter.execute(f"DROP TABLE {NcC.__tablename__}")
    db.adapter.auto_commit()
    db.insert_document(NcC.__tablename__, {"name": "c"})
    got = NcP.from_ids([1], db=db)[0]
    assert (got.one._id, got.one._version) == (1, 0)


# ---- typed fields read annotations, not words --------------------------------------


class LitC(SQLerModel):
    __tablename__ = "rep_lit_c"
    name: str = ""


class LitP(SQLerModel):
    __tablename__ = "rep_lit_p"
    state: typing.Literal["open", "closed"] = "open"
    kind: typing.Literal["LitC"] = "LitC"
    one: Optional[LitC] = None


@dataclass
class LLitC(SQLerLiteModel):
    __tablename__ = "rep_llit_c"
    name: str = ""


@dataclass
class LLitP(SQLerLiteModel):
    __tablename__ = "rep_llit_p"
    state: typing.Literal["open", "closed"] = "open"
    kind: typing.Literal["LLitC"] = "LLitC"
    one: Optional[LLitC] = None


@pytest.mark.parametrize(("p_cls", "c_cls"), [(LitP, LitC), (LLitP, LLitC)])
def test_a_literal_field_is_not_typed_and_the_answer_is_kept(p_cls, c_cls):
    """The values of a ``Literal`` are not names: ``closed`` is not looked for and
    ``Literal["LitC"]`` names no class. The answer is found once and kept: the second ask
    gives the same object."""
    _refs._TYPED.pop(p_cls, None)
    first = _refs.typed_fields(p_cls)
    assert first == {"one": _at(c_cls)}
    assert p_cls in _refs._TYPED
    assert _refs.typed_fields(p_cls) is first


class _ElsewhereSub(LLitP):
    """A lite subclass declared as if in another module: the inherited string annotations
    are read in the module of the class that wrote them."""


_ElsewhereSub.__module__ = "rep_no_such_module"
_ElsewhereSub = dataclass(_ElsewhereSub)


def test_a_lite_subclass_in_another_module_inherits_its_parents_answer():
    assert _refs.typed_fields(_ElsewhereSub) == {"one": _at(LLitC)}
    assert _ElsewhereSub in _refs._TYPED


# ---- what a parent's mode="before" validator receives -------------------------------

SEEN_BEFORE: list = []


class BvC(SQLerSafeModel):
    __tablename__ = "rep_bv_c"
    name: str = ""


class BvP(SQLerSafeModel):
    __tablename__ = "rep_bv_p"
    name: str = ""
    one: Optional[BvC] = None

    @field_validator("one", mode="before")
    @classmethod
    def _seen(cls, v: Any) -> Any:
        SEEN_BEFORE.append(type(v).__name__)
        return v


@pytest.mark.parametrize("way", TYPED_WAYS)
def test_a_before_validator_on_a_typed_field_receives_an_instance(make_db, way):
    """Pinned: through ``from_id`` / ``refresh`` and through the batch ways the validator is
    handed the child model (the batch ways handed it the stored dict before)."""
    db = make_db()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        BvC.set_db(db)
        BvP.set_db(db)
    BvP(name="p", one=BvC(name="c").save()).save()
    SEEN_BEFORE.clear()
    got = load(BvP, 1, way)
    assert SEEN_BEFORE[-1] == "BvC"
    assert (got.one._id, got.one.name) == (1, "c")
