# sqler

Document-oriented JSON store on SQLite.

## Follow-up TODO

### From BUG-1/BUG-2 plan (not in scope)
- [ ] Remove `set_db()` (backward compat concern — `using()` is the recommended path now)
- [ ] Add `db` param to `save()`/`delete()` instance methods (allows per-call DB binding; `using()` covers query path which is qler's primary need)
- [ ] Sync adapter changes (already uses thread-local; no concurrency bug — but could add `using()` parity)

### From security audit (pre-existing)
- [ ] Add field-name validation (`_validate_field_name`) to `order_by()`, `distinct_values()`, aggregate methods — prevents SQL injection via unvalidated JSON paths
- [ ] Validate `create_index`/`drop_index` params (`field`, `name`, `where`) before embedding in DDL
- [ ] Validate promoted column names in `_ensure_table_with_promoted()` before building DDL
- [ ] Wrap cursor operations in `try/finally` throughout `async_db.py` to prevent resource leaks on exception
- [ ] Restrict `execute_sql()` to SELECT statements or rename to private method
