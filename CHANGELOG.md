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
- **A stored reference is followed only in a field declared as a model.** A `{"_table", "_id"}` dict found anywhere in a document was followed when the row was loaded, also in a plain `dict`, `list` or `Any` field and at any depth. Someone who could write such a dict into a free-form field (a comment's metadata, say) got another table's row loaded into that field by `query().all()`, `first()` and `from_ids()`, and made `from_id()` raise `ValidationError`. A reference is now followed only in a field whose annotation names a model class (`author: User`, `Optional[User]`, `list[User]`, `dict[str, User]`), and only into a table of a class that field admits or a subclass of it. A reference anywhere else comes back as the dict it is, and no statement is sent for it. Every way of loading gives the same, sync and async.

### Fixed

- **`created_at` and `updated_at` are equal after the first save.** Since 1.2026.4.3 the first save set `updated_at` one microsecond after `created_at`. Every later save still gives a strictly later `updated_at`, now also when the clock has not advanced or has gone backwards, and when two saves follow each other within one clock tick. Applies to `TimestampMixin`, `AuditMixin` and `AsyncAuditMixin`. A stored `updated_at` that lies in the future is not pulled back: the next save is one microsecond after it.
- **Async adapter: a task that ends holding a pooled connection no longer keeps it.** A task cancelled between a statement and its commit (an outer `cancel()`, a `wait_for` timeout) used to end with its connection out of the pool and its write transaction open, so every other writer waited out the busy timeout and failed with "database is locked". When the last task using a connection ends without releasing it, the connection is now rolled back and returned to the pool, once. `close()` waits for any such rollback first.
- **Async adapter: a task created while its parent held a connection no longer returns that connection twice.** The child inherits the pin; the connection now goes back to the pool when the last task using it releases it or ends, and a child whose inherited pin was already returned takes a fresh connection instead of sharing one that is back in the pool.
- **Async adapter: a failed rollback no longer shrinks the pool.** If rolling back a reclaimed connection fails while the adapter is open, a warning is logged, that connection is closed and a fresh one is put in the pool.
- **Async adapter: `adapter.connection` is `None` for a task whose inherited connection was already returned.**
- **Async adapter: a statement that fails outside a transaction no longer keeps its pooled connection.** A syntax error, a constraint violation, a wrong parameter count, a failed commit or a failed `BEGIN` used to leave the task holding the connection, with any write it had started, until the task ended. The connection is now rolled back and returned before the error reaches the caller; this also rolls back earlier statements of the same task that were not yet committed on it. Inside a transaction the connection stays until the transaction ends, and ending it returns the connection whether the commit or rollback succeeds.
- **Async adapter: a cancelled statement returns its connection by itself.** A `wait_for` timeout or `task.cancel()` reaches the caller at once, also while the statement waits on a lock held by another connection. The connection goes back to the pool once SQLite has finished with it, and a cancelled write is rolled back. A commit or `executescript` that SQLite had already started when it was cancelled still completes.
- **Async adapter: `executemany` inside a transaction no longer commits it**, so a rollback removes its rows. Outside a transaction `executemany` and `executescript` commit and return the connection.
- **Async: a task that outlives the transaction it was started in is outside it.** Its writes are committed on a pooled connection, `in_transaction` is False, and `db.transaction()` begins a new transaction with `BEGIN IMMEDIATE`. Before, such a write was rolled back without an error and the pool stayed one connection short.
- **Async: a statement of one task waits while another task's nested transaction on the same connection is open**, so that task's rollback can no longer undo it. Statements go in the order they were asked, also one that had waited when a nested transaction was about to open on top of it; a wait longer than `acquire_timeout` raises `LockTimeoutError`.
- **Async: when the end of a nested transaction fails in SQLite, the whole transaction ends rolled back.** No `RELEASE` follows a failed `ROLLBACK TO`, which kept the rows it was to undo. The nested block, every later statement in that transaction (`execute`, `executemany`, a new nested transaction, also one that was waiting its turn, from any task), every later nested end and the block that ends the transaction raise `TransactionStateError` with SQLite's error as its cause, and send nothing. A block that is already raising keeps its exception and gets the sentence as a note.
- **Async: a statement that has its turn when the adapter is closed raises `NotConnectedError`** and sends nothing.
- **Async `query().all()`, `first()`, `from_id()` and `refresh()` raise a database error met while a referenced row is read** (for example `database is locked`), as the sync bases do. They returned the parent with an empty model in place of the referenced row: no `_id`, every field at its default. A typed reference into a table that is not a table of documents raises SQLite's error in the same way, and on the async lite bases a `TypeError` raised by the referenced row's class reaches the caller from `from_id()` and `refresh()`, as on the sync lite bases.

- **`async_export_jsonl` and `async_import_jsonl` run.** They imported `aiofiles`, which sqler does not depend on, and the import called `afrom_id` and `asave`, which no model has. They now need no extra package, do their file work off the event loop thread, work with all four async model bases, and give the same file and the same result as `export_jsonl` and `import_jsonl`.
- **JSONL import ignores a `_version` key in the file**, sync and async. An export of a safe model carries it; a lite safe model could not import its own export, and an update wrote the file's version over the stored one.
- **Async migrations are atomic.** `AsyncMigrationRunner` runs each migration's `up()` (or `down()`) and its version row in one transaction, as the sync runner does. A migration that fails or is cancelled leaves the schema, the data and the recorded versions as they were.
- **The async migration runner, `async_health_check`, `async_get_stats`, `async_checkpoint` and `async_vacuum` return their pooled connection** when they finish or raise. Inside your own transaction they keep using its connection and do not end it.
- **`async_health_check` always returns a `HealthStatus`**, also when returning the connection fails (the status is then unhealthy and names the error).
- **A migration runner first used inside a transaction that is rolled back** creates its version table again on the next call.

- **Sync adapter: a statement that fails outside a transaction no longer keeps the lock.** Python's `sqlite3` opens a transaction before a write and left it open when the write failed, so the thread held the write lock until its next commit and other threads got "database is locked". The failed statement, or a failed commit, is now rolled back before the error reaches the caller; this also rolls back that thread's earlier uncommitted statements, a raw `BEGIN` included. Inside `db.transaction()` nothing changes.
- **Sync adapter: `in_transaction` and `auto_commit()` are per thread.** A transaction open on one thread no longer stops other threads from committing. A plain `:memory:` database has one connection for all threads; its transaction belongs to the thread that opened it (next entry).
- **Sync adapter: on a plain `":memory:"` database a transaction belongs to the thread that opened it** (`SQLiteAdapter(":memory:")`, `SQLerDB.in_memory(shared=False)`). Other threads' statements wait for it to end, at most `timeout_ms`, then raise `sqlite3.OperationalError: database is locked`; every waiting thread goes on when it ends. Before, a second thread's transaction joined the first as a savepoint, and whichever thread ended the outer level committed or rolled back the work of both. A thread that ends inside its transaction has it rolled back, and a thread interrupted (Ctrl-C) while it waits holds nothing.
- **Sync adapter: on a plain `":memory:"` database a cursor no longer returns another thread's uncommitted rows.** A statement's rows are read before another thread may use the connection, so `query().iter()`, `iter_dicts()` and `stream_jsonl()` hold the whole result in memory there. A file database and a shared in-memory database are read as before.
- **`with adapter:` commits its own writes and leaves the thread's open transaction alone.** On a plain `":memory:"` database its writes (`bulk_upsert` uses it) stayed uncommitted on the one connection; inside a transaction of the same thread it committed that transaction early.
- **`backup()` and `restore()` on a plain `":memory:"` database wait for another thread's open transaction**, at most `timeout_ms`, and a `restore()` that runs out of time leaves that transaction as it is. They used to run in the middle of it.
- **`backup()` inside the calling thread's own open transaction fails at once**, on every database: the result is unsuccessful and says so, and nothing is copied. On a plain `":memory:"` database it never returned.
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
- **A typed reference to a row that is gone reads as `None`, on every way of loading.** A field typed as a model (`Child`, `Optional[Child]`, `list[Child]`, `dict[str, Child]`) whose reference leads to no row of the field's class (the row deleted, its table dropped, a table of another class or of none, an unusable `_table` or `_id`) reads as `None` when it is the field's value and is left out of a list or a dict (the key goes too); in a tuple with one entry per place (`tuple[Child, Child]`) the entry reads as `None` at its place. That holds for `from_id`, `from_ids`, `refresh`, `query().all()` and `first()`, sync and async, on every model base. Before, depending on the way and the base, it gave a model of the field's class with every field at its default and no row behind it (saving its parent raised), the reference dict, `None` inside a list, or a `ValidationError`. A model loaded that way is dirty (`is_dirty()`), so `save()` writes what was read; a required field gets its class's own `ValidationError` on the pydantic bases.
- **A reference is followed only as deep as the field's annotation puts a model.** The field's value for `Child`, `Optional[Child]` and `Union[..., Child]`, an entry for `list[Child]`, a value for `dict[str, Child]`, nested as the annotation nests. A `{"_table", "_id"}` dict deeper than that is data, and nothing is read for it. A name the module gives to an annotation (`Children = list[Child]`, a `type` statement) is read through, in a quoted annotation too.
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
- **`is_dirty()` is False for a model nobody changed, on every way it is loaded or saved.** It was True for a `SQLerModel` after `save()`, `save_many()`, `from_id()` and `from_ids()`, for a `SQLerSafeModel` after `from_ids()`, and on every model base as soon as a typed reference field was set. `get_dirty_fields()` names the fields `save()` would write differently, and no others.
- **`is_dirty()` sees a list or dict edited in place** (`model.tags.append(...)`, `model.meta["k"] = ...`) on the lite model bases, where it answered False. `save()` always wrote the edit.
- **`is_dirty()` does not raise for a model that holds a related model never saved**; every field is dirty.
- **A safe save that is rebased onto a newer row keeps the other writer's changes.** When a save on a stale version qualified for rebasing (by default a step of 1 on a field named `count`), every field of the saving model was written over the newer row, so a field only the other writer had changed went back to its old value without an error. The rebased save now writes the newer row, the fields the caller changed and the counter step.
- **After a rebased save the model holds what was written**, typed references included, and `is_dirty()` is False. It kept its values from before the merge, so a second `save()` wrote them back or raised `StaleVersionError`, and writers adding to one counter in turn lost increments.
- **A value is stored in one form, whichever call writes it, and a filter binds that form.** A `datetime`, `date` or `time` is its `isoformat()`; a `UUID` or a path its `str`; an `Enum` its value; a set a sorted list; a tuple a list; a nested pydantic model its fields; the same at every depth of a list or dict. This holds for `save()` and `save_many()` of every model base, for `update()`, `update_one()` and `update_n()` on a query, for the document writes of `SQLerDB` and `AsyncSQLerDB`, and for the value in `F("field") == value` and the other comparisons. What it fixes:
  - `save()` raised `TypeError` for a field holding a `UUID`, a `time`, an `Enum`, a path, a set, or a nested model with a `date` in it, on every model base, and for a `date` on `AsyncSQLerModel` and `AsyncSQLerSafeModel`.
  - `F("happened") == datetime(...)` found no row: the filter bound `2026-11-14 09:30:00`, the row holds `2026-11-14T09:30:00`. `update(happened=datetime(...))` stored the form with the space.
  - `update(tags=[...])` and `update(meta={...})` stored a string of JSON text instead of a list or an object; a pydantic model could not load the row afterwards, and `contains()` found nothing in it.
  - `update(flag=True)` stored the integer 1 where `save()` stores `true`.
  - `is_dirty()` was True straight after loading a model with a tuple field.
- **A model with `AuditLogMixin` or `AsyncAuditLogMixin` can hold a `datetime`, a `date` or a reference.** Creating one with a `date` or a reference field raised `TypeError`, and so did updating one with a `datetime` field, `TimestampMixin`'s included. The audit row now compares the row before the save with the document the save wrote, so `changes` names the fields that changed and no others, and `changes` and `snapshot` hold values as they are stored: a datetime as its ISO text, a reference as `{"_table", "_id"}`.
- **A search index follows its table.** With `content_sync=True` (the default) `FTSIndex.create()` adds triggers that keep the index equal to its table whoever writes, and indexes the rows already in the table; the flag used to do nothing, so a row written without the mixin was not found and a changed row was found by its old words. An index made by an earlier version gets its triggers and its missing rows on the first `create()` after the upgrade, which reads the whole table once. A `create()` that finds the index as defined writes nothing and waits for no lock; one that fails leaves the index and its triggers as they were. An update that changes no indexed field does not rewrite the index. `content_sync=False` keeps the manual `index()`.
- **A search string that SQLite refuses raises `SearchQueryError`** naming the query, in place of a bare `sqlite3.OperationalError` (`C++`, `it's`, `foo-bar`, an empty string).
- **The FastAPI example runs again, on the public API.** Its seed wrote through `execute_sql`, which takes only reads, so seeding raised; its filters on a related row used `F("country._id")`, which is one key, so they matched nothing. It now names its database on every call (`Model.using(db)`, `save(db=)`, `from_ids(db=)`) and uses neither `set_db()` nor the adapter. Its tests run with the default `pytest` run, on a temporary database.
- **`FTSIndex` given an `AsyncSQLerDB` raises `TypeError`** saying the async database has no search yet, instead of an unrelated error later.
- **`query_logger.capture()` gives the statements of its block when the log is full.** The log keeps its newest 1000 entries, and a block opened on a full log got nothing (with 995 entries, a block of ten statements got five). A block now gets every statement logged while it is open, and its list fills as they are logged. Of two blocks that overlap, the one that ends first no longer switches logging off under the other; `enable()` and `disable()` called inside a block keep what they set once it ends; and `metrics.enable()` called inside a block keeps counting after it.
- **Sync adapter: a thread that ends gives back its connection.** Each thread's connection stayed open until `close()`, so a program that starts threads over its life grew one open connection per thread, and a thread that ended inside a transaction kept the write lock. The connection now closes when its thread ends, and its open transaction is rolled back. A shared in-memory database (`SQLerDB.in_memory()`) still lives from `connect()` until `close()`, whichever threads end in between.
- **Sync adapter: a thread that was running across `close()` and `connect()` works again.** It kept its closed connection and failed with "Cannot operate on a closed database."

### Changed

- **A model kept in an untyped field reads back as its reference.** This follows from the security entry "A stored reference is followed only in a field declared as a model". A model put into a `dict`, `list`, `Any` or unannotated field is still stored as a reference, and is now read back as `{"_table": ..., "_id": ...}`. To get the model back, declare the field: `author: Optional[User] = None`, `tags: list[Tag]`, `by_key: dict[str, User]`. The stored rows need no change. A model loaded with a reference dict in an untyped field is not dirty, and `save()` writes the row back unchanged. A class with no typed field sends no statement for references and walks no document when it is loaded.
- **The PyPI publish workflow runs the linter and the test suite first**, and refuses a tag that does not match the version in `pyproject.toml`.
- **Async adapter: `executescript` inside a transaction raises `QueryError`**, because SQLite commits the open transaction before it runs a script.
- **Async adapter: waiting for a pooled connection times out.** `acquire_timeout` (seconds, default 30, `None` waits forever) on `AsyncSQLiteAdapter`, `on_disk()` and `in_memory()`; a wait that runs out raises `ConnectionPoolExhaustedError`. Tasks waiting when `close()` is called raise `NotConnectedError`.
- **Async: a transaction, or a nested transaction, that commits while another task's nested transaction is still open inside it is rolled back.** The block and the other task both get the new `TransactionStateError` (a `ConcurrencyError`, exported from `sqler`). End the tasks inside the block: await them there, or use a `TaskGroup`. A nested transaction that is only still being closed is waited for and the commit goes through. A task that ends with its own nested transaction open has it rolled back.

- **`adapter.executescript()` inside an async migration fails.** The migration now runs in a transaction and the adapter refuses a script there. Use one `adapter.execute()` per statement; the failed `MigrationResult.error` says so.
- **`async_vacuum` inside a transaction raises `QueryError`** before it runs anything; the transaction stays usable.

- **Sync adapter: `executescript` inside a transaction raises `QueryError`**, as the async adapter does.
- **`SQLiteAdapter.execute()` is annotated as returning `CursorLike`** (`sqler.adapter.abstract`): `fetchone`, `fetchmany`, `fetchall`, iteration, `description`, `rowcount`, `lastrowid` and `close`. A `sqlite3.Cursor` is one, and so are the rows a plain `":memory:"` database has already read. Code that needs another attribute of `sqlite3.Cursor` (`connection`, `row_factory`) does not find it on those rows.
- **`RebaseConfig.max_retries` counts version conflicts only**, and a safe save sleeps at most 50 ms between two tries.
- **`FTSIndex.fields` returns a copy.** Appending to it no longer changes the index; assign a new list instead.
- **A violated unique index or primary key raises `UniqueConstraintError`**, sync and async, from every write path including a raw `adapter.execute`. It names the table and columns SQLite reported (or the index, for an index on an expression) and has the driver error as its cause. It is still a `sqlite3.IntegrityError`, so `except sqlite3.IntegrityError` keeps working; NOT NULL, CHECK and FOREIGN KEY errors are unchanged.

- **Every route uses one rule for a model's table**: the table bound with `set_db()`, else `__tablename__`, else the plural of the class name. For a class that was never bound, `FTSIndex` now reads `articles` instead of `article`, so its default index name changes from `article_fts` to `articles_fts`.
- **A read creates no table.** `find_document`, `find_documents`, `find_document_with_version` and `query()` of `SQLerDB` and `AsyncSQLerDB`, and through a model `query()`, `using(db)`, `from_id`, `from_ids` and `refresh`, give the empty answer on a table that does not exist (`[]`, `None`, 0, false; a queryset `update()` or `delete()` changes 0 rows) instead of creating the table or raising "no such table". A query built by hand (`SQLerQuery(table=..., adapter=...)`, `AsyncSQLerQuery(...)`) does the same, where it raised "no such table"; `explain()` and `explain_query_plan()` still raise. The exports (`export_csv`, `export_json`, `export_jsonl` and their string, stream and async forms) of a table that does not exist write what they write for an empty table. A read no longer needs the write lock. "no such table" for another table in the query still raises. `find_document_with_version` still adds a missing `_version` column to an existing table.
- **Model classes are registered when they are defined**, so `sqler.registry.tables()` lists classes that were never bound, and the integrity scans look at their tables. A class bound with `set_db()` is never replaced by a class that is only defined.
- **`as_ref()` on an unsaved model** now says "model must be saved"; a saved model no longer has to be bound.

- **`from_id()` and `refresh()` read each referenced table with one query** instead of one query per reference (a row with 20 references: 2 queries, was 21), and a cycle of references is cut at the first row seen again, as `from_ids()` cuts it, instead of after about 160 levels.
- **A reference that is kept as it is stored is left whole.** That is a reference to a row being loaded further up (a cycle) and one deeper than `max_depth`. `from_id()`, `refresh()` and async `query().all()` no longer load references nested inside it.
- **Async `query().all()` no longer walks each row twice.** It follows references in the batch only, as the sync bases do.
- **A rebased save raises `StaleVersionError` when the newer row cannot be loaded as the model class** (text where the class has a list, on the pydantic safe bases). The validation error is its cause; nothing is written. Before, the save went through and wrote the caller's old values.
- **Rows that `update()` wrote before this release keep the form they were written in.** A datetime written by `update()` has a space where `save()` writes `T`, and a list or dict written by `update()` is a string of JSON text; filters now bind the `save()` form and do not find the first, and the second was never a list. To rewrite them (table `events`, fields `happened` and `tags`):

  ```sql
  UPDATE events SET data = json_set(data, '$.happened', replace(json_extract(data, '$.happened'), ' ', 'T'))
  WHERE json_extract(data, '$.happened') LIKE '____-__-__ __:__:__%';

  UPDATE events SET data = json_set(data, '$.tags', json(json_extract(data, '$.tags')))
  WHERE json_type(data, '$.tags') = 'text' AND json_valid(json_extract(data, '$.tags'));
  ```

  The second statement also turns a field that is meant to hold a string of JSON text into the list or object; leave such a field out.
- **The audit log reports what changed in the stored row.** A key of the row that the model class has no field for is listed in `changes` with `new` null, because `save()` drops it. A save that stores nothing, because the row was deleted in the meantime, writes no audit row.
- **A `Decimal` is stored as a number.** `save()`, `update()`, the document writes and filters refused a `Decimal`. It is now stored as the JSON number it equals: an integer when it is whole (`Decimal("5.00")` is stored as `5`), else a float, and only when reading that float back gives the same `Decimal`. `Decimal("0.1")` is stored; `Decimal("0.1234567890123456789")`, `Decimal("1E+30")`, a whole value outside SQLite's 64-bit integers, `NaN` and `Infinity` are refused, so a stored value is never a rounded one. Store such a value as text (`str(value)`) in a `str` field. A filter by a `Decimal` finds the rows written with it, and a filter by a refused `Decimal` raises instead of finding nothing. A model base that decodes by annotation (pydantic, msgspec) loads the field as a `Decimal` again; the lite bases load the number.
- **A value with no stored form raises `UnstorableValueError`.** It is a `ValidationError` and still a `TypeError`, so `except TypeError` keeps working. `value` is the refused value; `field` is its place and the message names it: the path in the document for a document write (`lines[2].price`), the field and the path inside it for a model save (`v[1].price`), the column for a promoted column. `bytes` in a document are refused this way (see below).
- **A `timedelta` is stored as its number of seconds.** `save()`, `update()`, the document writes and filters refused a `timedelta`. It is now stored as a JSON number: an integer when the seconds are whole, else a float, and only when `timedelta(seconds=number)` equals it; a `timedelta` that no number of seconds holds raises `UnstorableValueError`. Filters, ordering, `sum()` and `avg()` work on the seconds. A model base that decodes by annotation (pydantic, msgspec) loads the field as a `timedelta` again; the lite bases load the number.
- **`bytes` in a document say where they go.** `bytes`, `bytearray` and `memoryview` in a JSON document raise `UnstorableValueError`, with a message that says to promote the field to a `BLOB` column or store text; `update()` raised `sqlite3.OperationalError` for them. A field promoted to a `BLOB` column takes them as they are.
- **`FTSIndex.create()` raises `SchemaError` when the index exists with another definition**: other fields, another field order or another tokenizer. It used to keep the old index without a word. Call `drop()` then `create()` to change an index. With `content_sync=False`, `create()` drops the index's triggers.
- **`is_dirty()` answers what `save()` would change in the stored row.** One case now reads dirty where it did not: a typed reference whose row is gone and reads `None`, because `save()` would write null over the reference.
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

### Removed

- **`PooledSQLerDB`, `PooledSQLiteAdapter`, `ConnectionPool` and the `sqler.pool` module**, with no alias. Use `SQLerDB`: it gives each thread its own connection and takes it back when the thread ends, so threads share one `SQLerDB` (`SQLerDB.on_disk(path)` in place of `PooledSQLerDB.on_disk(path, max_connections=n)`). There is no `max_connections`: the number of open connections is the number of live threads that used the database. `ConnectionPoolExhaustedError` stays; the async pool raises it.
- **`PoolStats` moved from `sqler.pool` to `sqler.adapter.asynchronous`** and is exported from `sqler`. Its fields are unchanged. A `PoolStats` pickled by an older version does not load.
- **`Transaction(adapter)` without a database.** `Transaction(adapter, db)` requires both; `db.transaction()` is the way to open one.

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
- **`search(text, plain=True)`**, and the same on every search method of `FTSIndex` and `SearchableMixin`, finds the words of the text, so `C++`, `it's` and `foo-bar` can be searched. Without it the string is an FTS5 query, as before. `SearchQueryError` can be imported from `sqler`; it is a `QueryError`.

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
