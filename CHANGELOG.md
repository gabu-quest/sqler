# Changelog

All notable changes to sqler are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/).

---

## [Unreleased]

### Security

- **A reference stored in a document is resolved only when it is well formed.** `{"_table": ..., "_id": ...}` found in a document was used to build a `SELECT` with the table name taken from the stored data. It is now resolved only when `_table` is a valid table name and `_id` is an integer that fits SQLite's 64-bit range; anything else is left as it is. A reference whose `_id` is a string or a float is no longer converted to an integer, and an `_id` too large for SQLite no longer raises `OverflowError` when the document is read.
- **Field and key names used in queries are escaped.** A name with a quote could end the SQL string that held its JSON path, in `F("...")` comparisons and in `any()` on nested arrays. Names are now carried as data. A name with a backslash, a NUL, or whitespace other than single spaces raises `InvalidFieldNameError`.
- **Names ending in a newline are rejected.** `validate_table_name`, `validate_field_name` and `validate_identifier` accepted `"users\n"`.
- **Model field paths, the search index and the integrity helpers check what they put into SQL.** A key in a model field path (`any()` and filters on related models) is carried as data; the key `a'` no longer matches the key `a`, and an empty key raises `InvalidFieldNameError`. `FTSIndex` checks its fields, `tokenizer` and `index_table` when they are assigned as well as in the constructor, checks the model's table name, and `snippet()` raises `TypeError` for a `max_tokens` that is not an integer. `find_referrers`, `validate_references`, `set_null_referrers`, `cascade_delete` and their async versions raise `InvalidTableNameError` for a table name that is not a plain identifier, before any SQL runs.
- **The promoted write methods check their names.** `upsert_document_promoted`, `upsert_with_version_promoted` and `insert_many_promoted`, sync and async, raise `InvalidTableNameError` or `InvalidIdentifierError` for a bad table or promoted field name before any SQL runs.

### Fixed

- **`created_at` and `updated_at` are equal after the first save.** Since 1.2026.4.3 the first save set `updated_at` one microsecond after `created_at`. Every later save still gives a strictly later `updated_at`, now also when the clock has not advanced or has gone backwards, and when two saves follow each other within one clock tick. Applies to `TimestampMixin`, `AuditMixin` and `AsyncAuditMixin`. A stored `updated_at` that lies in the future is not pulled back: the next save is one microsecond after it.
- **Async adapter: a task that ends holding a pooled connection no longer keeps it.** A task cancelled between a statement and its commit (an outer `cancel()`, a `wait_for` timeout) used to end with its connection out of the pool and its write transaction open, so every other writer waited out the busy timeout and failed with "database is locked". When the last task using a connection ends without releasing it, the connection is now rolled back and returned to the pool, once. `close()` waits for any such rollback first.
- **Async adapter: a task created while its parent held a connection no longer returns that connection twice.** The child inherits the pin; the connection now goes back to the pool when the last task using it releases it or ends, and a child whose inherited pin was already returned takes a fresh connection instead of sharing one that is back in the pool.
- **Async adapter: a failed rollback no longer shrinks the pool.** If rolling back a reclaimed connection fails while the adapter is open, a warning is logged, that connection is closed and a fresh one is put in the pool.
- **Async adapter: `adapter.connection` is `None` for a task whose inherited connection was already returned.**
- **Async adapter: a statement that fails outside a transaction no longer keeps its pooled connection.** A syntax error, a constraint violation, a wrong parameter count, a failed commit or a failed `BEGIN` used to leave the task holding the connection, with any write it had started, until the task ended. The connection is now rolled back and returned before the error reaches the caller; this also rolls back earlier statements of the same task that were not yet committed on it. Inside a transaction the connection stays until the transaction ends, and ending it returns the connection whether the commit or rollback succeeds.
- **Async adapter: a cancelled statement returns its connection by itself.** A `wait_for` timeout or `task.cancel()` reaches the caller at once, also while the statement waits on a lock held by another connection. The connection goes back to the pool once SQLite has finished with it, and a cancelled write is rolled back. A commit or `executescript` that SQLite had already started when it was cancelled still completes.
- **Async adapter: `executemany` inside a transaction no longer commits it**, so a rollback removes its rows. Outside a transaction `executemany` and `executescript` commit and return the connection.

