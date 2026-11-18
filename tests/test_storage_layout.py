"""Each statement of docs/STORAGE.md, shown on a database written by sqler and read with ``sqlite3`` alone.

Every test names the section of the document it proves, and checks that the
document still says what the test shows.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from sqler import F, SQLerDB, SQLerModel, SQLerSafeModel
from sqler.exceptions import StaleVersionError
from sqler.fts import FTSIndex
from sqler.logging import query_logger
from sqler.migrations import Migration, MigrationRunner

DOC = (Path(__file__).resolve().parent.parent / "docs" / "STORAGE.md").read_text(encoding="utf-8")


class Author(SQLerModel):
    name: str


class Book(SQLerModel):
    title: str
    author: Author | None = None


@pytest.fixture
def written(tmp_path):
    """A database file written by sqler; returns a plain ``sqlite3`` connection to it.

    - ``docs``: two documents, ``{"a": 1, "nested": {"b": "x"}}`` and ``{"a": 2}``,
      a unique index on ``a``, an index on ``nested.b`` and one on ``_id``.
    - ``jobs``: promoted ``status`` and ``priority``, versioned; one row
      ``{"status": "running", "priority": 5, "payload": "p"}`` updated once.
    - ``notes``: made by a first query, no rows. ``tasks``: a safe model, saved once, then updated.
    - ``uv``, ``imv``, ``imvp``: first writes through ``upsert_with_version``,
      ``insert_many_with_version`` and ``insert_many_with_version_promoted``.
    - ``authors`` / ``books``: a book that refers to its author.
    - ``_sqler_migrations``: one applied migration.
    """
    path = str(tmp_path / "layout.db")
    db = SQLerDB.on_disk(path)
    db.insert_document("docs", {"a": 1, "nested": {"b": "x"}})
    db.insert_document("docs", {"a": 2})
    db.create_index("docs", "a", unique=True)
    db.create_index("docs", "nested.b")
    db.create_index("docs", "_id")
    db.ensure_table(
        "jobs",
        promoted={"status": "TEXT NOT NULL DEFAULT 'pending'", "priority": "INTEGER"},
        versioned=True,
    )
    _id, v = db.upsert_with_version_promoted(
        "jobs",
        None,
        {"status": "queued", "priority": 5, "payload": "p"},
        None,
        ["status", "priority"],
    )
    db.upsert_with_version_promoted(
        "jobs", _id, {"status": "running", "priority": 5, "payload": "p"}, v, ["status", "priority"]
    )

    class Note(SQLerModel):
        text: str

    class Task(SQLerSafeModel):
        n: int

    Note.set_db(db, "notes")
    Note.query().all()  # a first query on a table that does not exist yet makes it
    Task.set_db(db, "tasks")
    task = Task(n=1).save()
    task_v0 = task._version
    task.n = 2
    task.save()
    db.upsert_with_version("uv", None, {"x": 1}, None)
    db.insert_many_with_version("imv", [{"x": 1}, {"x": 2}])
    db.ensure_table("imvp", promoted={"s": "TEXT"})
    db.insert_many_with_version_promoted("imvp", [{"x": 1, "s": "a"}], ["s"])
    assert task_v0 == 0
    Author.set_db(db, "authors")
    Book.set_db(db, "books")
    ann = Author(name="Ann").save()
    Book(title="T", author=ann).save()
    MigrationRunner(db, [Migration(1, "first", lambda d: None)]).migrate()
    db.close()
    conn = sqlite3.connect(path)
    yield conn
    conn.close()


def _cols(conn, table):
    return [(r[1], r[2], r[3], r[4], r[5]) for r in conn.execute(f"PRAGMA table_info({table})")]


def test_document_names_every_statement():
    for text in (
        "_id INTEGER PRIMARY KEY AUTOINCREMENT",
        "data JSON NOT NULL",
        "_version INTEGER NOT NULL DEFAULT 0",
        '{"_table": "<table>", "_id": <id>}',
        "idx_<table>_<field>",
        "json_extract(data, '$.<field>')",
        "_sqler_migrations",
        "a table made by a query has no rows",
        "safe model",
        "`_version` key",
        "`upsert_with_version`, `insert_many_with_version`",
    ):
        assert text in DOC


def test_document_table(written):
    """Section "Document tables"."""
    assert _cols(written, "docs") == [
        ("_id", "INTEGER", 0, None, 1),
        ("data", "JSON", 1, None, 0),
    ]
    sql = written.execute("SELECT sql FROM sqlite_master WHERE name='docs'").fetchone()[0]
    assert "_id INTEGER PRIMARY KEY AUTOINCREMENT" in sql
    rows = written.execute("SELECT _id, data FROM docs ORDER BY _id").fetchall()
    assert rows == [(1, '{"a":1,"nested":{"b":"x"}}'), (2, '{"a":2}')]
    assert written.execute(
        "SELECT name, seq FROM sqlite_sequence WHERE name='docs'"
    ).fetchone() == ("docs", 2)


def test_promoted_and_version_columns(written):
    """Sections "Promoted columns" and "The version column"."""
    assert _cols(written, "jobs") == [
        ("_id", "INTEGER", 0, None, 1),
        ("data", "JSON", 1, None, 0),
        ("status", "TEXT", 1, "'pending'", 0),
        ("priority", "INTEGER", 0, None, 0),
        ("_version", "INTEGER", 1, "0", 0),
    ]
    row = written.execute("SELECT _id, data, status, priority, _version FROM jobs").fetchall()
    assert row == [(1, '{"payload":"p"}', "running", 5, 1)]


def test_reference(written):
    """Section "References"."""
    (data,) = written.execute("SELECT data FROM books").fetchone()
    assert json.loads(data) == {"title": "T", "author": {"_table": "authors", "_id": 1}}
    assert written.execute(
        "SELECT json_extract(data, '$.name') FROM authors WHERE _id = 1"
    ).fetchone() == ("Ann",)


def test_index_names(written):
    """Section "Indexes made by create_index"."""
    rows = written.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='docs' ORDER BY name"
    ).fetchall()
    assert [r[0] for r in rows] == ["idx_docs__id", "idx_docs_a", "idx_docs_nested_b"]
    by_name = dict(rows)
    assert (
        by_name["idx_docs_a"].split()
        == "CREATE UNIQUE INDEX idx_docs_a ON docs (json_extract(data, '$.a'))".split()
    )
    assert by_name["idx_docs_nested_b"].split() == (
        "CREATE INDEX idx_docs_nested_b ON docs (json_extract(data, '$.nested.b'))".split()
    )
    assert by_name["idx_docs__id"].split() == "CREATE INDEX idx_docs__id ON docs (_id)".split()


def test_migrations_table(written):
    """Section "The migrations table"."""
    assert _cols(written, "_sqler_migrations") == [
        ("version", "INTEGER", 0, None, 1),
        ("name", "TEXT", 1, None, 0),
        ("applied_at", "TEXT", 1, None, 0),
        ("duration_ms", "REAL", 1, None, 0),
        ("checksum", "TEXT", 0, None, 0),
    ]
    assert written.execute("SELECT version, name FROM _sqler_migrations").fetchall() == [
        (1, "first")
    ]


def test_table_made_by_a_query(written):
    """Section "Document tables": a first query makes the table."""
    assert _cols(written, "notes") == [("_id", "INTEGER", 0, None, 1), ("data", "JSON", 1, None, 0)]
    assert written.execute("SELECT COUNT(*) FROM notes").fetchone() == (0,)


def test_safe_model_table_has_version_and_data_key(written):
    """Section "The version column": safe models; the version is the column, not a key in data."""
    assert [c[0] for c in _cols(written, "tasks")] == ["_id", "data", "_version"]
    assert _cols(written, "tasks")[2] == ("_version", "INTEGER", 1, "0", 0)
    ((data, version),) = written.execute("SELECT data, _version FROM tasks").fetchall()
    # saved once, then updated once: the column says 1, and the document holds no copy
    assert version == 1
    assert json.loads(data) == {"n": 2}


def test_with_version_writes_add_the_column_and_insert_zero(written):
    """Section "The version column": the `*_with_version` writes."""
    for table in ("uv", "imv", "imvp"):
        assert _cols(written, table)[-1] == ("_version", "INTEGER", 1, "0", 0)
    assert written.execute("SELECT data, _version FROM uv").fetchall() == [('{"x":1}', 0)]
    assert written.execute("SELECT data, _version FROM imv ORDER BY _id").fetchall() == [
        ('{"x":1}', 0),
        ('{"x":2}', 0),
    ]
    assert written.execute("SELECT data, s, _version FROM imvp").fetchall() == [('{"x":1}', "a", 0)]


def test_record_of_settled_promoted_columns(written):
    """Section "The record of settled promoted columns"."""
    assert "`_sqler_promoted`" in DOC
    assert (
        "tbl TEXT NOT NULL COLLATE NOCASE, col TEXT NOT NULL COLLATE NOCASE, PRIMARY KEY (tbl, col)) WITHOUT ROWID"
        in DOC
    )
    assert _cols(written, "_sqler_promoted") == [
        ("tbl", "TEXT", 1, None, 1),
        ("col", "TEXT", 1, None, 2),
    ]
    (sql,) = written.execute(
        "SELECT sql FROM sqlite_master WHERE name = '_sqler_promoted'"
    ).fetchone()
    assert sql.endswith("PRIMARY KEY (tbl, col)) WITHOUT ROWID")
    assert written.execute("SELECT tbl, col FROM _sqler_promoted ORDER BY tbl, col").fetchall() == [
        ("imvp", "s"),
        ("jobs", "priority"),
        ("jobs", "status"),
    ]
    # no document of a recorded column's table holds the column's key
    assert written.execute(
        "SELECT COUNT(*) FROM jobs WHERE json_type(data, '$.status') IS NOT NULL OR json_type(data, '$.priority') IS NOT NULL"
    ).fetchone() == (0,)


def test_a_write_names_only_the_columns_its_document_holds(tmp_path):
    """Section "Promoted columns": a key that names a column goes into it, a column whose key
    the document does not hold is not written (an insert gives its DEFAULT, an update leaves
    it), a key held as ``None`` writes NULL, a key in another case is a key of ``data``."""
    for text in (
        "a generated column is\nnot one",
        "an update leaves its value, and an insert leaves it out, so SQLite\ngives it its `DEFAULT`",
        "A key held with the value `None` writes NULL.",
        "SQLite checks `NOT NULL` first",
        "(`Status` beside the column `status`) is a key of\n`data`",
    ):
        assert text in DOC
    path = str(tmp_path / "w.db")
    db = SQLerDB.on_disk(path)
    db.ensure_table("w", promoted={"status": "TEXT NOT NULL DEFAULT 'pending'", "note": "TEXT"})
    db.insert_document("w", {"name": "a", "status": "open", "note": "n"})
    db.insert_document("w", {"name": "b"})
    db.insert_document("w", {"name": "c", "Status": "x"})
    db.upsert_document("w", 1, {"name": "a2"})
    db.upsert_document("w", 2, {"name": "b2", "note": None, "status": "done"})
    db.close()
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT _id, data, status, note FROM w ORDER BY _id").fetchall()
    finally:
        conn.close()
    assert rows == [
        (1, '{"name":"a2"}', "open", "n"),
        (2, '{"name":"b2"}', "done", None),
        (3, '{"name":"c","Status":"x"}', "pending", None),
    ]


def test_a_database_object_learns_a_tables_columns_once(tmp_path):
    """Section "Promoted columns": an object learns a table's columns at its first write of
    it; SQLite refuses its next statement once another object added a column, and this
    object learns again: the key goes into its column."""
    assert "Only then does the object learn the table again and\nbuild the statement again" in DOC
    path = str(tmp_path / "m.db")
    first = SQLerDB.on_disk(path)
    first.insert_document("m", {"name": "a"})
    other = SQLerDB.on_disk(path)
    other.ensure_table("m", promoted={"status": "TEXT"})
    other.close()
    first.insert_document("m", {"name": "b", "status": "kept in data"})
    first.close()
    fresh = SQLerDB.on_disk(path)
    fresh.insert_document("m", {"name": "c", "status": "in its column"})
    fresh.close()
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT _id, data, status FROM m ORDER BY _id").fetchall()
    finally:
        conn.close()
    assert rows == [
        (1, '{"name":"a"}', None),
        (2, '{"name":"b"}', "kept in data"),
        (3, '{"name":"c"}', "in its column"),
    ]


# ---- what a database object remembers holds for one schema version ----------------------------
#
# Object ``a`` has learned table ``t`` (written to it, read it, asked for ``_version``); object
# ``b`` on the same file then changes the table. Each way ``a`` writes or reads must act on the
# table as it is now: the key in its column, the column read back.

# scene: (what ``a`` learns first, what ``b`` does, the key ``a`` writes, the value ``b`` leaves in it)
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
ONE_WAY = ("rename a column", "drop a column", "same columns in another order")
REMADE = ("drop and make again", "same columns in another order")


class PromotingRow(SQLerModel):
    """A class of ``t`` that promotes ``status`` itself: its writers are told the columns."""

    name: str
    status: str | None = None
    prio: str | None = None
    other: str | None = None

    __tablename__ = "t"
    __promoted__ = {"status": "TEXT"}


class SceneRow(SQLerModel):
    name: str
    status: str | None = None
    prio: str | None = None
    other: str | None = None

    __tablename__ = "t"


def _write_ways(a, key):
    doc = {"name": "y"} if key == "_version" else {"name": "y", key: "v"}
    return {
        "insert_document": lambda: a.insert_document("t", doc),
        "upsert_document": lambda: a.upsert_document("t", 1, doc),
        "the fields update": lambda: (a._update_fields("t", 1, doc, list(doc)), 1)[1],
        "insert_many": lambda: a.insert_many("t", [doc])[0],
        "bulk_upsert": lambda: a.bulk_upsert("t", [dict(doc, _id=1)])[0],
        "upsert_with_version": lambda: a.upsert_with_version("t", None, doc, None)[0],
        "model save": lambda: SceneRow(**doc).save(db=a)._id,
        "promoting model save": lambda: PromotingRow(**doc).save(db=a)._id,
        "promoting save_many": lambda: PromotingRow.save_many([PromotingRow(**doc)], db=a)[0]._id,
    }


# a class's own promoted column is gone from a table remade without it: its save is refused
# (``test_a_class_whose_promoted_column_another_object_dropped_is_refused_once``)
PROMOTING = ("promoting model save", "promoting save_many")


def _searched_in_sync(a, key):
    """What a search index ``a`` makes now finds after ``a`` writes a second row like
    row 1: the triggers ``a`` installs read the key where the table stores it."""
    field = "name" if key == "_version" else key
    index = FTSIndex(SceneRow, [field], index_name="t_synced", db=a)
    index.create(a)
    a.insert_document("t", {"name": "x"} if key == "_version" else {"name": "x", key: "old"})
    return [hit.model_dump().get(key) for hit in index.search("x" if key == "_version" else "old")]


# each reads row 1 of ``t``: the value of the key (a plain model does not read ``_version``)
READS = {
    "find_document": lambda a, key: (
        a.find_document_with_version if key == "_version" else a.find_document
    )("t", 1)[key],
    "from_id": lambda a, key: SceneRow.from_id(1, db=a).model_dump().get(key),
    "query all": lambda a, key: next(r for r in SceneRow.using(a).all() if r._id == 1)
    .model_dump()
    .get(key),
    "count": lambda a, key: SceneRow.using(a)
    .filter(F(key) == (7 if key == "_version" else "old"))
    .count(),
    "search in sync": _searched_in_sync,
}


def _cells(ways, one):
    """The (scene, way) cells: every way for each scene, ``one`` alone for a scene of ``ONE_WAY``."""
    return [
        (scene, way)
        for scene in SCENES
        for way in ways
        if (scene not in ONE_WAY or way == one)
        and not (scene == "drop and make again" and way in PROMOTING)
    ]


def _scene(tmp_path, name):
    """``a`` with ``t`` learned, after ``b`` changed it and left row 1 with ``'old'`` in the
    key's column (``_version`` 7); returns ``(a, path, key)``."""
    first, change, key = SCENES[name]
    path = str(tmp_path / "scene.db")
    a = SQLerDB.on_disk(path)
    a.ensure_table("t", promoted=first) if first else a._ensure_table("t")
    a.insert_document("t", {"name": "x"})
    a.find_document("t", 1)
    a._has_version_column("t")
    b = SQLerDB.on_disk(path)
    change(b)
    if name in REMADE:
        b.insert_document("t", {"name": "x"})
    b.adapter.execute(
        f"UPDATE t SET {key} = {7 if key == '_version' else repr('old')} WHERE _id = 1;"
    )
    b.adapter.auto_commit()
    b.close()
    return a, path, key


