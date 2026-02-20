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

### M-4: Per-Call DB Binding for Instance Methods ⬚
- Add `db` param to `save()` and `delete()` on sync `SQLerModel`
- Add `db` param to `save()` and `delete()` on async `AsyncSQLerModel`
- Mirror to `SafeModel` / `AsyncSafeModel` (optimistic locking variants)
- Mirror to `SQLerLiteModel` / `AsyncSQLerLiteModel` (dataclass variants)
- Completes BUG-2: `using()` covers queries, this covers writes

### M-5: Deprecate `set_db()` + Sync `using()` Parity ⬚
- Add `using()` classmethod to sync `SQLerModel` (async already has it)
- Deprecate `set_db()` with `warnings.warn()` pointing to `using()`
- Update docstrings and any internal usage