- **`async_export_jsonl` and `async_import_jsonl` run.** They imported `aiofiles`, which sqler does not depend on, and the import called `afrom_id` and `asave`, which no model has. They now need no extra package, do their file work off the event loop thread, work with all four async model bases, and give the same file and the same result as `export_jsonl` and `import_jsonl`.
- **JSONL import ignores a `_version` key in the file**, sync and async. An export of a safe model carries it; a lite safe model could not import its own export, and an update wrote the file's version over the stored one.
- **Async migrations are atomic.** `AsyncMigrationRunner` runs each migration's `up()` (or `down()`) and its version row in one transaction, as the sync runner does. A migration that fails or is cancelled leaves the schema, the data and the recorded versions as they were.
- **The async migration runner, `async_health_check`, `async_get_stats`, `async_checkpoint` and `async_vacuum` return their pooled connection** when they finish or raise. Inside your own transaction they keep using its connection and do not end it.
- **`async_health_check` always returns a `HealthStatus`**, also when returning the connection fails (the status is then unhealthy and names the error).
- **A migration runner first used inside a transaction that is rolled back** creates its version table again on the next call.

- **Sync adapter: a statement that fails outside a transaction no longer keeps the lock.** Python's `sqlite3` opens a transaction before a write and left it open when the write failed, so the thread held the write lock until its next commit and other threads got "database is locked". The failed statement, or a failed commit, is now rolled back before the error reaches the caller; this also rolls back that thread's earlier uncommitted statements, a raw `BEGIN` included. Inside `db.transaction()` nothing changes.
- **Sync adapter: `in_transaction` and `auto_commit()` are per thread.** A transaction open on one thread no longer stops other threads from committing. A plain `:memory:` database has one connection for all threads and still has one transaction.
- **`upsert_with_version` and `upsert_with_version_promoted` end their `BEGIN IMMEDIATE`** when the update raises.
- **A safe save gives up on a locked database in bounded time.** A save that found the database locked was retried like a version conflict, up to 128 times with a full busy timeout each, and could wait for minutes. Lock waits now count against `RebaseConfig.lock_timeout` (5 seconds by default); past it the save raises `LockTimeoutError`. On a file database the longest wait is that limit plus one busy timeout. A lock met while the save re-reads the row after a conflict is handled the same way instead of leaving the save as `sqlite3.OperationalError`. Applies to all four safe model bases.
- **Sync adapter: `executemany` inside a transaction no longer commits it**, so a rollback removes its rows.

- **A model saved with `save(db=db)` and never bound with `set_db()` can be referenced and searched.** A model holding it stored a reference with no table, or raised "model must be bound"; its search index read the table `article` while the rows were in `articles`. Such a model is now stored as a reference, read back through `using(db)` with the related model in place, and found by `FTSIndex(Model, fields, db=db)`.
- **`first_or_create` under a unique index no longer fails when two callers race.** The caller whose insert is refused gets the other's row with `created` false. A refusal from a unique index on a field that is not in the lookup still raises `UniqueConstraintError`.
- **`save(db=db)` on a model with promoted columns creates its table** on a fresh database instead of raising "no such table" (sync bases).

