# sqler

Document-oriented JSON store on SQLite.

## Benchmark Hygiene (MANDATORY)

1. **Always run with `--storage both`** — never report memory-only results. Disk I/O can change ratios.
2. **Every sqler measurement needs a sqlite baseline** — no orphan measurements. If there's no natural baseline, document why.
3. **Both arms must do equivalent work** — matched PRAGMAs, matched SQL, matched serialization.
4. **Run at medium scale minimum** (50K rows) — small scale results are noisy and misleading.
5. **Document known caveats** — every benchmark has weaknesses. State them, don't hide them.

## Follow-up TODO

### Architecture decisions pending
- [x] Hydration alternatives — msgspec prototype shipped (M-5: 2.1x hydration speedup). `SQLerMsgspecModel` available as opt-in.

### From BUG-1/BUG-2 plan (not in scope)
- [~] Deprecate `set_db()` — soft deprecation with `warnings.warn()` added (M-5); full removal deferred
- [x] Add `db` param to `save()`/`delete()` instance methods (M-4 complete)
- [x] Sync adapter changes (using() parity already added in M-4/M-5)

### From security audit (pre-existing)
- [x] Add field-name validation (`validate_field_name`) to `order_by()`, `distinct_values()`, aggregate methods, `update()`, `update_one()` — prevents SQL injection via unvalidated JSON paths
- [x] Validate `create_index`/`drop_index` params (`field`, `name`) before embedding in DDL
- [x] Validate promoted column names in `_ensure_table_with_promoted()` before building DDL
- [x] Wrap cursor operations in `try/finally` throughout `async_db.py` to prevent resource leaks on exception
- [x] Restrict `execute_sql()` to SELECT/EXPLAIN/PRAGMA/WITH statements
- [x] Validate `FTSIndex` fields, index_name, and tokenizer in `fts.py` + parameterize highlight/snippet tags
- [x] Validate `checkpoint` mode parameter against allowlist in `ops.py`
