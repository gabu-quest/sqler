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

### M-7: Mixin `db=` Forwarding ✅
- Added `*, db=None` to all 21 mixin methods that override `save()`/`delete()`
- `_require_binding()` → `_resolve_binding(db)` in AuditLogMixin/AsyncAuditLogMixin
- 30 tests (15 sync + 15 async) covering all mixin variants with db= forwarding
- Hardened assertions: `assert_recent_utc()`, exact value comparisons, veto paths, db isolation

### M-8: Mixin Test Coverage Gaps ✅
Coverage gaps identified by test auditor — all relate to mixin behavior not exercised by the db= forwarding tests. 14 tests added (7 sync + 7 async), hardened by second audit pass.

- [x] Persisted doc audit field round-trip: ORM reload + raw doc verification
- [x] AuditLogMixin log entry fields: user, recency-checked timestamp, snapshot
- [x] SoftDeleteMixin class methods: `active()`, `with_deleted()`, `only_deleted()` with exact name assertions + clean-slate guard
- [x] AuditLogMixin silent-update branch: re-save without changes produces no audit entry
- [x] HooksMixin `_hooks_enabled = False`: all 4 hooks skipped on both save and delete paths
- [x] AuditMixin getter exception-swallowing: raising getter → `created_by is None`
- [x] FullMixin delete path with `db=`: `hard_delete(db=db2)` through full MRO with hook tracking

### M-9: BUG-6 — `F("_id")` Silently Returns No Results ✅
**Found:** 2026-02-23 | **Severity:** Medium

`F("_id")` generates `json_extract(data, '$._id')` in WHERE clause, but `_id` is a real SQLite column (rowid), not stored in the JSON `data` blob. Query completes without error but finds zero rows.

- [x] Added `_META_COLUMNS = ["_id", "_version"]` constant to `query.py`
- [x] Applied `_rewrite_promoted_refs(sql, _META_COLUMNS)` in all SQL-building paths: `_build_query`, `_build_aggregate_query`, `distinct_values`, `update`, `update_one`, `delete` (sync + async)
- [x] Also fixed F-expression SET values in `update()`/`update_one()` (e.g., `update(score=F("_id") + 0)`)
- [x] 29 tests (15 sync + 14 async) covering filter, order_by, aggregate, version filter, promoted interaction, SQL-level rewrite verification

### M-10: Test Suite Hardening ✅
Audit-driven pass to eliminate lying tests, softball assertions, and coverage gaps across 8 test files. No production code changes.

- [x] 4 critical test rewrites: `test_C09` (bare except → `pytest.raises`), `test_C10` (zero assertions → index property checks), `test_C29` (manual log injection → adapter auto-capture), `test_C42` (hasattr-only → behavioral pool test)
- [x] ~30 loose assertions tightened: `hasattr` guards removed, `>= N` → exact values, `any()` existence checks → exact count + value, `isinstance` standalone → content proof, guard-without-proof → field-value assertions
- [x] 2 new retry exhaustion tests (sync + async): monkey-patch commit to force rebase loop exhaustion at `max_retries=2`
- [x] 1 missing async SQL injection parametrize value added (multi-statement INSERT)
- [x] Timestamp assertions hardened with recency bounds (within 5 seconds)
- [x] Explain plan assertions check `r["detail"]` for SCAN/SEARCH content
- [x] All 1141 tests pass, 0 failures
