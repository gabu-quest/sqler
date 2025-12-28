# Sync/Async Parity Checklist

This document enumerates every feature in the sync implementation and tracks parity with the async implementation.

**Legend:**
- ✅ = Full parity
- ⚠️ = Partial parity (works but missing features/options)
- ❌ = Missing in async
- 🔧 = Different implementation (check notes)

---

## 1. Adapter Layer

### SQLiteAdapter vs AsyncSQLiteAdapter

| Feature | Sync | Async | Parity | Notes |
|---------|------|-------|--------|-------|
| **Constructor** |
| `__init__(path, pragmas, timeout_ms)` | ✅ | ⚠️ | ⚠️ | Async missing `timeout_ms` parameter |
| **Connection Management** |
| `connect()` | ✅ | ✅ | ✅ | |
| `close()` | ✅ | ✅ | ✅ | |
| Thread-local connections | ✅ | N/A | 🔧 | Sync uses thread-local; async is single-connection |
| Memory singleton mode | ✅ | ❌ | ❌ | Sync has `_memory_singleton` flag for `:memory:` |
| **Execution** |
| `execute(query, params)` | ✅ | ✅ | ✅ | |
| `executemany(query, param_list)` | ✅ | ✅ | ✅ | |
| `executescript(script)` | ✅ | ✅ | ✅ | |
| Query logging | ✅ | ✅ | ✅ | Both use `query_logger.log()` |
| **Commits & Transactions** |
| `commit()` | ✅ | ✅ | ✅ | |
| `auto_commit()` | ✅ | ✅ | ✅ | Transaction-aware commit |
| `in_transaction` property | ✅ | ✅ | ✅ | |
| `begin_transaction()` | ✅ | ✅ | ✅ | Uses BEGIN IMMEDIATE |
| `end_transaction(commit=True)` | ✅ | ✅ | ✅ | |
| `_txn_depth` tracking | ✅ | ✅ | ✅ | Nested transaction support |
| **Context Managers** |
| `__enter__/__exit__` | ✅ | ✅ | ✅ | |
| `__aenter__/__aexit__` | N/A | ✅ | ✅ | |
| **Factory Methods** |
| `in_memory(shared, name)` | ✅ | ✅ | ✅ | Same pragmas |
| `on_disk(path)` | ✅ | ✅ | ✅ | Same pragmas |

### Adapter Issues Found:
1. **`timeout_ms` parameter missing in async** - Sync accepts `timeout_ms` parameter, async doesn't

---

## 2. Database Layer

### SQLerDB vs AsyncSQLerDB

| Feature | Sync | Async | Parity | Notes |
|---------|------|-------|--------|-------|
| **Factory Methods** |
| `in_memory(shared, name)` | ✅ | ✅ | ✅ | |
| `on_disk(path)` | ✅ | ✅ | ✅ | |
| **Connection Management** |
| `connect()` | ✅ | ✅ | ✅ | |
| `close()` | ✅ | ✅ | ✅ | |
| **Document Operations** |
| `_ensure_table(table)` | ✅ | ✅ | ✅ | Uses `auto_commit()` |
| `insert_document(table, doc)` | ✅ | ✅ | ✅ | Uses `auto_commit()` |
| `upsert_document(table, _id, doc)` | ✅ | ✅ | ✅ | Uses `auto_commit()` |
| `find_document(table, _id)` | ✅ | ✅ | ✅ | |
| `delete_document(table, _id)` | ✅ | ✅ | ✅ | Uses `auto_commit()` |
| `bulk_upsert(table, docs)` | ✅ | ✅ | ⚠️ | Sync uses context manager for batching |
| `execute_sql(query, params)` | ✅ | ✅ | ⚠️ | Sync has better column name detection |
| **Query** |
| `query(table)` | ✅ | ✅ | ✅ | Returns QuerySet/AsyncQuerySet |
| **Indexing** |
| `create_index(table, field, unique, name, where)` | ✅ | ✅ | ✅ | |
| `drop_index(name)` | ✅ | ✅ | ✅ | |
| `list_indexes(table)` | ✅ | ✅ | ✅ | |
| `index_exists(name)` | ✅ | ✅ | ✅ | |
| **Transactions** |
| `transaction()` | ✅ | ✅ | ✅ | Returns Transaction/AsyncTransaction |
| `__enter__/__exit__` | ✅ | ⚠️ | ⚠️ | Async uses direct SQL instead of adapter methods |
| **Versioned (Optimistic Locking)** |
| `_ensure_versioned_table(table)` | ✅ | ✅ | ✅ | |
| `upsert_with_version(table, _id, doc, version)` | ✅ | ✅ | ⚠️ | Sync has write lock optimization |
| `find_document_with_version(table, _id)` | ✅ | ✅ | ✅ | |
| `_ddl_lock` for thread safety | ✅ | N/A | 🔧 | Async doesn't need thread locks |
| `_versioned_tables` cache | ✅ | ✅ | ✅ | |