def _stored(path, _id, key):
    conn = sqlite3.connect(path)
    try:
        data, value = conn.execute(f"SELECT data, {key} FROM t WHERE _id = ?", [_id]).fetchone()
    finally:
        conn.close()
    return json.loads(data), value


@pytest.mark.parametrize(("scene", "way"), _cells(_write_ways(None, "k"), "insert_document"))
def test_a_write_after_another_object_changed_the_table_lands_each_key_in_its_column(
    tmp_path, scene, way
):
    """Section "Promoted columns": SQLite refuses a statement built for the columns ``t`` had,
    so ``a`` learns ``t`` again and writes it as it is."""
    a, path, key = _scene(tmp_path, scene)
    try:
        with query_logger.capture() as sent:
            _id = _write_ways(a, key)[way]()
    finally:
        a.close()
    learned = [q.sql for q in sent if q.sql.startswith("PRAGMA")]
    assert bool(learned) is (scene != "same columns in another order")
    data, value = _stored(path, _id, key)
    if key == "_version":
        assert ("_version" in data, type(value)) == (False, int)
    else:
        assert (data["name"], key in data, value) == ("y", False, "v")
    fresh = SQLerDB.on_disk(path)
    try:
        assert fresh.find_document("t", _id).get(key, "absent") == value
    finally:
        fresh.close()


