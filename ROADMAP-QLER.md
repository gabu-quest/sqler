# Roadmap: sqler qler-prerequisites

Branch: `feat/qler-prerequisites`

## Milestones

### M-1: sqler gaps for qler ✅
- Connection pool fix for async adapter
- `using()` for per-query DB binding (async)
- Promoted column aggregate query fixes

### M-2: Security Hardening ✅
- `validate_field_name()` / `validate_identifier()` across all SQL interpolation surfaces
- `execute_sql()` restricted to read-only + multi-statement rejection
- FTSIndex field/tokenizer/highlight validation + parameterization
- Checkpoint mode allowlist
- 300+ adversarial tests (sync + async)

### M-3: Async Cursor Resource Safety ✅
- Wrap all ~24 cursor operations in `async_db.py` with `try/finally`
- Prevents resource leaks if exceptions fire between `execute()` and `cur.close()`
- Mechanical change — no API changes, no behavior changes

### M-4: Per-Call DB Binding for Instance Methods ✅
- Add `db` param to `save()` and `delete()` on sync `SQLerModel`
- Add `db` param to `save()` and `delete()` on async `AsyncSQLerModel`
- Mirror to `SafeModel` / `AsyncSafeModel` (optimistic locking variants)
- Mirror to `SQLerLiteModel` / `AsyncSQLerLiteModel` (dataclass variants)
- Safe model rebase path uses direct `db.find_document_with_version()` instead of `cls.from_id()` (respects per-call db)
- `_resolve_binding(db=None)` added to all 4 base model classes
- 69 tests (37 sync + 32 async) covering all 8 model variants
- Completes BUG-2: `using()` covers queries, this covers writes

### M-5: Deprecate `set_db()` + Lite `using()` Parity ✅
- Add `using()` classmethod to `SQLerLiteModel` and `AsyncSQLerLiteModel`
- Deprecate `set_db()` with `warnings.warn()` on all 4 base model classes
- Safe models inherit warning via `super().set_db()`
- `bind()` aliases on lite models also trigger warning

### M-6: Security Fixes + Test Hardening ✅
- `_resolve_binding` validates table names via `validate_table_name()` in all 4 model files
- Async `delete_with_policy` routes through `db.delete_document()` instead of raw adapter calls
- Async promoted `save(db=...)` uses per-call DB for schema setup (not class-level)
- Async test fixture conversion: `make_async_db_pair()` → `@pytest_asyncio.fixture` with try/finally cleanup
- Removed standalone `assert _id > 0` assertion
- Added 6 coverage tests: lite delete_with_policy, lite delete_wrong_db_is_noop (sync+async), promoted save to alternate db (async), resolve_binding fallback (async)
- Added missing `_version` assertion to `test_safe_save_default_behavior_unchanged`
