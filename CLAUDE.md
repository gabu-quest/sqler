# sqler

Document-oriented JSON store on SQLite.

## Follow-up TODO

### From BUG-1/BUG-2 plan (not in scope)
- [ ] Remove `set_db()` (backward compat concern — `using()` is the recommended path now)
- [ ] Add `db` param to `save()`/`delete()` instance methods (allows per-call DB binding; `using()` covers query path which is qler's primary need)
- [ ] Sync adapter changes (already uses thread-local; no concurrency bug — but could add `using()` parity)

### From security audit (pre-existing)
- [x] Add field-name validation (`validate_field_name`) to `order_by()`, `distinct_values()`, aggregate methods, `update()`, `update_one()` — prevents SQL injection via unvalidated JSON paths
- [x] Validate `create_index`/`drop_index` params (`field`, `name`) before embedding in DDL
- [x] Validate promoted column names in `_ensure_table_with_promoted()` before building DDL
- [ ] Wrap cursor operations in `try/finally` throughout `async_db.py` to prevent resource leaks on exception
- [x] Restrict `execute_sql()` to SELECT/EXPLAIN/PRAGMA/WITH statements
- [ ] Validate `FTSIndex` fields, index_name, and tokenizer in `fts.py` (CRITICAL — unvalidated strings in DDL)
- [ ] Validate `checkpoint` mode parameter against allowlist in `ops.py`
