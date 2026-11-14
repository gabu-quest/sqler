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
