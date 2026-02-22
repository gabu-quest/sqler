# sqler

Document-oriented JSON store on SQLite.

## Active Roadmaps
- [qler-prerequisites](./ROADMAP-QLER.md) — all milestones complete (M-1 through M-6)

## Follow-up TODO

### From BUG-1/BUG-2 plan (not in scope)
- [~] Deprecate `set_db()` — soft deprecation with `warnings.warn()` added (M-5); full removal deferred
- [x] Add `db` param to `save()`/`delete()` instance methods (M-4 complete)
- [ ] Sync adapter changes (already uses thread-local; no concurrency bug — but could add `using()` parity)

### From security audit (pre-existing)
- [x] Add field-name validation (`validate_field_name`) to `order_by()`, `distinct_values()`, aggregate methods, `update()`, `update_one()` — prevents SQL injection via unvalidated JSON paths
- [x] Validate `create_index`/`drop_index` params (`field`, `name`) before embedding in DDL
- [x] Validate promoted column names in `_ensure_table_with_promoted()` before building DDL
- [x] Wrap cursor operations in `try/finally` throughout `async_db.py` to prevent resource leaks on exception
- [x] Restrict `execute_sql()` to SELECT/EXPLAIN/PRAGMA/WITH statements
- [x] Validate `FTSIndex` fields, index_name, and tokenizer in `fts.py` + parameterize highlight/snippet tags
- [x] Validate `checkpoint` mode parameter against allowlist in `ops.py`