- **A read of a missing table keeps your uncommitted work.** Both adapters rolled back the connection after any failed statement, so a raw statement left uncommitted outside a transaction was dropped when the same thread or task then read a table that does not exist. A read that fails with "no such table" now rolls back nothing; a failing write still does.
- **A table created in a transaction that rolled back is created again by the next write.** The database object kept remembering the promoted or versioned table, and the next write failed with "no such table". What it learns about a table inside a transaction now ends with that transaction: a commit keeps it, a rollback drops it, and neither sends a statement. This also holds for a transaction opened on the adapter directly (`db.adapter.begin_transaction()`), outside or inside `with db:`; nothing is remembered there, so the next write looks the table up again.
- **A safe model first saved inside a transaction that rolled back can be saved afterwards.** `SQLerSafeModel.save()` and `SQLerLiteSafeModel.save()` with promoted columns failed with "no column named ...".
- **A reference to a lite safe row loads after a rolled-back column promotion.** The loader selected the promoted column the rollback had removed.
- **Async adapter: a fetch that fails or is cancelled returns its connection.** `fetchone`, `fetchall`, `fetchmany` and `async for` on the cursor from `execute()`, outside a transaction. `execute()` now returns a wrapper around the `aiosqlite` cursor with the same methods and attributes; `isinstance(cursor, aiosqlite.Cursor)` is false.
- **Async adapter: a statement cancelled inside a transaction no longer holds the caller.** The cancellation reaches the caller at once instead of after the statement has finished in its thread; the connection returns to the pool, rolled back, when SQLite is done with it.
- **Ending a transaction by rollback raises nothing of its own.** The caller sees its own exception, also when the adapter was closed inside the block or the pool's only connection is still busy.
- **A failed `backup()` or `async_backup()` leaves the destination as it was.** The copy is made in a temporary file beside the destination and moved into place when complete; before, a failure left an empty or half-written file. A destination that is a symlink is written through, and an existing destination keeps its mode.
- **`metrics.disable()` puts the query logger back as it was before `enable()`.**
- **`metrics.prometheus_export()` gives valid output.** Label values and table names are escaped, and the duration histogram is declared once under `sqler_query_duration_ms` with `_bucket`, `_sum` and `_count` samples.
- **Two migration runners started together no longer report a failure.** The slower runner skips a version the other already recorded (`migrate`) or already removed (`rollback`) and reports success with nothing applied; each `up()` and `down()` runs once. Sync and async.
- **CSV export and import return every string unchanged.** The empty string, strings starting with `=`, `+`, `-`, `@`, tab or CR, and cells with line breaks round-trip. Export puts one apostrophe in front of such a string and import removes it; a cell such as `'90s` written by another program is read as written.
- **`import_csv(batch_size=N)` works.** An import that fails at a row keeps the full batches before it and nothing of the failed batch.
- **`AuditLogMixin` and `AsyncAuditLogMixin` write a change and its audit row together.** Outside a transaction a save or delete whose audit row is refused leaves no saved change, and a refused delete leaves no audit row. After a failed save the instance keeps its earlier `_id` and `_version`.
- **A safe model reached through a reference keeps its stored version**, so saving it does not raise `StaleVersionError` (never released; found while batching `from_id`).
- **`from_ids` and `query().all()` no longer raise "no such table" when a referenced table is gone.** The reference reads as `None`, as `from_id` gives.
- **A lite safe model reached through a reference no longer recreates a dropped table.** The reference reads as `None`.
- **`set_db()` on the safe model classes reports its deprecation warning at your own line**, so Python's default warning filters show it.
- **`from_ids()` on a model with promoted columns gives the stored values.** The promoted fields came back at their defaults, and a `save()` of such an instance wrote those defaults over the stored columns. `find_documents`, sync and async, now returns promoted columns as `find_document` does.
- **A referenced row loaded through `from_ids()`, `query().all()` or `first()` carries its promoted columns.**
- **`from_ids()` on the safe model bases holds the stored `_version`.** It held 0, so saving a row that had been updated before raised `StaleVersionError`.
- **A database object that did not create a table reads its promoted columns.** A second process, `Model.using(db)` without `set_db()`, or any `SQLerDB` that never created or ensured the table read every row without them, through `find_document` as well. The columns are now read from the table, once per database object.
- **`resolve(False)` stays in force across chained calls** such as `resolve(False).filter(...)`, and a chained queryset keeps the database it was made from.
- **A very long id list no longer fails with "too many SQL variables"** in `from_ids()` and `find_documents()`.
- **A model loaded through `from_ids()`, `query().all()` or `first()` can be saved when it holds a typed reference.** A field typed as a model (`Child`, `Optional[Child]`, `list[Child]`) held a child without an `_id`, and `save()` raised "Related model must be saved before saving parent". The child is now the model of its row and can be saved itself.
- **Lite models: a typed list loaded through `from_ids()`, `query().all()` or `first()` holds models.** It held dicts, and `save()` replaced the stored references with copies of the rows.
- **A child of a safe model class loaded through `from_ids()`, `query().all()` or `first()` holds the version of its row**, so saving it no longer raises `StaleVersionError`.
- **`SQLerSafeModel.from_id()` and `refresh()` follow typed references**, sync and async. They gave an empty child without an `_id`.

