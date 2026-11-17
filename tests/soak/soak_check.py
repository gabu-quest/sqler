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
- ``index``: the index over two fields of the promoted table is gone, or a key of it is not
  where the table stores the field (the column of a promoted field, else its JSON path).
- ``search_missing``: the search index does not find a row by a word only it holds, or
  holds another text for it.
- ``search_deleted``: the search index still finds a row that was deleted (or no row).
- ``search_integrity``: the index's own ``integrity-check`` fails.
- ``rollback_left``: a write that rolled back left a row, an audit row or an index entry.
- ``archive``: an archived row is in both tables or in neither, or holds another document.
- ``integrity_check`` and ``foreign_key_check``: SQLite's own checks are not clean;
  ``health`` is ``sqler.ops.health_check`` when the checker is given a ``SQLerDB``.

A killed worker may leave one call it made and never journaled. Its last ``intent`` line holds
everything that call writes when it goes through (``fx``). Every call the soak makes is one
unit by the package's documents (one statement, or one transaction: see ``CONTRIBUTING.md``,
"The soak"), so for that call the checker accepts exactly two states: none of ``fx`` stored,
or all of it, over every row, version, audit row, index entry, archive row and reference the
call writes. Any other state is reported with the violations it shows in each of the two
states (invariant, table, row), their text starting "half-applied call". Killed calls that
touch the same rows are judged together: one mix of them, each stored or not, must fit.
``tolerated`` counts the calls of killed workers found wholly stored.
"""

from __future__ import annotations

import json
import os
import re
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


#: Above this many killed calls touching the same rows the checker does not try every mix
#: of them stored or not (2**n checks); it reports them instead.
MAX_OVERLAP = 6


@dataclass
class Violation:
    invariant: str
    table: str
    row: Any
    text: str
    #: the row of a source table this is about, when ``(table, row)`` is not it (an audit row
    #: names its record, an index entry its row)
    ref: Any = None
    #: the ``key`` of the document it is about, when known (a row no journal knows by id)
    key: Any = None

    def about(self) -> tuple:
        if self.ref is not None:
            return tuple(self.ref)
        table = self.table[: -len("_fts")] if self.table.endswith("_fts") else self.table
        return (table, self.row)

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
    unresolved: list = field(default_factory=list)  # (table, key): a planned new row not stored
    inflight: list = field(default_factory=list)  # the unfinished calls of killed workers

    def copy(self) -> "Expected":
        return Expected(
            live=defaultdict(dict, {t: dict(rows) for t, rows in self.live.items()}),
            puts=defaultdict(int, self.puts),
            audits=defaultdict(int, self.audits),
            deleted=defaultdict(set, {t: set(ids) for t, ids in self.deleted.items()}),
            archived=dict(self.archived),
            incs=defaultdict(int, self.incs),
            ghosts=list(self.ghosts),
            promotions=set(self.promotions),
            unresolved=list(self.unresolved),
            inflight=list(self.inflight),
        )


@dataclass
class InFlight:
    """The unfinished call of a killed worker: what it writes when it goes through, and the
    rows (and keys of new rows) any of that can show up in."""

    worker: str
    op: str
    fx: list
    rows: set = field(default_factory=set)
    keys: set = field(default_factory=set)
    promote: bool = False

    def owns(self, v: Violation) -> bool:
        about = v.about()
        if about in self.rows or (v.key is not None and v.key in self.keys):
            return True
        return self.promote and about[0] == PROMO

    def meets(self, other: "InFlight") -> bool:
        if self.rows & other.rows or self.keys & other.keys:
            return True
        touches = {t for t, _ in self.rows}, {t for t, _ in other.rows}
        return (self.promote and (other.promote or PROMO in touches[1])) or (other.promote and PROMO in touches[0])


def _lines(path: str) -> Iterable[dict]:
    with open(path, encoding="utf-8") as f:
        for raw in f:
            if not raw.endswith("\n"):
                return  # a line a killed writer did not finish: it was never "told"
            yield json.loads(raw)


def read_journals(journal_dir: str, killed: Iterable[str] = ()) -> Expected:
    """Replay every journal of ``journal_dir``; ``killed`` names the journals (file stems) of
    workers that were killed, whose last unfinished intent is kept in ``inflight``."""
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
            fx = open_intent.get("fx", [])
            exp.inflight.append(
                InFlight(
                    stem,
                    open_intent["intent"],
                    fx,
                    rows={(t, i) for t, i in open_intent["ids"]},
                    keys=set(open_intent["keys"]),
                    promote=any(f[0] == "promote" for f in fx),
                )
            )
    return exp


def _resolve(conn: sqlite3.Connection, call: InFlight) -> None:
    """Find, by its key, the id of each new row ``call`` plans (its ``put`` names it by key):
    its audit rows and index entries are then known by that id. A planned row not found
    stays named by key; :func:`_apply` then expects it and the check finds it missing."""
    for fx in call.fx:
        if fx[0] == "put" and isinstance(fx[2], dict):
            table, key = fx[1], fx[2]["key"]
            if not _columns(conn, table):
                continue
            found = [r[0] for r in conn.execute(f"SELECT _id FROM {table} WHERE json_extract(data, '$.key') = ?", [key])]
            if len(found) == 1:
                fx[2] = found[0]
                call.rows.add((table, found[0]))


def _apply(exp: Expected, fx: list) -> None:
    kind = fx[0]
    if kind == "put" and isinstance(fx[2], dict):
        exp.unresolved.append((fx[1], fx[2]["key"]))
    elif kind == "put":
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

    def bad(self, invariant: str, table: str, row: Any, text: str, key: Any = None, ref: Any = None) -> None:
        self.report.violations.append(Violation(invariant, table, row, text, ref=ref, key=key))

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
                self.bad("row_doc", table, _id, f"data {data} != journal {want}", key=doc.get("key"))
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
                self.bad("rollback_left", table, _id, f"row of a rolled-back write: {data}", key=key)
            else:
                self.bad("row_unknown", table, _id, f"no journal knows it: {data}", key=key)
        for t, key, _ in exp.ghosts:
            if t == table:
                self.seen("rollback_left")
        for t, key in exp.unresolved:
            if t == table:
                self.bad("row_missing", table, None, f"a call planned a row with key {key!r}; none is stored", key=key)
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
            if _id not in self.exp.live[table]:
                self.bad("row_unknown", table, _id, f"{data}")
                continue
            count = data.get("count")
            if count != want:
                self.bad("counter", table, _id, f"count {count}, journals add up to {want}")
            if version != want:
                self.bad("counter", table, _id, f"_version {version}, journals have {want} increments")
        for _id in self.exp.live[table]:
            if _id not in rows:
                self.bad("row_missing", table, _id, "counter is gone")

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
                    self.bad(
                        "rollback_left", AUDIT_TABLE, aid, f"audit row of a rolled-back write: {snap}",
                        key=snap.get("key"), ref=(table, record_id),
                    )
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
                    self.bad(
                        "audit", AUDIT_TABLE, aid, f"audit row ({action}) of record {record_id} without its write",
                        key=key, ref=(table, record_id),
                    )

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
            key = word.rsplit("v", 1)[0] if word else None
            if any(g == word for t, _k, g in self.exp.ghosts if t == table):
                self.bad("rollback_left", idx, rowid, f"index entry of a rolled-back write: {body!r}", key=key)
            elif rowid in self.exp.deleted[table]:
                self.bad("search_deleted", idx, rowid, f"deleted row still found: {body!r}", key=key)
            else:
                self.bad("search_deleted", idx, rowid, f"entry of no row: {body!r}", key=key)
        for t, ghost_key, word in self.exp.ghosts:
            if t == table and word:
                self.seen("rollback_left")
                hits = [r[0] for r in self.conn.execute(f"SELECT rowid FROM {idx} WHERE {idx} MATCH ?", [f'"{word}"'])]
                for h in hits:
                    if h not in live:
                        self.bad("rollback_left", idx, h, f"rolled-back word {word!r} is found", key=ghost_key)
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
            # each key where the table stores the field: its column once promoted, else its path
            want = [f if f in cols else f"json_extract(data, '$.{f}')" for f in ("prio", "stage")]
            have = []
            for name, sql in self.conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL", [PROMO]
            ):
                keys = _index_keys(sql)
                if keys and keys[0] in ("prio", "json_extract(data, '$.prio')"):
                    have.append((name, keys))
            if not any(keys == want for _n, keys in have):
                found = "; ".join(f"{n} on ({', '.join(k)})" for n, k in have) or "none"
                self.bad("index", PROMO, None, f"no index on ({', '.join(want)}); over prio there is: {found}")

    def sqlite(self) -> None:
        self.seen("integrity_check")
        result = [r[0] for r in self.conn.execute("PRAGMA integrity_check")]
        if result != ["ok"]:
            for text in result[:20]:
                self.bad("integrity_check", "*", None, text)
        for table, rowid, parent, _fk in self.conn.execute("PRAGMA foreign_key_check"):
            self.bad("foreign_key_check", table, rowid, f"refers to a missing row of {parent}")
        self.seen("foreign_key_check")


def _index_keys(sql: str) -> list[str]:
    """The key terms of a ``CREATE INDEX`` as ``sqlite_master`` keeps it, each as sqler writes
    it (a column's name, or ``json_extract(data, '$.<field>')``), quotes and case of a column
    name dropped, ``DESC`` kept."""
    head = re.match(r"(?is)\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+.*?\bON\s+\S+?\s*\(", sql)
    if head is None:
        return []
    body = sql[head.end() :]
    terms, depth, cur = [], 0, ""
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            terms.append(cur)
            cur = ""
        else:
            cur += ch
    terms.append(cur)
    out = []
    for term in terms:
        t = " ".join(term.split())
        if "(" not in t:
            t = t.replace('"', "").replace("`", "").replace("[", "").replace("]", "").lower()
        out.append(t)
    return out


def _check_once(conn: sqlite3.Connection, exp: Expected, db: Any = None) -> Report:
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


def _groups(calls: list) -> list[list]:
    """The killed calls in groups that touch the same rows (a group's calls are judged
    together; calls of different groups touch different rows and are judged apart)."""
    groups: list[list] = []
    for call in calls:
        joined = [g for g in groups if any(call.meets(other) for other in g)]
        merged = [call]
        for g in joined:
            groups.remove(g)
            merged = g + merged
        groups.append(merged)
    return groups


def _applied(exp: Expected, calls: list) -> Expected:
    """The journals' expectation with every write of ``calls`` stored as well."""
    out = exp.copy()
    for call in calls:
        for fx in call.fx:
            _apply(out, fx)
    return out


def check(conn: sqlite3.Connection, journal_dir: str, killed: Iterable[str] = (), db: Any = None) -> Report:
    """Check the database behind ``conn`` against the journals in ``journal_dir``.

    Each group of killed calls that touch the same rows must match one mix of its calls
    stored wholly or not at all (a lone call: none of it, or all of it). The first check
    expects none of them; a group with a violation then is checked again with each other mix
    of its calls stored, and passes on the first mix that leaves it no violation."""
    exp = read_journals(journal_dir, killed)
    for call in exp.inflight:
        _resolve(conn, call)
    groups = _groups(exp.inflight)
    first = _check_once(conn, exp, db)

    def owned(report: Report, g: list) -> list:
        return [v for v in report.violations if any(call.owns(v) for call in g)]

    free = [v for v in first.violations if not any(call.owns(v) for g in groups for call in g)]
    found: dict[int, list] = {}  # group -> its violations in each mix tried, by mix
    pending = []
    accepted: dict[int, int] = {}
    for k, g in enumerate(groups):
        mine = owned(first, g)
        if not mine:
            accepted[k] = 0
        elif len(g) > MAX_OVERLAP:
            found[k] = [mine]
        else:
            found[k] = [mine]
            pending.append(k)
    mixes = max((1 << len(groups[k]) for k in pending), default=1)
    for mix in range(1, mixes):
        todo = [k for k in pending if k not in accepted and mix < (1 << len(groups[k]))]
        if not todo:
            break
        calls = [groups[k][b] for k in todo for b in range(len(groups[k])) if mix >> b & 1]
        report = _check_once(conn, _applied(exp, calls))
        for k in todo:
            mine = owned(report, groups[k])
            found[k].append(mine)
            if not mine:
                accepted[k] = mix
    result = first
    result.violations = list(free)
    result.tolerated = sum(bin(mix).count("1") for mix in accepted.values())
    for k, tries in found.items():
        if k in accepted:
            continue
        g = groups[k]
        calls = ", ".join(f"{c.op} of {c.worker}" for c in g)
        if len(g) > MAX_OVERLAP:
            shown = [(0, f"{len(g)} killed calls touch the same rows, too many to try each mix: {calls}")]
        elif len(g) == 1:
            # a lone call: what breaks if none of it is stored, and what if all of it is
            shown = [(0, f"half-applied call {calls}, if none stored"), (1, f"half-applied call {calls}, if all stored")]
        else:
            nearest = min(range(len(tries)), key=lambda m: len(tries[m]))
            stored = ", ".join(f"{c.op} of {c.worker}" for b, c in enumerate(g) if nearest >> b & 1) or "none"
            shown = [(nearest, f"half-applied calls ({calls}), nearest mix stores: {stored}")]
        for mix, why in shown:
            for v in tries[mix]:
                result.violations.append(Violation(v.invariant, v.table, v.row, f"{why}: {v.text}", ref=v.ref, key=v.key))
    return result


def connect(path_or_uri: str, uri: bool = False) -> sqlite3.Connection:
    """The checker's own connection (it writes nothing; the search check is rolled back)."""
    conn = sqlite3.connect(path_or_uri, uri=uri, timeout=30, isolation_level="DEFERRED")
    return conn
