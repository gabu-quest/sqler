# How sqler stores data on disk

This is for code that reads or writes a sqler database with plain SQL (another
package, a report, a shell). Every statement here is checked by
`tests/test_storage_layout.py`, which opens a database written by sqler with the
`sqlite3` module alone.

## Document tables

A table made by sqler (on the first insert, query or `ensure_table`; a table made by a query has no rows) has two columns:

| Column | Definition | Holds |
|--------|------------|-------|
| `_id` | `_id INTEGER PRIMARY KEY AUTOINCREMENT` | the document's id |
| `data` | `data JSON NOT NULL` | the document as JSON text |

- `data` is written through SQLite's `json()`, so it is stored minified
  (`{"a":1}`). The id is not inside `data`; it is the `_id` column.
- Because of `AUTOINCREMENT`, SQLite keeps the last id per table in
  `sqlite_sequence`.
- Read a field with `json_extract(data, '$.field')`.

## Promoted columns

`ensure_table(table, promoted={...})` adds real columns after `data`, in the
order given, with the definition given (for example
`status TEXT NOT NULL DEFAULT 'pending'`). A promoted field's value is stored in
its column and not in `data`.

Every column beside `data` but `_version` whose name sqler takes for a promoted column
(a column added with plain SQL too; a generated column is
not one) is such a column, and every writer of sqler keeps the rule. A write puts each key
the document holds that names such a column, in the same case, into the column in its
stored form, and every other key into `data`. A column whose key the document does not
hold is not written: an update leaves its value, and an insert leaves it out, so SQLite
gives it its `DEFAULT`, or NULL, or raises `IntegrityError` for a `NOT NULL` column without
one. A key held with the value `None` writes NULL. So a write never changes the value of a
column the document does not name. This holds for the database object's document writers
(`insert_document`, `upsert_document`, `insert_many`, `insert_many_with_version`,
`bulk_upsert`, `upsert_with_version`, their `*_promoted` forms, and a save of some fields),
for the saves of every model class, also one that has no field for the column or promotes
other fields, and for the write of a row whose reference a delete sets to null. A class's
`__promoted__` (a `*_promoted` writer's list) names columns the table must have: a write of
one the table lacks is refused by SQLite, not put into `data`. One exception: `bulk_upsert` writes an existing row as an
insert that becomes an update, and SQLite checks `NOT NULL` first, so a `NOT NULL` column
without a `DEFAULT` refuses such a row whose document lacks its key. A value a column
refuses raises SQLite's `IntegrityError` and nothing of the call is written. A key in
another case than its column's name (`Status` beside the column `status`) is a key of
`data`.

A database object learns a table's columns once, at its first ensure, read or write of
the table, and remembers them as `SELECT *` gives them. Each statement it builds from what
it remembers carries a guard, `CASE WHEN 1 THEN 1 ELSE (SELECT (<the names>) IN (SELECT *
FROM <table>) FROM <table>) END` (in an `INSERT`, on its first value; a bulk write whose
first statement has several rows, and the triggers of a search index, send it alone, as a
`SELECT`, first in their transaction, and the statements after it there carry none), whose
`ELSE` never runs: SQLite checks its names when it prepares the statement, and again after any change of
the schema by any connection, so it refuses the statement once the table has other columns
(another database object or process promoted a column, renamed or dropped one, added
`_version`, dropped or made the table). Only then does the object learn the table again and
build the statement again, so each key lands in its column, with no error: a refused statement
is built again when the names the table has now are not the names it was built from, also
when another thread or task of the object learned them first; nothing sent before it is
undone. A refusal that is not of those names (the caller's own mistake) ends what any failed
statement ends and is raised as it is; a read of a table that is not there ends nothing and
reads as no row. SQLite charges about 0.1 µs for the guard on a statement it has prepared,
and 12 to 20 µs when it prepares the text (its first use on a connection, and once after
each change of the schema). In Python, on a statement SQLite does not refuse, a by-id read
costs about 0.2 µs more than a sqler without the guard, a single write under 1 µs, and a
query of a model's rows nothing measurable. A bulk write of one row costs about 3 µs more;
one of several rows sends the check as a statement of its own (a guard in any row of a
many-row `INSERT` takes it off SQLite's fast path) and costs about 9 to 12 µs more a call,
`bulk_upsert` about 5 µs more than that; from a thousand rows on a bulk write is within 2%. `sql`, `debug()`, `explain()` and
`explain_query_plan()` show a query without it; `query_logger` logs what is sent, with it.
(Inside a transaction opened on the adapter itself nothing is remembered: each write reads
the columns again.)

## The version column

A versioned table (`ensure_table(..., versioned=True)`, safe models, the
`*_with_version` writes) has one more column:
`_version INTEGER NOT NULL DEFAULT 0`. A safe model's table has it, and so do the
tables first written by `upsert_with_version`, `insert_many_with_version` and
`insert_many_with_version_promoted`, by a safe model's save of some fields, and by a safe
model's queryset `update()`, `update_one()` and `update_n()` (those writes add the column).
A plain write adds no column, and neither does a read: a table made by plain writes gets the
column at a safe model's first write to it. Until then nothing counts its writes, so a safe
instance read before a plain write saves over that write without an error.
`db.ensure_table(table, versioned=True)` before the first read gives the table the column.

The column is the row's version, and the only place it is kept. Every statement sqler
sends that writes a row of a table with the column moves it, whichever class or method
sends it: an insert writes 0, each update adds 1 (an update to the same values too; the
version counts writes), each row of a set-based update once. A query built from the adapter
alone (`SQLerQuery(table, adapter)`) has no database object that knows the table, so each of
its updates first reads the table's columns (one statement). A statement that only moves a
promoted value between its column and the document leaves the version as it is, unless it
changes what a read of the row gives: `move_promoted(..., keep="document")` moves the version
of each row whose document held a value other than its column's; `keep="column"` and the
promotion of a field change no read. A versioned save is written only while the column is at
the version its instance read. `data` holds no copy: a `_version` key in a document written
to the table is dropped, and every read through a database object or a model gives
`_version` from the column. On a table without the column, a `_version` key is a field like
any other.

A document last written by sqler 1.2026.4.4 or earlier may still hold
a `_version` key inside `data`, equal to the column when it was written. A database object
and a model do not read it; a query built from the adapter alone gives each document as it
is stored, that key included. The next write of the whole row drops it, a write of some
fields leaves it. Such a file is
used as it is, with no step and no change of its schema.

sqler 1.2026.4.4 and earlier cannot share a file with 1.2026.11.1 and later. The older one
reads the version from the column too, but it checks a save against the key in `data` as well
(`_version = ? AND COALESCE(json_extract(data, '$._version'), ?) = ?`), and its own plain
writes do not move the column. So, with both on one file:

- The older version fails with an error: a row whose key the newer one left at an older
  value (a write of some fields of a row the older version saved) is refused by every
  save of the older version with `StaleVersionError`.
- A write is lost with no error: a plain write the older version makes (a plain class's
  save, `upsert_document()`, a plain query's update) leaves the column as it was, so a save
  by the newer version of an instance read before it goes through and overwrites it.

## References

A saved model stored in a field of another model is written inside `data` as
`{"_table": "<table>", "_id": <id>}`: the referred table's name and the row's `_id`. A
reference is exactly these two keys, `_table` a string and `_id` an integer (not `true` or
`false`); any other dict, one with these two keys and more among them, is data: it is
stored and read whole, and never followed (`sqler.utils.is_ref_dict` tells them apart).

## Indexes made by `create_index`

`create_index(table, field)` without a `name` makes an index named
`idx_<table>_<field>`, with each `.` in the field replaced by `_`
(`nested.b` gives `idx_docs_nested_b`). It indexes
`json_extract(data, '$.<field>')`; a field that starts with `_` (such as `_id`) is
indexed as the column itself. `unique=True` makes a `UNIQUE` index.

## The migrations table

`MigrationRunner` records applied migrations in `_sqler_migrations`:

| Column | Definition |
|--------|------------|
| `version` | `INTEGER PRIMARY KEY` |
| `name` | `TEXT NOT NULL` |
| `applied_at` | `TEXT NOT NULL` |
| `duration_ms` | `REAL NOT NULL` |
| `checksum` | `TEXT` |

## The record of settled promoted columns

sqler keeps a table of its own, `_sqler_promoted`, beside the migrations table:
`CREATE TABLE IF NOT EXISTS _sqler_promoted (tbl TEXT NOT NULL COLLATE NOCASE, col TEXT NOT NULL COLLATE NOCASE, PRIMARY KEY (tbl, col)) WITHOUT ROWID`.
A row `(tbl, col)` says that the promoted column `col` of table `tbl` holds its values: no
document of that table holds a key named `col`.

- sqler writes a row when it makes a table with its promoted columns, when it adds a
  promoted column to a table (the values move out of the documents in the same
  transaction), and when it finds an existing column with no such key, or settles it with
  `move_promoted`. A table sqler makes with promoted columns replaces what was recorded
  under its name.
- A column beside `data` that is not recorded is checked once by each database object, at
  its first ensure, read or document write of the table: a document that still holds the key
  makes that call raise `SchemaError`, because a read sets the column over the document.
  When the database takes no write at that moment (opened read-only, or another
  connection holds the write lock past the busy timeout) a table found settled is used as
  settled and its row is not written; each later ensure of that database object checks
  again and tries to write it, waiting up to the busy timeout.
- Code that writes with plain SQL keeps the rule: a key put into `data` under a recorded
  column's name is not read (the column wins).
- sqler drops and renames no table. A table dropped and made again with plain SQL keeps
  what is recorded under its name and counts as settled: a key such a table holds in a
  document under a recorded column's name is not read, with no error. Delete its rows
  from `_sqler_promoted` to have it checked again.
- `_sqler_promoted` and `_sqler_migrations` are not names for your own tables: sqler
  refuses them as table names and leaves them out of its table counts.