### Changed

- **The PyPI publish workflow runs the linter and the test suite first**, and refuses a tag that does not match the version in `pyproject.toml`.
- **Async adapter: `executescript` inside a transaction raises `QueryError`**, because SQLite commits the open transaction before it runs a script.
- **Async adapter: waiting for a pooled connection times out.** `acquire_timeout` (seconds, default 30, `None` waits forever) on `AsyncSQLiteAdapter`, `on_disk()` and `in_memory()`; a wait that runs out raises `ConnectionPoolExhaustedError`. Tasks waiting when `close()` is called raise `NotConnectedError`.

- **`adapter.executescript()` inside an async migration fails.** The migration now runs in a transaction and the adapter refuses a script there. Use one `adapter.execute()` per statement; the failed `MigrationResult.error` says so.
- **`async_vacuum` inside a transaction raises `QueryError`** before it runs anything; the transaction stays usable.

- **Sync adapter: `executescript` inside a transaction raises `QueryError`**, as the async adapter does.
- **`RebaseConfig.max_retries` counts version conflicts only**, and a safe save sleeps at most 50 ms between two tries.
- **`FTSIndex.fields` returns a copy.** Appending to it no longer changes the index; assign a new list instead.
- **A violated unique index or primary key raises `UniqueConstraintError`**, sync and async, from every write path including a raw `adapter.execute`. It names the table and columns SQLite reported (or the index, for an index on an expression) and has the driver error as its cause. It is still a `sqlite3.IntegrityError`, so `except sqlite3.IntegrityError` keeps working; NOT NULL, CHECK and FOREIGN KEY errors are unchanged.

- **Every route uses one rule for a model's table**: the table bound with `set_db()`, else `__tablename__`, else the plural of the class name. For a class that was never bound, `FTSIndex` now reads `articles` instead of `article`, so its default index name changes from `article_fts` to `articles_fts`.
- **A read creates no table.** `find_document`, `find_documents`, `find_document_with_version` and `query()` of `SQLerDB` and `AsyncSQLerDB`, and through a model `query()`, `using(db)`, `from_id`, `from_ids` and `refresh`, give the empty answer on a table that does not exist (`[]`, `None`, 0, false; a queryset `update()` or `delete()` changes 0 rows) instead of creating the table or raising "no such table". A query built by hand (`SQLerQuery(table=..., adapter=...)`, `AsyncSQLerQuery(...)`) does the same, where it raised "no such table"; `explain()` and `explain_query_plan()` still raise. The exports (`export_csv`, `export_json`, `export_jsonl` and their string, stream and async forms) of a table that does not exist write what they write for an empty table. A read no longer needs the write lock. "no such table" for another table in the query still raises. `find_document_with_version` still adds a missing `_version` column to an existing table.
- **Model classes are registered when they are defined**, so `sqler.registry.tables()` lists classes that were never bound, and the integrity scans look at their tables. A class bound with `set_db()` is never replaced by a class that is only defined.
- **`as_ref()` on an unsaved model** now says "model must be saved"; a saved model no longer has to be bound.
- **Lite models: a reference to a row that no longer exists stays the `{"_table", "_id"}` dict when it is read through a query.** Before, the field held a model built from those two keys alone, with every other field at its default.