@pytest.mark.parametrize(("scene", "way"), _cells(READS, "find_document"))
def test_a_read_after_another_object_changed_the_table_returns_the_column(tmp_path, scene, way):
    a, _, key = _scene(tmp_path, scene)
    try:
        got = READS[way](a, key)
    finally:
        a.close()
    old = 7 if key == "_version" else "old"
    model = None if key == "_version" else old
    expected = {"find_document": old, "from_id": model, "query all": model, "count": 1}
    assert got == {**expected, "search in sync": [model, model]}[way]


def _two(tmp_path, **kw):
    path = str(tmp_path / "two.db")
    a, b = SQLerDB.on_disk(path, **kw), SQLerDB.on_disk(path, **kw)
    a.insert_document("t", {"name": "x"})
    return a, b, path


def test_a_change_between_the_begin_and_the_first_write_waits(tmp_path):
    """``a``'s transaction takes the write lock at its begin: ``b``'s promotion waits for it."""
    a, b, path = _two(tmp_path, timeout_ms=50)
    try:
        with a.transaction():
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                b.ensure_table("t", promoted={"status": "TEXT"})
            _id = a.insert_document("t", {"name": "y", "status": "v"})
    finally:
        a.close(), b.close()
    assert _stored(path, _id, "data") == ({"name": "y", "status": "v"}, '{"name":"y","status":"v"}')


def test_no_change_lands_between_the_check_and_a_write_inside_an_open_unit(tmp_path):
    """``a`` holds its unit open after a write: its write lock keeps ``b``'s promotion out
    until it commits, so the next write of the unit acts on the table it checked."""
    a, b, path = _two(tmp_path, timeout_ms=50)
    try:
        with a.transaction():
            a.insert_document("t", {"name": "y", "status": "v"})
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                b.ensure_table("t", promoted={"status": "TEXT"})
            a.insert_document("t", {"name": "z", "status": "w"})
        b.ensure_table("t", promoted={"status": "TEXT"})
        _id = a.insert_document("t", {"name": "after", "status": "v"})
    finally:
        a.close(), b.close()
    assert _stored(path, _id, "status") == ({"name": "after"}, "v")


