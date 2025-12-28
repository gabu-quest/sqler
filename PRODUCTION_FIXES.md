# SQLer Production Readiness Fixes

## P0 - Production Blockers

- [ ] **1. Exception Hierarchy Chaos**
  - Async raises `ConnectionError`, sync raises `NoAdapterError`
  - `StaleVersionError` defined twice with different base classes
  - Files: `exceptions.py`, `query.py`, `async_query.py`, `safe.py`, `async_safe.py`

- [ ] **2. Type Ignore Cleanup (73 instances)**
  - Run `mypy --strict` and fix issues
  - Files: `safe.py`, `queryset.py`, `mixins.py`, `model.py`

- [ ] **3. Silent Error Swallowing**
  - Replace `except Exception:` with specific types
  - Files: `sqler_db.py`, `queryset.py`, `synchronous.py`, `query.py`, `async_query.py`

## P1 - High Priority

- [ ] **4. Async Implementation Incomplete**
  - `async delete()` skips integrity checks
  - Missing `delete_with_policy()` in async
  - Add async test parity

- [ ] **5. Error Messages Lack Context**
  - Include model name, table, operation in errors
  - Files: `model.py`, `query.py`, `async_query.py`

- [ ] **6. HooksMixin Doesn't Actually Hook**
  - Base model never calls hook methods
  - Fix: Override save/delete in mixin to call hooks

## P2 - Medium Priority

- [ ] **7. Documentation Gaps**
  - Add error handling patterns to README
  - Add concurrency best practices
  - Add troubleshooting section

- [ ] **8. Index Management Incomplete**
  - Add `list_indexes()` method
  - Add `drop_index()` method
  - Make `ensure_index()` idempotent

- [ ] **9. Logging Not Auto-Integrated**
  - Integrate query_logger into adapter execute methods

## P3 - Lower Priority

- [ ] **10. Table Pluralization Edge Cases**
  - Handle irregular plurals better
  - Document the rules clearly

---

## Progress Log

| Date | Issue | Status | Notes |
|------|-------|--------|-------|
| | | | |