- **`from_id()` and `refresh()` read each referenced table with one query** instead of one query per reference (a row with 20 references: 2 queries, was 21), and a cycle of references is cut at the first row seen again, as `from_ids()` cuts it, instead of after about 160 levels.
- **A `{"_table", "_id"}` dict that cannot be followed is left whole.** `from_id()`, `refresh()` and async `query().all()` no longer load references nested inside it.
- **Async `query().all()` no longer walks each row twice** when no document holds a reference.
- **Lite safe models: `is_dirty()` after `from_id()` or `refresh()` does not see a list or dict edited in place**, as on the other ways a lite model is loaded. `save()` writes the edit.
- **One `NoAdapterError`.** The sync and the async query raise `sqler.NoAdapterError`. It is a `QueryError` and still a built-in `ConnectionError`, so `except ConnectionError` keeps working.
- **`F('')` raises `InvalidFieldNameError`.**
- **`FTSIndex` refuses a search field with a dot** (`meta.title`) with `InvalidIdentifierError` in the constructor and the `fields` setter; before, `create()` failed with an SQLite parse error.
- **`import_csv()`'s `batch_size` default is `None`** (one commit per row, as before); it was 100 and ignored. A value below 1 raises `ValueError`.
- **CSV import removes one leading apostrophe from a cell that looks escaped**: apostrophes followed by nothing or by `=`, `+`, `-`, `@`, tab or CR. Such a cell written by hand or by another program is read without that apostrophe.
- **`metrics.enable()` raises `ValueError`** for a custom label named `le`, `table` or `operation`, or for two labels that become the same name once cleaned.
- **A column added to a table by hand shows in documents read by id**, under the column's name, when that name is a valid field name.
- **Lite safe models: `from_ids()` sends one statement** instead of one per id, and leaves out an id given as a string, as `from_ids()` on the other bases does.
- **`SQLerSafeModel.from_id()` and `refresh()` raise `ValidationError` for a typed list that names a deleted row**, as `SQLerModel.from_id()` does. They gave an empty child in its place.
- **A `mode="before"` validator on a typed reference field receives the child model** through `from_ids()`, `query().all()` and `first()`, as it does through `from_id()`. It received the stored dict.
- **Async `query().all()` cuts a cycle of typed references where the other ways do**, one level earlier than before, with fewer statements.

### Added

- **`AsyncSQLiteAdapter.pool_stats()` and `AsyncSQLerDB.pool_stats()`** return `PoolStats`: pool size, free and in-use connections, checkouts, timeouts, average and longest wait.
- **`AsyncMigrationRunner.applied_migrations()`**, and `status()` returns `applied_count` and `applied` as the sync runner does.
- **`RebaseConfig.lock_timeout`** (seconds, default 5), and `LockTimeoutError` can be imported from `sqler`.
- **`SQLerDB.ensure_table()` and `AsyncSQLerDB.ensure_table()`** create a table with promoted columns, CHECK constraints and a version column (`table, *, promoted=None, checks=None, versioned=False`). They can be called again and add a missing version column to an existing table.
- **`AsyncSQLerDB.in_memory()` and `on_disk()` take `acquire_timeout`**, how long to wait for a free connection.
- **`AsyncAdapterABC.pool_stats()`** is part of the adapter interface; an adapter without a pool need not define it.
- **`docs/STORAGE.md`** describes how sqler lays out its tables on disk, for code that reads a database with plain SQL.
- **`Model.using(db, table)`** reads another table for one query without rebinding the class, on all model bases. The name is checked before any SQL. A row read this way saves, deletes and refreshes against the table it came from, and `as_ref()` names it. A copy of such a row made with `dataclasses.replace()` or `msgspec.structs.replace()` does not keep that table.
- **`FTSIndex(Model, fields, db=db)`** takes the database, for classes that were never bound.
- **`from_ids(ids, db=db)`** reads from a given database, on all model bases.

## [1.2026.4.2] - 2026-04-18

### Security

- **`PartialUpdateMixin.save_partial()` / `asave_partial()` — validate field names.** `field_name` is now run through `validate_field_name()` before interpolation into `json_set()`. Defence-in-depth, consistent with every other field-path site.
- **CSV export escapes formula-injection prefixes.** Values starting with `=`, `+`, `-`, `@` get a leading single quote so spreadsheet apps treat them as text, not formulas. Affects all `export_csv*` paths.
- **`set_db()` validates table names on async backends too.** The async Pydantic model and async Lite model now call `validate_table_name()` (parity with the sync fix in 1.2026.4.1).