def test_a_change_while_a_cursor_is_half_read_is_seen_by_the_next_write(tmp_path):
    """``b`` promotes while ``a`` holds a half-read cursor (a read snapshot from before the
    change). ``a``'s write first reads the rest of the cursor, which ends that snapshot: the
    write sees the column, and the cursor gives the rows its statement saw."""
    a, b, path = _two(tmp_path, timeout_ms=50)
    for n in range(3):
        a.insert_document("t", {"name": f"r{n}"})
    try:
        cursor = a.adapter.execute("SELECT _id FROM t ORDER BY _id;")
        assert tuple(cursor.fetchone()) == (1,)
        b.ensure_table("t", promoted={"status": "TEXT"})
        _id = a.insert_document("t", {"name": "y", "status": "v"})
        assert [r[0] for r in cursor.fetchall()] == [2, 3, 4]
    finally:
        a.close(), b.close()
    assert (_id, _stored(path, _id, "status")) == (5, ({"name": "y"}, "v"))


def test_a_promotion_by_another_process_is_seen(tmp_path):
    import subprocess
    import sys

    a, _, path = _two(tmp_path)
    try:
        code = "import sys; from sqler import SQLerDB; SQLerDB.on_disk(sys.argv[1]).ensure_table('t', promoted={'status': 'TEXT'})"
        subprocess.run([sys.executable, "-c", code, path], check=True, timeout=60)
        _id = a.insert_document("t", {"name": "y", "status": "v"})
    finally:
        a.close()
    assert _stored(path, _id, "status") == ({"name": "y"}, "v")


def test_a_move_of_the_version_for_another_table_costs_no_relearn_and_changes_nothing(tmp_path):
    """1,000 tables made by ``b``: ``t`` has the columns ``a`` remembers, so SQLite runs each
    write of ``a`` as it is, and ``a`` never looks at ``t`` again."""
    a, b, path = _two(tmp_path)
    a.ensure_table("t", promoted={"status": "TEXT"})
    try:
        with query_logger.capture() as sent:
            for n in range(1000):
                b._ensure_table(f"other_{n}")
                a.insert_document("t", {"name": f"y{n}", "status": "v"})
        assert [q.sql for q in sent if q.sql.lstrip().upper().startswith("PRAGMA")] == []
        assert a._promoted_of("t") == ["status"]
    finally:
        a.close(), b.close()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT count(*) FROM t WHERE status = 'v' AND data NOT LIKE '%status%'"
        ).fetchone() == (1000,)
    finally:
        conn.close()


def test_a_table_dropped_by_another_object_is_not_written_by_its_old_layout(tmp_path):
    from sqler.exceptions import NotFoundError

    a, b, path = _two(tmp_path)
    a.ensure_table("t", promoted={"status": "TEXT"})
    b.adapter.execute("DROP TABLE t;")
    b.adapter.auto_commit()
    try:
        with pytest.raises(NotFoundError):
            a.upsert_document("t", 1, {"name": "y", "status": "v"})
        _id = a.insert_document("t", {"name": "y", "status": "v"})
        assert a._promoted_of("t") == []
    finally:
        a.close(), b.close()
    assert _stored(path, _id, "data") == ({"name": "y", "status": "v"}, '{"name":"y","status":"v"}')


def test_a_new_row_in_a_remembered_table_sends_its_insert_alone(tmp_path):
    a, b, path = _two(tmp_path)
    try:
        with query_logger.capture() as sent:
            a.insert_document("t", {"name": "y"})
            SceneRow(name="z").save(db=a)
    finally:
        a.close(), b.close()
    assert [q.sql.split()[0] for q in sent] == ["INSERT", "INSERT"]
    assert (_stored(path, 2, "data")[0], _stored(path, 3, "data")[0]["name"]) == (
        {"name": "y"},
        "z",
    )


def test_a_new_row_in_a_remembered_table_another_object_dropped_makes_the_table_again(tmp_path):
    """The insert is sent with no ``CREATE TABLE`` before it and SQLite refuses it; the table
    is found gone and forgotten, and the insert built again makes it."""
    a, b, path = _two(tmp_path)
    b.adapter.execute("DROP TABLE t;")
    b.adapter.auto_commit()
    try:
        with query_logger.capture() as sent:
            _id = a.insert_document("t", {"name": "y"})
    finally:
        a.close(), b.close()
    assert [q.sql.split()[0] for q in sent] == ["INSERT", "PRAGMA", "CREATE", "PRAGMA", "INSERT"]
    assert (_id, _stored(path, _id, "data")) == (1, ({"name": "y"}, '{"name":"y"}'))


@pytest.mark.parametrize("memory", ["named", "one adapter"])
def test_two_objects_on_one_in_memory_database_see_each_others_change(memory):
    if memory == "named":
        a, b = SQLerDB.in_memory(name="sq6ca"), SQLerDB.in_memory(name="sq6ca")
    else:
        a = SQLerDB.in_memory(shared=False)
        b = SQLerDB(a.adapter)
    try:
        a.insert_document("t", {"name": "x"})
        b.ensure_table("t", promoted={"status": "TEXT"})
        _id = a.insert_document("t", {"name": "y", "status": "v"})
        row = a.adapter.execute("SELECT data, status FROM t WHERE _id = ?;", [_id]).fetchone()
        assert (json.loads(row[0]), row[1]) == ({"name": "y"}, "v")
    finally:
        a.close()
        if memory == "named":
            b.close()


def test_a_promotion_by_the_object_itself_in_its_transaction_asks_nothing_again(tmp_path):
    from sqler.logging import query_logger

    a, _, path = _two(tmp_path)
    try:
        with a.transaction():
            a.ensure_table("t", promoted={"status": "TEXT"})
            with query_logger.capture() as sent:
                _id = a.insert_document("t", {"name": "y", "status": "v"})
                a.find_document("t", _id)
        assert [q.sql.split()[0] for q in sent] == ["INSERT", "SELECT"]
        with query_logger.capture() as sent:
            a.insert_document("t", {"name": "z", "status": "w"})
        assert [q.sql.split()[0] for q in sent] == ["INSERT"]
    finally:
        a.close()
    assert _stored(path, _id, "status") == ({"name": "y"}, "v")


# ---- the guard itself, on plain ``sqlite3`` ----------------------------------------------------


def _plain_guard(names):
    row = ", ".join(f"`{n}`" for n in names)
    return f"CASE WHEN 1 THEN 1 ELSE (SELECT ({row}) IN (SELECT * FROM t) FROM t) END"