### DB Issues Found:
1. **`__aenter__/__aexit__` doesn't use adapter transaction methods** - Lines 361-375 in async_db.py use direct SQL instead of `begin_transaction()`/`end_transaction()`
2. **`bulk_upsert` batching differs** - Sync uses adapter context manager for implicit transaction
3. **`execute_sql` column detection** - Sync has row.keys() fallback for sqlite3.Row
4. **`upsert_with_version` write lock** - Sync has early write lock acquisition to reduce live-lock

---

## 3. Query Layer

### SQLerQuery vs AsyncSQLerQuery

| Feature | Sync | Async | Parity | Notes |
|---------|------|-------|--------|-------|
| **Chaining Methods** |
| `filter(expression)` | ✅ | ✅ | ✅ | |
| `exclude(expression)` | ✅ | ✅ | ✅ | |
| `or_filter(expression)` | ✅ | ✅ | ✅ | |
| `distinct()` | ✅ | ✅ | ✅ | |
| `order_by(field, desc)` | ✅ | ✅ | ✅ | |
| `limit(n)` | ✅ | ✅ | ✅ | |
| `offset(n)` | ✅ | ✅ | ✅ | |
| `select(*fields)` | ✅ | ✅ | ✅ | |
| `with_version()` | ✅ | ✅ | ✅ | |
| **Execution Methods** |
| `all()` | ✅ | ✅ | ✅ | |
| `first()` | ✅ | ✅ | ✅ | |
| `all_dicts()` | ✅ | ✅ | ⚠️ | Sync has `InvariantViolationError` |
| `first_dict()` | ✅ | ✅ | ✅ | |
| **Aggregates** |
| `count()` | ✅ | ✅ | ✅ | |
| `sum(field)` | ✅ | ✅ | ✅ | |
| `avg(field)` | ✅ | ✅ | ✅ | |
| `min(field)` | ✅ | ✅ | ✅ | |
| `max(field)` | ✅ | ✅ | ✅ | |
| `exists()` | ✅ | ✅ | ✅ | |
| `distinct_values(field)` | ✅ | ✅ | ✅ | |
| **Pagination** |
| `paginate(page, per_page)` | ✅ | ✅ | ✅ | Both use PaginatedResult |
| **Bulk Operations** |
| `update(**fields)` | ✅ | ✅ | ⚠️ | Sync uses `rowcount`, async uses `changes()` |
| `delete()` | ✅ | ✅ | ⚠️ | Sync uses `rowcount`, async uses `changes()` |
| **Debug** |
| `sql` property | ✅ | ✅ | ✅ | |
| `params` property | ✅ | ✅ | ✅ | |
| `debug()` | ✅ | ✅ | ✅ | |
| `explain(adapter)` | ✅ | ❌ | ❌ | Async doesn't have this |
| `explain_query_plan(adapter)` | ✅ | ❌ | ❌ | Async doesn't have this |

### Query Issues Found:
1. **`explain()` and `explain_query_plan()` missing in AsyncSQLerQuery** - These exist in sync but not async
2. **`InvariantViolationError` not raised in async** - Sync raises on NULL JSON data
3. **Row count detection differs** - Sync uses `cursor.rowcount`, async queries `SELECT changes()`

---

## 4. QuerySet Layer

### SQLerQuerySet vs AsyncSQLerQuerySet