### Added

- **`SQLerModel.all()` / `SQLerModel.first()`** — convenience classmethods, parity with Lite and Msgspec backends.
- **`SQLerModel.is_dirty()` / `SQLerModel.get_dirty_fields()`** — surface the existing `_snapshot` for Pydantic users, parity with Lite and Msgspec backends. Method (not property) to match the other backends.
- **`bind()` emits its own dedicated `DeprecationWarning`** with `stacklevel=2` so the warning points at the user's call site and names `bind()` (not `set_db()`). Applies to Lite, Msgspec, and async Lite.

### Changed

- **`QuerySet.all()` / `QuerySet.first()` no longer swallow arbitrary exceptions.** Only `RecursionError` from circular reference resolution is tolerated; everything else now re-raises so real bugs surface instead of being silently logged.
- **Bare `assert adapter is not None` in `_batch_resolve` replaced with explicit `RuntimeError`** — so the check survives `python -O`.

### Internal

- **De-duplicated `_pluralize` / `_default_table_name`** into `src/sqler/models/_table_names.py`. Was byte-identical across three model files; now sourced from one place.
- **Added security warnings to `backup()` and `restore()` docstrings** about path-traversal risk when paths come from untrusted input.
- **Added DoS warning to `FTSIndex.search()` docstring** about wildcard queries on large indexes.
- **Tightened `test_timing_active_when_enabled`** softball — was `len(logs) > 0`, now asserts exactly one INSERT log against the items table.

---

## [1.2026.4.1] - 2026-04-17

### Security

- **`_ensure_table_with_promoted` — validate promoted-column definitions.** `col_def` strings (e.g. `TEXT NOT NULL DEFAULT 'pending'`) and CHECK expressions are now validated via new `validate_column_def()` / `validate_check_expression()` helpers. Unvalidated DDL interpolation was a latent injection surface for callers passing definitions from dynamic config.
- **`create_index(where=...)` — validate partial-index WHERE clauses.** The `where` string is now passed through `validate_check_expression()`, which rejects `;`, SQL comment markers (`--`, `/*`, `*/`), and DDL/DML keywords. Legitimate partial indexes (e.g. `json_extract(data,'$.name') IS NOT NULL`) continue to work.
- **`set_db()` — validate table names on Pydantic and Lite backends.** The msgspec backend already validated, but the Pydantic and Lite backends accepted arbitrary table strings. All three backends now mirror the same injection guard.

### Fixed

- **`SQLerModel.refresh()` — update the dirty-tracking snapshot.** The Pydantic `refresh()` updated instance fields but left `_snapshot` at its stale pre-refresh value, causing downstream dirty-tracking consumers to see incorrect state. Now matches the Lite and Msgspec backends.

### Added

- **`validate_column_def()` and `validate_check_expression()`** — reusable predicate-grammar validators in `sqler.utils`.
- **`InvalidColumnDefError`** — new `SchemaError` subclass.

---

## [1.2026.3.4] - 2026-03-04

### Added

- **`SQLerMsgspecModel`** — high-performance model variant using msgspec Structs for 2x faster hydration on bulk reads
- **`queryset.as_dicts()`** — bypass Pydantic hydration entirely, return raw dicts for maximum throughput
- **`update_n(n, **fields)`** — atomic batch updates with `RETURNING` clause
- **`save_many()` / `asave_many()`** — batch INSERT with auto-chunking for fast bulk creation
- **Per-call DB binding** — `model.save(db=other_db)`, `model.delete(db=other_db)` for multi-database workflows
- **`Model.using(db)`** — context manager for temporary DB binding on any model class
- **SQL injection prevention** — field name validation (`validate_field_name`) across all query paths, DDL identifier validation
- **FTS security** — validate index names, field names, and tokenizer parameters; parameterize highlight/snippet tags
- **`execute_sql()` restriction** — limited to SELECT/EXPLAIN/PRAGMA/WITH statements only
- **Async resource safety** — `try/finally` around all cursor operations in `async_db.py`
- **Promoted column rewriting** — `delete()` and aggregate queries now correctly rewrite WHERE clauses for promoted columns
- **Connection pool for async adapter** — `using()` per-query DB binding in async context
- **Memory profiler** — `benchmarks.memprofile` with multi-run stats, setup/run split, regression detection
- **Benchmark v1.2** — complete rewrite with matched PRAGMAs, fixed SQL asymmetries, 4-arm charts, sqlite3 baselines for all 22 scenarios

