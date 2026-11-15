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
`_version INTEGER NOT NULL DEFAULT 0`. A versioned insert writes 0; each
versioned update adds 1. A safe model's table has it, and so do the tables
first written by `upsert_with_version`, `insert_many_with_version` and
`insert_many_with_version_promoted` (those writes add the column).

A safe model also writes the version as a `_version` key inside `data`
(`{"n":2,"_version":1}`), and it agrees with the column after each save. The
`*_with_version` writes put nothing in `data`; the column alone holds the version.

## References

A saved model stored in a field of another model is written inside `data` as
`{"_table": "<table>", "_id": <id>}`: the referred table's name and the row's `_id`.

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