def test_sqlite_refuses_a_row_value_of_other_names_than_select_star_where_the_branch_never_runs(
    tmp_path,
):
    """The mechanism every statement built from what an object remembers relies on, without
    sqler: a SQLite that stops refusing fails here, by this name."""
    path = str(tmp_path / "plain.db")
    a, b = sqlite3.connect(path), sqlite3.connect(path)
    try:
        a.execute("CREATE TABLE t (_id INTEGER PRIMARY KEY, data TEXT, status TEXT)")
        a.execute("INSERT INTO t (data) VALUES ('{}')")
        a.commit()
        with pytest.raises(
            sqlite3.OperationalError, match="sub-select returns 3 columns - expected 2"
        ):
            a.execute(f"SELECT _id FROM t WHERE {_plain_guard(['_id', 'data'])}")
        with pytest.raises(sqlite3.OperationalError, match="no such column: gone"):
            a.execute(f"SELECT _id FROM t WHERE {_plain_guard(['_id', 'data', 'gone'])}")
        held = f"SELECT _id FROM t WHERE {_plain_guard(['_id', 'data', 'status'])}"
        assert a.execute(held).fetchall() == [(1,)]  # prepared and cached by the driver
        b.execute("ALTER TABLE t ADD COLUMN note TEXT")
        b.commit()
        with pytest.raises(
            sqlite3.OperationalError, match="sub-select returns 4 columns - expected 3"
        ):
            a.execute(held)
        # a connection that has not seen a column yet reloads the schema at the name it lacks
        c = sqlite3.connect(path)
        c.execute("SELECT * FROM t").fetchall()
        b.execute("ALTER TABLE t ADD COLUMN later TEXT")
        b.commit()
        assert c.execute(
            f"SELECT _id FROM t WHERE {_plain_guard(['_id', 'data', 'status', 'note', 'later'])}"
        ).fetchall() == [(1,)]
        c.close()
    finally:
        a.close(), b.close()


# ---- hostile inputs ------------------------------------------------------------------------------


def test_a_table_with_a_generated_column_is_learned_as_select_star_gives_it(tmp_path):
    """A generated column made with plain SQL is in what ``a`` remembers (``SELECT *`` gives
    it), so nothing is refused for ever; no write names it, every way reads the row."""
    path = str(tmp_path / "gen.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE t (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL, status TEXT, "
        "g TEXT GENERATED ALWAYS AS (upper(json_extract(data, '$.name'))) VIRTUAL)"
    )
    conn.commit()
    conn.close()
    a = SQLerDB.on_disk(path)
    try:
        with query_logger.capture() as sent:
            _id = a.insert_document("t", {"name": "x", "status": "old"})
            a.upsert_document("t", _id, {"name": "x", "status": "old"})
            a.insert_many("t", [{"name": "y"}])
            a.bulk_upsert("t", [{"name": "x", "status": "old", "_id": _id}])
            a._update_fields("t", _id, {"status": "old"}, ["status"])
            got = (
                a.find_document("t", _id)["status"],
                SceneRow.from_id(_id, db=a).status,
                [r.status for r in SceneRow.using(a).all()],
                SceneRow.using(a).filter(F("status") == "old").count(),
            )
    finally:
        a.close()
    assert got == ("old", "old", ["old", None], 1)
    assert [q.sql for q in sent if q.sql.startswith("PRAGMA")] == ['PRAGMA table_xinfo("t");']
    assert [
        q.sql for q in sent if q.sql.startswith("INSERT") and " g" in q.sql.split("VALUES")[0]
    ] == []
    assert [q.error for q in sent if q.error and "_sqler_promoted" not in q.sql] == []


def test_columns_whose_names_need_quoting_are_in_the_guard_as_names(tmp_path):
    """Columns added with plain SQL under names sqler never reads (a space, two spaces, a
    backtick, a double quote): the guard names each as an identifier, so every statement runs."""
    path = str(tmp_path / "quoted.db")
    a = SQLerDB.on_disk(path)
    a.ensure_table("t", promoted={"status": "TEXT"})
    conn = sqlite3.connect(path)
    for name in ('"my col"', '"two  spaces"', '"a`b"', '"x""y"'):
        conn.execute(f"ALTER TABLE t ADD COLUMN {name} TEXT")
    conn.commit()
    conn.close()
    try:
        with query_logger.capture() as sent:
            _id = a.insert_document("t", {"name": "x", "status": "old"})
            got = (
                a.find_document("t", _id)["status"],
                [r.status for r in SceneRow.using(a).all()],
                SceneRow.using(a).filter(F("status") == "old").count(),
                SceneRow.using(a).filter(F("status") == "old").update(prio="p"),
            )
    finally:
        a.close()
    assert got == ("old", ["old"], 1, 1)
    assert [q.error for q in sent if q.error] == ["sub-select returns 7 columns - expected 3"]


def test_the_callers_own_mistake_is_raised_as_sqlite_raised_it_after_one_learning(tmp_path):
    from sqler.query import SQLerExpression

    a, b, path = _two(tmp_path)
    try:
        a.find_document("t", 1)
        with query_logger.capture() as sent:
            with pytest.raises(sqlite3.OperationalError, match="^no such column: nocolumn$"):
                SceneRow.using(a).filter(SQLerExpression("nocolumn = 1", [])).all()
    finally:
        a.close(), b.close()
    assert [(q.sql.split()[0], q.error) for q in sent] == [
        ("SELECT", "no such column: nocolumn"),
        ("PRAGMA", None),
    ]


def test_a_locked_database_is_raised_and_not_sent_again(tmp_path):
    a, b, path = _two(tmp_path, timeout_ms=50)
    try:
        b.adapter.execute("BEGIN IMMEDIATE;")
        with query_logger.capture() as sent:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                a.upsert_document("t", 1, {"name": "y"})
        b.adapter.execute("ROLLBACK;")
    finally:
        a.close(), b.close()
    assert [(q.sql.split()[0], q.error) for q in sent if not q.sql.startswith("PRAGMA")] == [
        ("UPDATE", "database is locked")
    ]


@pytest.mark.parametrize(
    "nested", [False, True], ids=["in the unit", "in a nested level that rolls back"]
)
def test_a_refusal_inside_a_unit_keeps_its_earlier_write(tmp_path, nested):
    """``b`` promoted after ``a`` learned ``t``: ``a``'s statement inside its unit is refused,
    learned again and sent again, and the unit's earlier write is there after the commit."""
    a, b, path = _two(tmp_path)
    try:
        b.ensure_table("t", promoted={"status": "TEXT"})
        with a.transaction():
            first = a.insert_document("t", {"name": "earlier"})
            if nested:
                with pytest.raises(KeyError):
                    with a.transaction():
                        a.insert_document("t", {"name": "inner", "status": "v"})
                        raise KeyError("roll the level back")
            _id = a.insert_document("t", {"name": "y", "status": "v"})
    finally:
        a.close(), b.close()
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT _id, data, status FROM t ORDER BY _id").fetchall()
    finally:
        conn.close()
    assert rows[-2:] == [(first, '{"name":"earlier"}', None), (_id, '{"name":"y"}', "v")]
    assert [r for r in rows if "inner" in r[1]] == []


def test_two_changes_by_another_object_between_two_statements_are_learned_once(tmp_path):
    a, b, path = _two(tmp_path)
    try:
        b.ensure_table("t", promoted={"status": "TEXT"})
        b.ensure_table("t", promoted={"status": "TEXT", "prio": "TEXT"})
        with query_logger.capture() as sent:
            _id = a.insert_document("t", {"name": "y", "status": "v", "prio": "p"})
    finally:
        a.close(), b.close()
    assert [q.sql.split()[0] for q in sent if q.sql.startswith("PRAGMA")] == ["PRAGMA"]
    assert _stored(path, _id, "prio") == ({"name": "y"}, "p")