### Changed

- **`set_db()` soft-deprecated** — emits `DeprecationWarning`; use `Model.using(db)` or pass `db=` to instance methods instead
- **Bulk insert rewritten** — `bulk_upsert()` now uses chunked multi-row INSERT for better throughput
- **FTS `search_ranked()`** — rewritten to single-JOIN query (was subquery + Python sort)
- **Query timing** — guarded behind `logger.enabled` check to eliminate overhead when not logging
- **JSON path queries** — use `json_each(data, path)` instead of `json_each(json_extract())` for cleaner SQL
- **Export functions** — bypass Pydantic hydration for faster CSV/JSON/JSONL export
- **Lite models** — now support `using()` and `set_db()` deprecation warnings

### Fixed

- `F("_id")` and `F("_version")` now rewrite to real SQLite columns instead of JSON paths
- Async cursor leak in `async_export_jsonl`
- Datetime serialization in `_adump_with_relations()` for async models
- Mixin `save()`/`delete()` overrides now forward `db=` parameter correctly
- Connection pool releases connections after read operations
- Benchmark fairness: 18 methodology issues fixed (deserialization asymmetry, connection handling, SQL patterns)

---

## [1.2026.2.1] - 2026-02-01

### Added

- **Benchmark suite v1.1** — 22 scenarios across 5 suites with sqlite3 baselines
- **Lite model tours** — 11 WASM-compatible marimo notebooks for browser-based learning
- **Japanese tours** — all 11 tours translated

---

## [1.2026.1.7] - 2026-01-07

### Added

- **Transaction-aware saves** — `model.save()` now respects explicit transactions; saves inside `with db.transaction():` are rolled back properly on error
- **Soft delete convenience methods** — `SoftDeleteMixin` class methods: `active()`, `only_deleted()`, `with_deleted()`
- **Extended query builder** — new field operations: `between`, `is_null`, `is_not_null`, `startswith`, `endswith`, `glob`, `in_list`
- **NULL-safe comparisons** — `F("field") == None` generates correct `IS NULL` (not `= NULL`)
- **Configurable intent rebasing** — `RebaseConfig`, `PERMISSIVE_REBASE_CONFIG`, `NO_REBASE_CONFIG` for automatic conflict resolution in safe models
- **Auto-calling lifecycle hooks** — `HooksMixin` automatically invokes `before_save`/`after_save` in `save()` and `delete()`
- **Index management** — `list_indexes()`, `index_exists()` for programmatic index introspection
- **Smart table naming** — proper English pluralization: `Category` -> `categories`, `Box` -> `boxes`, `Company` -> `companies`
- **Query caching** — `QueryCache` with TTL, LRU eviction, pattern/table invalidation, and `@cached_query` decorator
- **Data export/import** — CSV, JSON, JSONL formats with sync + async support, streaming, and transform hooks
- **Full-text search** — FTS5-based search via `FTSIndex` and `SearchableMixin`, with ranked results and highlighting
- **Connection pooling** — `PooledSQLerDB` for high-concurrency scenarios with pool stats
- **Schema migrations** — `MigrationRunner` with versioned up/down migrations (sync + async)
- **Metrics collection** — query metrics with histogram, Prometheus export, and custom callbacks
- **Database operations** — `backup()`, `restore()`, `health_check()`, `vacuum()`, `checkpoint()` (sync + async)
- **Change tracking** — `TrackedModel` for dirty field detection, `DiffMixin` for instance comparison and cloning
- **Benchmark suite** — 22 scenarios across 5 suites with automated chart generation

### Changed

- `model.save()` inside an explicit transaction no longer auto-commits (breaking change for code that relied on immediate commit behavior inside transactions)