| Feature | Sync | Async | Parity | Notes |
|---------|------|-------|--------|-------|
| **Chaining Methods** |
| `filter(expression)` | ✅ | ✅ | ✅ | |
| `exclude(expression)` | ✅ | ✅ | ✅ | |
| `or_filter(expression)` | ✅ | ❌ | ❌ | Missing in async |
| `distinct()` | ✅ | ❌ | ❌ | Missing in async |
| `select(*fields)` | ✅ | ❌ | ❌ | Missing in async |
| `order_by(field, desc)` | ✅ | ✅ | ✅ | |
| `limit(n)` | ✅ | ✅ | ✅ | |
| `offset(n)` | ✅ | ✅ | ✅ | |
| `resolve(flag)` | ✅ | ✅ | ✅ | |
| **Execution Methods** |
| `all()` | ✅ | ✅ | ✅ | |
| `first()` | ✅ | ✅ | ✅ | |
| **Aggregates** |
| `count()` | ✅ | ✅ | ✅ | |
| `sum(field)` | ✅ | ✅ | ✅ | |
| `avg(field)` | ✅ | ✅ | ✅ | |
| `min(field)` | ✅ | ✅ | ✅ | |
| `max(field)` | ✅ | ✅ | ✅ | |
| `exists()` | ✅ | ✅ | ✅ | |
| `distinct_values(field)` | ✅ | ❌ | ❌ | Missing in async |
| `paginate(page, per_page)` | ✅ | ✅ | ✅ | |
| **Bulk Operations** |
| `update(**fields)` | ✅ | ❌ | ❌ | Missing in async |
| `delete_all()` | ✅ | ❌ | ❌ | Missing in async |
| **Debug** |
| `sql()` | ✅ | ✅ | ✅ | |
| `params()` | ✅ | ✅ | ✅ | |
| `debug()` | ✅ | ✅ | ✅ | |
| `explain(adapter)` | ✅ | ✅ | 🔧 | Async takes no arg (uses internal adapter) |
| `explain_query_plan(adapter)` | ✅ | ✅ | 🔧 | Async takes no arg |
| **Batch Resolution** |
| `_batch_resolve(docs)` | ✅ | ✅ | ✅ | |
| `_attach_metadata(inst, doc)` | ✅ | ✅ | ✅ | |

### QuerySet Issues Found:
1. **`or_filter()` missing in async** - Exists in sync queryset
2. **`distinct()` missing in async** - Exists in sync queryset
3. **`select()` missing in async** - Exists in sync queryset
4. **`distinct_values()` missing in async** - Exists in sync queryset
5. **`update()` missing in async** - Exists in sync queryset
6. **`delete_all()` missing in async** - Exists in sync queryset

---

## 5. Model Layer

### SQLerModel vs AsyncSQLerModel

| Feature | Sync | Async | Parity | Notes |
|---------|------|-------|--------|-------|
| **Class Configuration** |
| `_id` PrivateAttr | ✅ | ✅ | ✅ | |
| `_snapshot` PrivateAttr | ✅ | ❌ | ❌ | Missing in async base model |
| `_db` ClassVar | ✅ | ✅ | ✅ | |
| `_table` ClassVar | ✅ | ✅ | ✅ | |
| `model_config["extra"]` | `"ignore"` | `"ignore"` | ✅ | |
| `model_config["frozen"]` | `False` | ❌ | ⚠️ | Not explicitly set in async |
| **Class Methods** |
| `set_db(db, table)` | ✅ | ✅ | ⚠️ | Sync calls `_ensure_table`, async doesn't |
| `db()` | ✅ | ❌ | ❌ | Missing in async |
| `_require_binding()` | ✅ | ✅ | ✅ | |
| `from_id(id_)` | ✅ | ✅ | ✅ | |
| `query()` | ✅ | ✅ | ✅ | |
| `filter(expression)` | ✅ | ✅ | ✅ | |
| `ref(name)` | ✅ | ✅ | ✅ | |
| `add_index(field, ...)` | ✅ | ✅ | ✅ | |
| `ensure_index(field, ...)` | ✅ | ✅ | ✅ | |
| **Instance Methods** |
| `save()` | ✅ | ✅ | ✅ | |
| `delete()` | ✅ | ✅ | ✅ | |
| `delete_with_policy(on_delete)` | ✅ | ⚠️ | ⚠️ | Async only supports "restrict" |
| `refresh()` | ✅ | ✅ | ✅ | |
| **Relationship Helpers** |
| `_is_ref_dict(value)` | ✅ | ❌ | ❌ | Helper missing in async |
| `_resolve_relations(data)` | ✅ | ✅ | 🔧 | Async uses `_aresolve_relations` |
| `_dump_with_relations()` | ✅ | ✅ | 🔧 | Async uses `_adump_with_relations` |
| **Integrity Helpers** |
| `_find_referrers()` | ✅ | ❌ | ❌ | Not in async |
| `_find_ref_paths()` | ✅ | ❌ | ❌ | Not in async |
| `_set_null_referrers()` | ✅ | ❌ | ❌ | Not in async |
| `_cascade_delete()` | ✅ | ❌ | ❌ | Not in async |
| `validate_references()` | ✅ | ❌ | ❌ | Not in async |