def test_a_table_dropped_by_another_object_between_two_reads_reads_as_none(tmp_path):
    a, b, path = _two(tmp_path)
    try:
        assert a.find_document("t", 1) == {"name": "x", "_id": 1}
        b.adapter.execute("DROP TABLE t;")
        b.adapter.auto_commit()
        assert (a.find_document("t", 1), SceneRow.using(a).count(), SceneRow.using(a).all()) == (
            None,
            0,
            [],
        )
    finally:
        a.close(), b.close()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 't'").fetchall() == []
    finally:
        conn.close()


# says it has started (the SQL function ``started``), then works with no Python in it
_SLOW = (
    "(WITH RECURSIVE c(x) AS (SELECT started() UNION ALL SELECT x + 1 FROM c WHERE x < 3000000) "
    "SELECT count(*) FROM c) > 0"
)


def test_a_keyboard_interrupt_while_the_statement_runs_is_not_caught(tmp_path):
    """Only SQLite's refusal (``OperationalError``) is caught: an interrupt that lands while a
    statement runs leaves at once, and the statement is not sent again.

    Another thread interrupts the main thread once the statement says it runs: the
    interrupt is pending while SQLite works and is raised as the statement returns. No
    alarm signal (Windows has none), and no timer to outrun."""
    import _thread
    import signal
    import threading

    from sqler.query import SQLerExpression

    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("this process does not turn SIGINT into KeyboardInterrupt")
    a, b, path = _two(tmp_path)
    running, wanted = threading.Event(), [True]

    def started():
        running.set()
        return 1

    def interrupt_once_it_runs():
        if running.wait(60) and wanted[0]:
            _thread.interrupt_main()

    a.adapter._conn().create_function("started", 0, started)
    sender = threading.Thread(target=interrupt_once_it_runs)
    try:
        with query_logger.capture() as sent:
            sender.start()
            with pytest.raises(KeyboardInterrupt):
                SceneRow.using(a).filter(SQLerExpression(_SLOW, [])).count()
    finally:
        try:  # an interrupt sent after the statement had returned is taken here, not by the run
            wanted[0] = False
            running.set()
            sender.join()
        except KeyboardInterrupt:
            pass
        a.close(), b.close()
    assert [(q.sql.split()[0], q.error) for q in sent] == [
        ("SELECT", "KeyboardInterrupt")
    ]  # sent once, nothing learned


# ---- a writer splits by the table's columns; a bulk run checks them once ------------------------


class StatusRow(SQLerModel):
    name: str
    status: str | None = None
    prio: str | None = None

    __tablename__ = "two_classes"
    __promoted__ = {"status": "TEXT"}


class PrioRow(SQLerModel):
    name: str
    status: str | None = None
    prio: str | None = None

    __tablename__ = "two_classes"
    __promoted__ = {"prio": "TEXT"}


@pytest.mark.parametrize("way", ["save", "save_many"])
def test_two_classes_that_promote_different_fields_of_one_table_each_write_both_columns(
    tmp_path, way
):
    """Each class tells the writer its own promoted field; the writer puts every key that
    names a column of the table into it, so neither class's save hides the other's column."""
    path = str(tmp_path / "two_classes.db")
    a = SQLerDB.on_disk(path)
    try:
        for n, cls in enumerate((StatusRow, PrioRow, StatusRow)):
            row = cls(name=f"r{n}", status="s", prio="p")
            row.save(db=a) if way == "save" else cls.save_many([row], db=a)
        got = [(r.status, r.prio) for r in StatusRow.using(a).all()]
    finally:
        a.close()
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT data, status, prio FROM two_classes ORDER BY _id").fetchall()
    finally:
        conn.close()
    assert [(json.loads(d), s, p) for d, s, p in rows] == [
        ({"name": f"r{n}"}, "s", "p") for n in range(3)
    ]
    assert got == [("s", "p")] * 3


def test_a_class_whose_promoted_column_another_object_dropped_is_refused_once(tmp_path):
    """``b`` remade ``t`` without ``status``: the save that still names it is refused by
    SQLite, nothing is written, and the next save of the class makes the column again."""
    a, path, _ = _scene(tmp_path, "drop and make again")
    try:
        with pytest.raises(sqlite3.OperationalError, match="^table t has no column named status$"):
            PromotingRow(name="y", status="v").save(db=a)
        assert sqlite3.connect(path).execute("SELECT count(*) FROM t").fetchone() == (1,)
        _id = PromotingRow(name="z", status="w").save(db=a)._id
    finally:
        a.close()
    data, value = _stored(path, _id, "status")
    assert (data["name"], "status" in data, value) == ("z", False, "w")


BULK = {
    "insert_many": lambda a, docs: a.insert_many("t", docs),
    "bulk_upsert": lambda a, docs: a.bulk_upsert("t", docs),
}


