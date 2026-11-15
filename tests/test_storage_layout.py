"""Each statement of docs/STORAGE.md, shown on a database written by sqler and read with ``sqlite3`` alone.

Every test names the section of the document it proves, and checks that the
document still says what the test shows.
"""

import json
import sqlite3
from pathlib import Path

import pytest
from sqler import SQLerDB, SQLerModel, SQLerSafeModel
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
        "jobs", None, {"status": "queued", "priority": 5, "payload": "p"}, None, ["status", "priority"]
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
    assert written.execute("SELECT name, seq FROM sqlite_sequence WHERE name='docs'").fetchone() == ("docs", 2)


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
    assert written.execute("SELECT json_extract(data, '$.name') FROM authors WHERE _id = 1").fetchone() == ("Ann",)


def test_index_names(written):
    """Section "Indexes made by create_index"."""
    rows = written.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='docs' ORDER BY name"
    ).fetchall()
    assert [r[0] for r in rows] == ["idx_docs__id", "idx_docs_a", "idx_docs_nested_b"]
    by_name = dict(rows)
    assert by_name["idx_docs_a"].split() == "CREATE UNIQUE INDEX idx_docs_a ON docs (json_extract(data, '$.a'))".split()
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
    assert written.execute("SELECT version, name FROM _sqler_migrations").fetchall() == [(1, "first")]


def test_table_made_by_a_query(written):
    """Section "Document tables": a first query makes the table."""
    assert _cols(written, "notes") == [("_id", "INTEGER", 0, None, 1), ("data", "JSON", 1, None, 0)]
    assert written.execute("SELECT COUNT(*) FROM notes").fetchone() == (0,)


def test_safe_model_table_has_version_and_data_key(written):
    """Section "The version column": safe models, and the `_version` key in data."""
    assert [c[0] for c in _cols(written, "tasks")] == ["_id", "data", "_version"]
    assert _cols(written, "tasks")[2] == ("_version", "INTEGER", 1, "0", 0)
    ((data, version),) = written.execute("SELECT data, _version FROM tasks").fetchall()
    # saved once, then updated once: the column and the key in data agree
    assert version == 1
    assert json.loads(data) == {"n": 2, "_version": 1}


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
    assert "tbl TEXT NOT NULL COLLATE NOCASE, col TEXT NOT NULL COLLATE NOCASE, PRIMARY KEY (tbl, col)) WITHOUT ROWID" in DOC
    assert _cols(written, "_sqler_promoted") == [("tbl", "TEXT", 1, None, 1), ("col", "TEXT", 1, None, 2)]
    (sql,) = written.execute("SELECT sql FROM sqlite_master WHERE name = '_sqler_promoted'").fetchone()
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