### Model Issues Found:
1. **`_snapshot` missing in AsyncSQLerModel** - Present in sync for delta tracking
2. **`db()` class method missing in async** - Returns bound database
3. **`set_db()` doesn't call `_ensure_table` in async** - Sync does this on bind
4. **Integrity helpers missing in async** - `_find_referrers`, `_cascade_delete`, etc.
5. **`delete_with_policy` limited in async** - Only "restrict" implemented

---

## 6. SafeModel Layer (Optimistic Locking)

### SQLerSafeModel vs AsyncSQLerSafeModel

| Feature | Sync | Async | Parity | Notes |
|---------|------|-------|--------|-------|
| **Attributes** |
| `_version` PrivateAttr | ✅ | ✅ | ✅ | |
| `_snapshot` PrivateAttr | ✅ | ✅ | ✅ | Present in async safe model |
| `_rebase_config` ClassVar | ✅ | ❌ | ❌ | Not in async |
| **Methods** |
| `set_db(db, table)` | ✅ | ⚠️ | ⚠️ | Sync ensures versioned table, async doesn't |
| `from_id(id_)` | ✅ | ✅ | ✅ | |
| `query()` | ✅ | ✅ | ✅ | |
| `save()` | ✅ | ✅ | ⚠️ | See rebase config below |
| `refresh()` | ✅ | ✅ | ✅ | |
| **Intent Rebasing** |
| `RebaseConfig` support | ✅ | ❌ | ❌ | Async has hardcoded rebase logic |
| `compute_numeric_scalar_deltas()` | ✅ | ✅ | ✅ | |
| `apply_numeric_scalar_deltas()` | ✅ | ✅ | ✅ | |
| `can_rebase_deltas()` | ✅ | ❌ | ❌ | Async has simple inline check |
| Configurable `max_retries` | ✅ | ❌ | ❌ | Async hardcodes 64 |
| Configurable `allowed_fields` | ✅ | ❌ | ❌ | Async hardcodes "count" only |
| Configurable `max_delta` | ✅ | ❌ | ❌ | Async checks `abs(dv) == 1` |

### SafeModel Issues Found:
1. **`_rebase_config` not in AsyncSQLerSafeModel** - Sync has full RebaseConfig support
2. **Async rebase logic is hardcoded** - Only allows `count` field with delta of ±1
3. **`set_db` doesn't ensure versioned table in async** - Has to be lazy

---

## 7. Mixins

### TimestampMixin, SoftDeleteMixin, HooksMixin

| Feature | Sync | Async | Parity | Notes |
|---------|------|-------|--------|-------|
| **TimestampMixin** |
| `created_at` field | ✅ | ✅ | ✅ | Same mixin |
| `updated_at` field | ✅ | ✅ | ✅ | Same mixin |
| `_set_timestamps()` | ✅ | ✅ | ✅ | Same mixin |
| **SoftDeleteMixin** |
| `deleted_at` field | ✅ | ✅ | ✅ | Same mixin |
| `is_deleted` property | ✅ | ✅ | ✅ | Same mixin |
| `soft_delete()` | ✅ | ⚠️ | ⚠️ | Calls sync `save()` |
| `restore()` | ✅ | ⚠️ | ⚠️ | Calls sync `save()` |
| `hard_delete()` | ✅ | ⚠️ | ⚠️ | Calls sync `delete()` |
| `active()` classmethod | ✅ | ⚠️ | ⚠️ | Returns sync queryset |
| `with_deleted()` classmethod | ✅ | ⚠️ | ⚠️ | Returns sync queryset |
| `only_deleted()` classmethod | ✅ | ⚠️ | ⚠️ | Returns sync queryset |
| **HooksMixin** |
| `before_save()` | ✅ | N/A | 🔧 | Use AsyncHooksMixin |
| `after_save()` | ✅ | N/A | 🔧 | Use AsyncHooksMixin |
| `before_delete()` | ✅ | N/A | 🔧 | Use AsyncHooksMixin |
| `after_delete()` | ✅ | N/A | 🔧 | Use AsyncHooksMixin |
| **AsyncHooksMixin** |
| `before_save()` | N/A | ✅ | ✅ | |
| `after_save()` | N/A | ✅ | ✅ | |
| `before_delete()` | N/A | ✅ | ✅ | |
| `after_delete()` | N/A | ✅ | ✅ | |
| **Convenience Mixins** |
| `FullMixin` | ✅ | N/A | 🔧 | Sync convenience |
| `AsyncFullMixin` | N/A | ✅ | ✅ | Async convenience |