@pytest.mark.parametrize("n", [1, 2, 2500])
@pytest.mark.parametrize("way", BULK)
def test_a_bulk_run_is_guarded_once_in_a_first_statement_of_one_row_or_by_a_check_before_it(
    tmp_path, way, n
):
    """A first statement of one row carries the guard of the remembered names. Before a
    longer one the check is a statement of its own, first in the run's transaction, and no
    statement of rows holds a ``SELECT``, so SQLite keeps its fast path."""
    a, _, path = _two(tmp_path)
    try:
        a.find_document("t", 1)
        with query_logger.capture() as sent:
            ids = BULK[way](a, [{"name": f"r{i}"} for i in range(n)])
        stored = sqlite3.connect(path).execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        a.close()
    kinds = [q.sql.split()[0] for q in sent]
    guard = "(SELECT (`_id`, `data`) IN (SELECT * FROM t) FROM t)"
    if n == 1:
        assert kinds == ["CREATE", "INSERT"]
        assert (
            sent[1].sql.count(f"json(CASE WHEN 1 THEN ? ELSE {guard} END)"),
            sent[1].sql.count("SELECT"),
        ) == (1, 2)
    else:
        assert kinds == ["CREATE", "SELECT", *["INSERT"] * -(-n // 999)]
        assert sent[1].sql == f"SELECT CASE WHEN 1 THEN 1 ELSE {guard} END;"
        assert [q.sql for q in sent[2:] if "SELECT" in q.sql] == []
    assert (ids[0], ids[-1], stored) == (2, n + 1, n + 1)


@pytest.mark.parametrize("n, refused", [(1, "INSERT"), (3, "SELECT")])
@pytest.mark.parametrize("way", BULK)
def test_a_bulk_run_in_the_callers_unit_after_a_promotion_lands_every_row_in_the_column(
    tmp_path, way, n, refused
):
    """``b`` promoted ``status`` after ``a`` learned ``t``: the run's guarded statement (its
    one row, or the check before several) is refused, ``a`` learns ``t`` again and builds the
    run again; the caller's earlier write is kept."""
    a, b, path = _two(tmp_path)
    try:
        a.find_document("t", 1)
        b.ensure_table("t", promoted={"status": "TEXT"})
        with query_logger.capture() as sent:
            with a.transaction():
                a.insert_document("beside", {"name": "the caller's own row"})
                ids = BULK[way](a, [{"name": f"r{i}", "status": "v"} for i in range(n)])
        beside = a.find_document("beside", 1)
    finally:
        a.close(), b.close()
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


@pytest.mark.parametrize("way", BULK)
def test_a_keyboard_interrupt_at_the_check_of_a_bulk_run_is_not_caught_and_writes_nothing(
    tmp_path, way
):
    a, _, path = _two(tmp_path)

    def interrupt(entry):
        if entry.sql.startswith("SELECT CASE"):
            raise KeyboardInterrupt

    a.find_document("t", 1)
    query_logger.add_callback(interrupt)
    try:
        with query_logger.capture() as sent:
            with pytest.raises(KeyboardInterrupt):
                BULK[way](a, [{"name": f"r{i}"} for i in range(2)])
        in_transaction = a.adapter.in_transaction
    finally:
        query_logger.remove_callback(interrupt)
        a.close()
    assert ([q.sql.split()[0] for q in sent], in_transaction) == (["CREATE", "SELECT"], False)
    assert sqlite3.connect(path).execute("SELECT count(*) FROM t").fetchone() == (1,)


@pytest.mark.parametrize("way", BULK)
def test_a_bulk_run_on_a_locked_database_is_raised_before_its_check(tmp_path, way):
    """The run's transaction takes the write lock at its begin: a lock held by another
    connection is raised there once, and neither the check nor a row is sent."""
    a, b, path = _two(tmp_path, timeout_ms=50)
    try:
        b.adapter.execute("BEGIN IMMEDIATE;")
        with query_logger.capture() as sent:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                BULK[way](a, [{"name": "r"}])
        b.adapter.execute("ROLLBACK;")
    finally:
        a.close(), b.close()
    assert [q.sql.split()[0] for q in sent] == ["CREATE"]


class SafePromotingRow(SQLerSafeModel):
    name: str
    status: str | None = None

    __tablename__ = "safe_promoting"
    __promoted__ = {"status": "TEXT"}


def test_a_safe_class_with_a_promoted_field_saved_at_a_stale_version_writes_nothing(tmp_path):
    path = str(tmp_path / "stale.db")
    a = SQLerDB.on_disk(path)
    try:
        held = SafePromotingRow(name="x", status="s").save(db=a)
        SafePromotingRow.from_id(held._id, db=a).save(db=a)  # the row moves to version 1
        held.status = "lost"
        with pytest.raises(StaleVersionError):
            held.save(db=a)
    finally:
        a.close()
    assert sqlite3.connect(path).execute(
        "SELECT data, status, _version FROM safe_promoting"
    ).fetchall() == [('{"name":"x"}', "s", 1)]


def test_a_bulk_write_of_documents_holding_id_and_version_keys_keeps_them_in_data(tmp_path):
    """``_id`` and ``_version`` are never promoted columns: a promoted bulk insert keeps such
    keys of its documents in ``data``, as it always did, and gives each row a new ``_id``."""
    a, _, path = _two(tmp_path)
    try:
        a.ensure_table("t", promoted={"status": "TEXT"})
        ids = a.insert_many_promoted(
            "t", [{"name": "y", "_id": 9, "_version": 3, "status": "s"}], ["status"]
        )
    finally:
        a.close()
    assert sqlite3.connect(path).execute(
        "SELECT _id, data, status FROM t WHERE _id = ?", ids
    ).fetchall() == [(2, '{"name":"y","_id":9,"_version":3}', "s")]


# ---- the sender decides by the names its statement was built from ------------------------------


def test_a_statement_refused_after_another_statement_learned_the_table_is_built_again(tmp_path):
    """Another statement learned ``t`` between the refusal and the sender's look (here the log's
    callback, as a second thread can): the names the refused statement was built from are not
    the table's names, so it is built again; the memory being new already does not hide that."""
    a, b, path = _two(tmp_path)
    a.find_document("t", 1)
    b.ensure_table("t", promoted={"status": "TEXT"})

    def learn_first(entry):
        if entry.error:
            a._forget("t")
            a._columns_of("t")

    query_logger.add_callback(learn_first)
    try:
        with query_logger.capture() as sent:
            _id = a.insert_document("t", {"name": "y", "status": "v"})
    finally:
        query_logger.remove_callback(learn_first)
        a.close(), b.close()
    assert [q.sql.split()[0] for q in sent if q.error] == ["INSERT"]
    assert _stored(path, _id, "status") == ({"name": "y"}, "v")


def _searchable(tmp_path):
    """``a`` with ``t`` learned and a search index of ``name`` holding row 1; then
    ``b`` promotes ``name``, and row 1 is changed by ``a``'s raw statement, committed."""

    class Named(SQLerModel):
        name: str
        __tablename__ = "t"

    a, b, path = _two(tmp_path)
    index = FTSIndex(Named, ["name"], index_name="t_search", db=a)
    index.create(a)
    index.rebuild(a)
    b.ensure_table("t", promoted={"name": "TEXT"})
    b.close()
    return a, index, path


def test_a_rebuild_after_another_object_changed_the_table_leaves_a_row_per_document(tmp_path):
    a, index, path = _searchable(tmp_path)
    try:
        a.adapter.execute("INSERT INTO t (data, name) VALUES ('{}', 'two');")
        a.adapter.auto_commit()
        assert index.rebuild(a) == 2
    finally:
        a.close()
    assert sqlite3.connect(path).execute("SELECT rowid, name FROM t_search").fetchall() == [
        (1, "x"),
        (2, "two"),
    ]


@pytest.mark.parametrize("way", ["insert_document", "upsert_document"])
def test_the_callers_raw_statement_before_a_refused_write_is_kept_and_committed_with_it(
    tmp_path, way
):
    """Outside a transaction the caller's own ``INSERT`` is pending when ``a``'s write is
    refused for a table that moved: nothing is ended, the write is built again, and its
    commit commits the caller's row too."""
    a, b, path = _two(tmp_path)
    a.find_document("t", 1)
    a.adapter.execute("CREATE TABLE mine (v TEXT);")
    a.adapter.auto_commit()
    b.ensure_table("t", promoted={"status": "TEXT"})
    try:
        a.adapter.execute("INSERT INTO mine (v) VALUES ('the caller''s');")
        if way == "insert_document":
            _id = a.insert_document("t", {"name": "y", "status": "v"})
        else:
            _id = a.upsert_document("t", 1, {"name": "y", "status": "v"})
    finally:
        a.close(), b.close()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT v FROM mine").fetchall() == [("the caller's",)]
    finally:
        conn.close()
    assert _stored(path, _id, "status") == ({"name": "y"}, "v")


def test_the_callers_own_mistake_after_a_raw_statement_ends_the_unit_and_is_raised_as_it_is(
    tmp_path,
):
    from sqler.query import SQLerExpression

    a, b, path = _two(tmp_path)
    a.find_document("t", 1)
    try:
        a.adapter.execute('INSERT INTO t (data) VALUES (\'{"name": "pending"}\');')
        with pytest.raises(sqlite3.OperationalError, match="^no such column: nocolumn$"):
            SceneRow.using(a).filter(SQLerExpression("nocolumn = 1", [])).all()
        a.adapter.commit()
    finally:
        a.close(), b.close()
    assert sqlite3.connect(path).execute("SELECT count(*) FROM t").fetchone() == (1,)


def test_the_callers_own_mistake_in_a_unit_that_is_not_the_objects_is_sent_once_and_changes_no_memory(
    tmp_path,
):
    from sqler.query import SQLerExpression

    a, b, path = _two(tmp_path)
    try:
        a.find_document("t", 1)
        before = a._known("t")
        with query_logger.capture() as sent:
            with pytest.raises(sqlite3.OperationalError, match="^no such column: nocolumn$"):
                with a.adapter:
                    SceneRow.using(a).filter(SQLerExpression("nocolumn = 1", [])).all()
        after = a._known("t")
    finally:
        a.close(), b.close()
    assert [q.sql.split()[0] for q in sent if not q.sql.startswith(("BEGIN", "ROLLBACK"))] == [
        "SELECT",
        "PRAGMA",
    ]
    assert after == before


def test_the_callers_own_mistake_in_another_threads_unit_on_one_memory_connection_changes_no_memory():
    import threading

    from sqler.query import SQLerExpression

    a = SQLerDB.in_memory()
    try:
        a.insert_document("t", {"name": "x"})
        a.find_document("t", 1)
        before = a._known("t")
        raised = []

        def other_thread():
            with query_logger.capture() as sent:
                try:
                    with a.transaction():
                        SceneRow.using(a).filter(SQLerExpression("nocolumn = 1", [])).all()
                except sqlite3.OperationalError as exc:
                    raised.append((str(exc), [q.sql.split()[0] for q in sent]))

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(10)
        assert (raised, a._known("t"), a.find_document("t", 1)) == (
            [("no such column: nocolumn", ["SELECT", "PRAGMA"])],
            before,
            {"name": "x", "_id": 1},
        )
    finally:
        a.close()


@pytest.mark.parametrize("unit", [False, True], ids=["outside a transaction", "inside one"])
def test_a_by_id_read_of_a_missing_table_ends_nothing_and_makes_nothing(tmp_path, unit):
    a, b, path = _two(tmp_path)
    try:
        if unit:
            with a.transaction():
                a.insert_document("t", {"name": "in the unit"})
                got = (a.find_document("absent", 1), a.find_documents("absent", [1]))
                a.insert_document("t", {"name": "after the read"})
        else:
            a.adapter.execute('INSERT INTO t (data) VALUES (\'{"name": "pending"}\');')
            got = (a.find_document("absent", 1), a.find_documents("absent", [1]))
            a.adapter.commit()
    finally:
        a.close(), b.close()
    conn = sqlite3.connect(path)
    try:
        assert (got, conn.execute("SELECT count(*) FROM t").fetchone()[0]) == (
            (None, []),
            3 if unit else 2,
        )
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'absent'").fetchall() == []
    finally:
        conn.close()


def test_a_failure_while_the_rows_are_fetched_is_raised_as_it_is(tmp_path):
    """Row 2's value makes ``abs`` overflow as SQLite steps to it, after the statement was
    answered: the error is SQLite's, raised once, and the statement is not sent again."""
    from sqler.query import SQLerExpression

    a, b, path = _two(tmp_path)
    try:
        a.adapter.execute(
            "INSERT INTO t (data) VALUES (json_object('name', 'y', 'n', -9223372036854775808));"
        )
        a.adapter.auto_commit()
        with query_logger.capture() as sent:
            with pytest.raises(sqlite3.OperationalError, match="^integer overflow$"):
                list(
                    SceneRow.using(a)
                    .filter(SQLerExpression("abs(coalesce(json_extract(data, '$.n'), 0)) >= 0", []))
                    .iter()
                )
    finally:
        a.close(), b.close()
    assert [q.sql.split()[0] for q in sent] == ["SELECT"]


def test_a_keyboard_interrupt_in_a_statement_sent_again_is_not_caught(tmp_path):
    a, b, path = _two(tmp_path)
    a.find_document("t", 1)
    b.ensure_table("t", promoted={"status": "TEXT"})
    inserts = []

    def interrupt(entry):
        if entry.sql.startswith("INSERT"):
            inserts.append(entry.error)
            if len(inserts) == 2:
                raise KeyboardInterrupt

    query_logger.add_callback(interrupt)
    try:
        with query_logger.capture(), pytest.raises(KeyboardInterrupt):
            a.insert_document("t", {"name": "y", "status": "v"})
    finally:
        query_logger.remove_callback(interrupt)
        a.close(), b.close()
    assert len(inserts) == 2 and inserts[0].startswith("sub-select returns")
    assert sqlite3.connect(path).execute("SELECT count(*) FROM t").fetchone() == (1,)


def test_a_bulk_upsert_that_fails_leaves_the_callers_documents_as_they_were_passed(tmp_path):
    a, b, path = _two(tmp_path)
    docs = [{"name": "new"}, {"name": "bad", "_id": "not a number"}]
    try:
        with pytest.raises(ValueError, match="invalid literal for int"):
            a.bulk_upsert("t", docs)
        failed = [dict(doc) for doc in docs]
        ids = a.bulk_upsert("t", [docs[0]])
    finally:
        a.close(), b.close()
    assert failed == [{"name": "new"}, {"name": "bad", "_id": "not a number"}]
    assert (ids, docs[0]) == ([2], {"name": "new", "_id": 2})
    assert sqlite3.connect(path).execute("SELECT _id, data FROM t").fetchall() == [
        (1, '{"name":"x"}'),
        (2, '{"name":"new"}'),
    ]


def test_a_document_passed_twice_to_one_bulk_upsert_is_two_rows_and_holds_the_id_of_the_last():
    db = SQLerDB.in_memory(shared=False)
    twice = {"name": "a"}
    try:
        ids = db.bulk_upsert("t", [twice, {"name": "b"}, twice])
        stored = [
            tuple(row)
            for row in db.adapter.execute("SELECT _id, data FROM t ORDER BY _id").fetchall()
        ]
    finally:
        db.close()
    assert (ids, twice) == ([1, 2, 3], {"name": "a", "_id": 3})
    assert stored == [(1, '{"name":"a"}'), (2, '{"name":"b"}'), (3, '{"name":"a"}')]
