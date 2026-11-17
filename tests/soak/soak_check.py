"""The checker of a soak: it reads the database and the journals and names every row that is
not what the journals say was written.

It reads the file with plain ``sqlite3`` (no sqler call stands between the checker and the
rows), so a fault of sqler's own reads cannot hide a fault of its writes. ``check()`` returns
a :class:`Report`: how many rows each invariant checked, and each violation with its
invariant, table and row id.

The invariants (the names the report uses):

- ``row_missing``: a row a journal says was written is not there.
- ``row_doc``: a row holds another document than the last write the journal has for it
  (the keys kept in ``data``; promoted columns are ``promoted_column``).
- ``row_deleted_present``: a row a journal says was deleted is there.
- ``row_unknown``: a row no journal knows.
- ``version``: a safe row's ``_version`` is not the number of writes after its insert.
- ``counter``: a shared counter is not the sum of the increments the journals have, or its
  ``_version`` is not their number (a lost update).
- ``audit``: an audited write without exactly one audit row, or an audit row without its
  write.
- ``reference``: a reference to a row that is not there (no delete policy leaves one).
- ``promoted_column``: a promoted column does not hold the value written, or a promotion a
  journal made is not in the table.
- ``promoted_in_data``: a promoted field is still in ``data``.
- ``index``: the index over two fields of the promoted table is gone.
- ``search_missing``: the search index does not find a row by a word only it holds, or
  holds another text for it.
- ``search_deleted``: the search index still finds a row that was deleted (or no row).
- ``search_integrity``: the index's own ``integrity-check`` fails.
- ``rollback_left``: a write that rolled back left a row, an audit row or an index entry.
- ``archive``: an archived row is in both tables or in neither, or holds another document.
- ``integrity_check`` and ``foreign_key_check``: SQLite's own checks are not clean;
  ``health`` is ``sqler.ops.health_check`` when the checker is given a ``SQLerDB``.

A killed worker may leave one call it made and never journaled. Its last ``intent`` line names
the rows that call could touch; a difference in those rows (or a new row with a key it names,
or one more increment of the counter it names) is counted as ``tolerated``, not a violation.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from soak_ops import ARCHIVE, TABLE

INVARIANTS = (
    "row_missing",
    "row_doc",
    "row_deleted_present",
    "row_unknown",
    "version",
    "counter",
    "audit",
    "reference",
    "promoted_column",
    "promoted_in_data",
    "index",
    "search_missing",
    "search_deleted",
    "search_integrity",
    "rollback_left",
    "archive",
    "integrity_check",
    "foreign_key_check",
)
SEARCH = {TABLE["search_s"]: "soak_search_s_fts", TABLE["search_m"]: "soak_search_m_fts"}
AUDIT_TABLE = f"{TABLE['audit']}_audit"
PROMO = TABLE["promo"]


@dataclass
class Violation:
    invariant: str
    table: str
    row: Any
    text: str

    def line(self) -> str:
        return f"VIOLATION {self.invariant} table={self.table} id={self.row}: {self.text}"


@dataclass
class Report:
    checked: dict = field(default_factory=lambda: {name: 0 for name in INVARIANTS})
    violations: list = field(default_factory=list)
    tolerated: int = 0
    rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations

    def names(self) -> set:
        return {(v.invariant, v.table, v.row) for v in self.violations}

    def summary(self) -> str:
        return (
            f"rows={self.rows} violations={len(self.violations)} tolerated={self.tolerated} "
            f"checked={','.join(f'{k}:{v}' for k, v in self.checked.items())}"
        )


@dataclass
class Expected:
    """What the journals say: the last write of each row, and everything around it."""

    live: dict = field(default_factory=lambda: defaultdict(dict))  # table -> id -> doc
    puts: dict = field(default_factory=lambda: defaultdict(int))  # (table, id) -> writes
    audits: dict = field(default_factory=lambda: defaultdict(int))  # id -> audit rows due
    deleted: dict = field(default_factory=lambda: defaultdict(set))  # table -> ids
    archived: dict = field(default_factory=dict)  # id -> doc
    incs: dict = field(default_factory=lambda: defaultdict(int))  # counter id -> increments
    ghosts: list = field(default_factory=list)  # (table, key, word or None)
    promotions: set = field(default_factory=set)  # (table, column)
    tol_ids: set = field(default_factory=set)
    tol_keys: set = field(default_factory=set)
    tol_incs: dict = field(default_factory=lambda: defaultdict(int))


def _lines(path: str) -> Iterable[dict]:
    with open(path, encoding="utf-8") as f:
        for raw in f:
            if not raw.endswith("\n"):
                return  # a line a killed writer did not finish: it was never "told"
            yield json.loads(raw)


def read_journals(journal_dir: str, killed: Iterable[str] = ()) -> Expected:
    """Replay every journal of ``journal_dir``; ``killed`` names the journals (file stems) of
    workers that were killed, whose last unfinished intent is tolerated."""
    exp = Expected()
    killed = set(killed)
    for name in sorted(os.listdir(journal_dir)):
        if not (name.startswith("journal-") and name.endswith(".jsonl")):
            continue
        stem = name[len("journal-") : -len(".jsonl")]
        open_intent: Optional[dict] = None
        for line in _lines(os.path.join(journal_dir, name)):
            if "intent" in line:
                open_intent = line
                continue
            open_intent = None
            for fx in line.get("fx", ()):
                _apply(exp, fx)
        if open_intent is not None and stem in killed:
            for table, _id in open_intent["ids"]:
                exp.tol_ids.add((table, _id))
                if table == TABLE["counter"]:
                    exp.tol_incs[_id] += 1
            exp.tol_keys.update(open_intent["keys"])
    return exp


def _apply(exp: Expected, fx: list) -> None:
    kind = fx[0]
    if kind == "put":
        _, table, _id, doc, audit = fx
        exp.live[table][_id] = doc
        exp.puts[(table, _id)] += 1
        exp.audits[_id] += audit if table == TABLE["audit"] else 0
    elif kind == "del":
        _, table, _id, audit = fx
        exp.live[table].pop(_id, None)
        exp.deleted[table].add(_id)
        exp.audits[_id] += audit if table == TABLE["audit"] else 0
    elif kind == "inc":
        exp.incs[fx[2]] += 1
    elif kind == "ghost":
        exp.ghosts.append((fx[1], fx[2], fx[3] if len(fx) > 3 else None))
    elif kind == "arch":
        _, table, _archive, _id, doc = fx
        exp.live[table].pop(_id, None)
        exp.archived[_id] = doc
    elif kind == "promote":
        exp.promotions.add((fx[1], fx[2]))


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _rows(conn: sqlite3.Connection, table: str) -> tuple[dict, list[str], bool]:
    """Each row of ``table`` as (data, promoted columns, version), by ``_id``."""
    cols = _columns(conn, table)
    if not cols:
        return {}, [], False
    promoted = [c for c in cols if c not in ("_id", "data", "_version")]
    versioned = "_version" in cols
    sel = ", ".join(["_id", "data", "_version" if versioned else "NULL", *promoted])
    out = {}
    for row in conn.execute(f"SELECT {sel} FROM {table}"):
        out[row[0]] = (json.loads(row[1]), dict(zip(promoted, row[3:])), row[2])
    return out, promoted, versioned


class _Checker:
    def __init__(self, conn: sqlite3.Connection, exp: Expected):
        self.conn = conn
        self.exp = exp
        self.report = Report()

    def bad(self, invariant: str, table: str, row: Any, text: str, key: Any = None) -> None:
        # an index entry belongs to the row of its source table: the in-flight call of a killed
        # worker that names the row covers its entry too
        source = table[: -len("_fts")] if table.endswith("_fts") else table
        if (
            (table, row) in self.exp.tol_ids
            or (source, row) in self.exp.tol_ids
            or (key is not None and key in self.exp.tol_keys)
        ):
            self.report.tolerated += 1
            return
        self.report.violations.append(Violation(invariant, table, row, text))

    def seen(self, invariant: str, n: int = 1) -> None:
        self.report.checked[invariant] += n

    def ghost_keys(self, table: str) -> set:
        return {key for t, key, _ in self.exp.ghosts if t == table}

    def table(self, table: str) -> dict:
        exp = self.exp
        rows, promoted, versioned = _rows(self.conn, table)
        self.report.rows += len(rows)
        ghosts = self.ghost_keys(table)
        archived_src = table == TABLE["task"]
        for _id, doc in exp.live[table].items():
            self.seen("row_missing")
            got = rows.get(_id)
            if got is None:
                self.bad("row_missing", table, _id, f"journal wrote {doc}", key=doc.get("key"))
                continue
            data, cols, version = got
            want = {k: v for k, v in doc.items() if k not in promoted}
            self.seen("row_doc")
            if data != want:
                self.bad("row_doc", table, _id, f"data {data} != journal {want}")
            for col in promoted:
                self.seen("promoted_in_data")
                if col in data:
                    self.bad("promoted_in_data", table, _id, f"{col!r} is in data: {data}")
                if col in doc:
                    self.seen("promoted_column")
                    if cols[col] != doc[col]:
                        self.bad("promoted_column", table, _id, f"column {col}={cols[col]!r}, journal {doc[col]!r}")
            if versioned and table in (TABLE["safe_p"], TABLE["safe_l"]):
                self.seen("version")
                want_v = exp.puts[(table, _id)] - 1
                if version != want_v:
                    self.bad("version", table, _id, f"_version {version}, journal has {want_v + 1} writes")
        for _id in exp.deleted[table]:
            self.seen("row_deleted_present")
            if _id in rows and _id not in exp.live[table]:
                self.bad("row_deleted_present", table, _id, f"deleted, still holds {rows[_id][0]}")
        for _id in exp.archived if archived_src else ():
            self.seen("archive")
            if _id in rows:
                self.bad("archive", table, _id, "archived row is still in its table")
        for _id, (data, _cols, _v) in rows.items():
            self.seen("row_unknown")
            if _id in exp.live[table] or _id in exp.deleted[table] or (archived_src and _id in exp.archived):
                continue
            key = data.get("key")
            if key in ghosts:
                self.bad("rollback_left", table, _id, f"row of a rolled-back write: {data}")
            else:
                self.bad("row_unknown", table, _id, f"no journal knows it: {data}", key=key)
        for t, key, _ in exp.ghosts:
            if t == table:
                self.seen("rollback_left")
        return rows

    def archive(self) -> None:
        rows, promoted, _ = _rows(self.conn, ARCHIVE)
        self.report.rows += len(rows)
        src = {r for r in _rows(self.conn, TABLE["task"])[0]}
        for _id, doc in self.exp.archived.items():
            got = rows.get(_id)
            if got is None:
                if _id not in src:
                    self.bad("archive", ARCHIVE, _id, "archived row is in neither table")
                else:
                    self.bad("archive", ARCHIVE, _id, "archived row is not in the archive")
                continue
            data = got[0]
            if data != doc:
                self.bad("archive", ARCHIVE, _id, f"archive holds {data}, journal {doc}")
        for _id, (data, _c, _v) in rows.items():
            if _id not in self.exp.archived:
                self.bad("row_unknown", ARCHIVE, _id, f"no journal archived it: {data}", key=data.get("key"))

    def counters(self) -> None:
        table = TABLE["counter"]
        rows, _, _ = _rows(self.conn, table)
        self.report.rows += len(rows)
        for _id, (data, _c, version) in rows.items():
            self.seen("counter")
            want = self.exp.incs.get(_id, 0)
            slack = self.exp.tol_incs.get(_id, 0)
            if _id not in self.exp.live[table]:
                self.report.violations.append(Violation("row_unknown", table, _id, f"{data}"))
                continue
            count = data.get("count")
            if not (isinstance(count, int) and want <= count <= want + slack):
                self.report.violations.append(
                    Violation("counter", table, _id, f"count {count}, journals add up to {want} (+{slack} in flight)")
                )
            if not (isinstance(version, int) and want <= version <= want + slack):
                self.report.violations.append(
                    Violation("counter", table, _id, f"_version {version}, journals have {want} increments")
                )
            if slack and count != version:
                self.report.violations.append(Violation("counter", table, _id, f"count {count} != _version {version}"))
        for _id in self.exp.live[table]:
            if _id not in rows:
                self.report.violations.append(Violation("row_missing", table, _id, "counter is gone"))

    def audit(self, audited_rows: dict) -> None:
        exp = self.exp
        table = TABLE["audit"]
        cols = _columns(self.conn, AUDIT_TABLE)
        got: dict = defaultdict(list)
        ghosts = self.ghost_keys(table)
        if cols:
            for aid, record_id, action, snapshot in self.conn.execute(
                f"SELECT _id, record_id, action, snapshot FROM {AUDIT_TABLE} ORDER BY _id"
            ):
                snap = json.loads(snapshot) if snapshot else {}
                if snap.get("key") in ghosts:
                    self.bad("rollback_left", AUDIT_TABLE, aid, f"audit row of a rolled-back write: {snap}")
                    continue
                got[record_id].append((aid, action, snap))
        known = set(exp.live[table]) | exp.deleted[table]
        for _id in sorted(known):
            self.seen("audit")
            want = exp.audits.get(_id, 0)
            have = got.get(_id, [])
            if len(have) != want:
                self.bad("audit", table, _id, f"{len(have)} audit rows, journal has {want} audited writes")
            elif have and _id in exp.deleted[table] and _id not in exp.live[table] and have[-1][1] != "delete":
                self.bad("audit", table, _id, f"last audit row is {have[-1][1]!r}, the row was deleted")
        for record_id, entries in got.items():
            if record_id not in known:
                key = entries[0][2].get("key")
                for aid, action, _snap in entries:
                    self.bad("audit", AUDIT_TABLE, aid, f"audit row ({action}) of record {record_id} without its write", key=key)

    def references(self, jobs: dict, crews: dict) -> None:
        for _id, (data, _c, _v) in jobs.items():
            crew = data.get("crew")
            if crew is None:
                continue
            self.seen("reference")
            target = crew.get("_id") if isinstance(crew, dict) else None
            if crew.get("_table") != TABLE["crew"] or target not in crews:
                self.bad("reference", TABLE["job"], _id, f"refers to {crew}, which is not there")

    def search(self, table: str, rows: dict) -> None:
        idx = SEARCH[table]
        if not _columns(self.conn, idx):
            if self.exp.live[table]:
                self.bad("search_missing", idx, None, "the search index is gone")
            return
        entries = {rowid: body for rowid, body in self.conn.execute(f"SELECT rowid, body FROM {idx}")}
        live = self.exp.live[table]
        for _id, doc in live.items():
            self.seen("search_missing")
            body = doc["body"]
            word = body.split()[1]
            hits = [r[0] for r in self.conn.execute(f"SELECT rowid FROM {idx} WHERE {idx} MATCH ?", [f'"{word}"'])]
            if entries.get(_id) != body or hits != [_id]:
                self.bad("search_missing", idx, _id, f"word {word!r} finds {hits}, entry {entries.get(_id)!r}", key=doc.get("key"))
        # each deleted row is looked for among the entries below
        self.seen("search_deleted", len(self.exp.deleted[table] - set(live)))
        for rowid, body in entries.items():
            if rowid in live:
                continue
            self.seen("search_deleted")
            word = body.split()[1] if body and len(body.split()) > 1 else ""
            if any(g == word for t, _k, g in self.exp.ghosts if t == table):
                self.bad("rollback_left", idx, rowid, f"index entry of a rolled-back write: {body!r}")
            elif rowid in self.exp.deleted[table]:
                self.bad("search_deleted", idx, rowid, f"deleted row still found: {body!r}")
            else:
                self.bad("search_deleted", idx, rowid, f"entry of no row: {body!r}", key=word.split("v")[0] if word else None)
        for t, _key, word in self.exp.ghosts:
            if t == table and word:
                self.seen("rollback_left")
                hits = [r[0] for r in self.conn.execute(f"SELECT rowid FROM {idx} WHERE {idx} MATCH ?", [f'"{word}"'])]
                for h in hits:
                    if h not in live:
                        self.bad("rollback_left", idx, h, f"rolled-back word {word!r} is found")
        self.seen("search_integrity")
        try:
            self.conn.execute(f"INSERT INTO {idx}({idx}) VALUES('integrity-check')")
        except sqlite3.DatabaseError as exc:
            self.bad("search_integrity", idx, None, f"integrity-check: {exc}")
        finally:
            self.conn.rollback()

    def promotion(self) -> None:
        cols = _columns(self.conn, PROMO)
        for table, col in sorted(self.exp.promotions):
            self.seen("promoted_column")
            if col not in _columns(self.conn, table):
                self.bad("promoted_column", table, None, f"the promotion of {col!r} is not in the table")
        if cols:
            self.seen("index")
            found = False
            for (name,) in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", [PROMO]
            ):
                # the key columns: a column's name, or None for an expression (a field in data)
                keys = [r[2] for r in self.conn.execute(f"PRAGMA index_xinfo({name})") if r[5]]
                if len(keys) == 2 and keys[0] == "prio" and keys[1] in ("stage", None):
                    found = True
            if not found:
                self.bad("index", PROMO, None, "no index over the two fields prio and stage")

    def sqlite(self) -> None:
        self.seen("integrity_check")
        result = [r[0] for r in self.conn.execute("PRAGMA integrity_check")]
        if result != ["ok"]:
            for text in result[:20]:
                self.bad("integrity_check", "*", None, text)
        for table, rowid, parent, _fk in self.conn.execute("PRAGMA foreign_key_check"):
            self.bad("foreign_key_check", table, rowid, f"refers to a missing row of {parent}")
        self.seen("foreign_key_check")


def check(conn: sqlite3.Connection, journal_dir: str, killed: Iterable[str] = (), db: Any = None) -> Report:
    """Check the database behind ``conn`` against the journals in ``journal_dir``."""
    exp = read_journals(journal_dir, killed)
    c = _Checker(conn, exp)
    seen: dict = {}
    for name, table in TABLE.items():
        if name == "counter":
            continue
        seen[table] = c.table(table)
    c.archive()
    c.counters()
    c.audit(seen[TABLE["audit"]])
    c.references(seen[TABLE["job"]], seen[TABLE["crew"]])
    for table in SEARCH:
        c.search(table, seen[table])
    c.promotion()
    c.sqlite()
    if db is not None:
        from sqler import ops

        status = ops.health_check(db)
        if not status.healthy:
            c.bad("integrity_check", "*", None, f"health_check: {status.message}")
    return c.report


def connect(path_or_uri: str, uri: bool = False) -> sqlite3.Connection:
    """The checker's own connection (it writes nothing; the search check is rolled back)."""
    conn = sqlite3.connect(path_or_uri, uri=uri, timeout=30, isolation_level="DEFERRED")
    return conn