### Mixin Issues Found:
1. **SoftDeleteMixin not async-aware** - Methods call sync `save()`/`delete()`
2. **Need AsyncSoftDeleteMixin** - For proper async soft delete support

---

## 8. Transaction Layer

### Transaction vs AsyncTransaction

| Feature | Sync | Async | Parity | Notes |
|---------|------|-------|--------|-------|
| `__init__(adapter)` | ✅ | ✅ | ✅ | |
| `__enter__/__exit__` | ✅ | N/A | 🔧 | |
| `__aenter__/__aexit__` | N/A | ✅ | ✅ | |
| `commit()` | ✅ | ✅ | ✅ | |
| `rollback()` | ✅ | ✅ | ✅ | |
| Uses `begin_transaction()` | ✅ | ✅ | ✅ | |
| Uses `end_transaction()` | ✅ | ✅ | ✅ | |

---

## Summary of Parity Issues

### Critical (Breaking Functionality)

| Issue | Location | Priority |
|-------|----------|----------|
| AsyncSQLerDB `__aenter__` doesn't use transaction tracking | async_db.py:361-375 | HIGH |
| SoftDeleteMixin not async-compatible | mixins.py | HIGH |
| `delete_with_policy` only supports "restrict" in async | async_model.py | MEDIUM |

### Important (Missing Features)

| Issue | Location | Priority |
|-------|----------|----------|
| AsyncSQLerQuerySet missing `or_filter()` | async_queryset.py | MEDIUM |
| AsyncSQLerQuerySet missing `distinct()` | async_queryset.py | MEDIUM |
| AsyncSQLerQuerySet missing `select()` | async_queryset.py | MEDIUM |
| AsyncSQLerQuerySet missing `distinct_values()` | async_queryset.py | MEDIUM |
| AsyncSQLerQuerySet missing `update()` | async_queryset.py | HIGH |
| AsyncSQLerQuerySet missing `delete_all()` | async_queryset.py | HIGH |
| AsyncSQLerQuery missing `explain()` | async_query.py | LOW |
| AsyncSQLerQuery missing `explain_query_plan()` | async_query.py | LOW |
| RebaseConfig not supported in async | async_safe.py | MEDIUM |
| AsyncSQLerModel missing `db()` classmethod | async_model.py | LOW |
| AsyncSQLerModel missing `_snapshot` in base | async_model.py | LOW |
| Integrity helpers missing in async | async_model.py | LOW |

### Minor (Different Implementation)

| Issue | Location | Notes |
|-------|----------|-------|
| `timeout_ms` parameter in adapter | asynchronous.py | Not applicable to aiosqlite |
| Memory singleton mode | asynchronous.py | Different concurrency model |
| `execute_sql` column detection | async_db.py | Different cursor behavior |
| Row count detection | async_query.py | Uses `changes()` instead of `rowcount` |

---

## Recommended Fixes (Priority Order)

### Phase 1: Critical Fixes
1. Fix `AsyncSQLerDB.__aenter__/__aexit__` to use `begin_transaction()`/`end_transaction()`
2. Create `AsyncSoftDeleteMixin` with proper async methods

### Phase 2: Feature Parity
3. Add `or_filter()`, `distinct()`, `select()` to `AsyncSQLerQuerySet`
4. Add `distinct_values()` to `AsyncSQLerQuerySet`
5. Add `update()` and `delete_all()` to `AsyncSQLerQuerySet`
6. Add `explain()` and `explain_query_plan()` to `AsyncSQLerQuery`

### Phase 3: Advanced Features
7. Add `RebaseConfig` support to `AsyncSQLerSafeModel`
8. Implement full `delete_with_policy` for async (set_null, cascade)
9. Add async integrity helpers

---

*Generated: 2024*
*Last Updated: After transaction-aware async adapter fix*
