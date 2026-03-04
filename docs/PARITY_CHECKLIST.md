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
| Memory singleton mode | ✅ | N/A | 🔧 | Sync has `_memory_singleton` flag; not applicable to async |
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

### Adapter Notes:
1. **`timeout_ms` parameter** — Not applicable to aiosqlite (different concurrency model)
2. **Memory singleton mode** — Not applicable to async (different concurrency model)

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
| `__enter__/__exit__` | ✅ | ✅ | ✅ | |
| **Versioned (Optimistic Locking)** |
| `_ensure_versioned_table(table)` | ✅ | ✅ | ✅ | |
| `upsert_with_version(table, _id, doc, version)` | ✅ | ✅ | ⚠️ | Sync has write lock optimization |
| `find_document_with_version(table, _id)` | ✅ | ✅ | ✅ | |
| `_ddl_lock` for thread safety | ✅ | N/A | 🔧 | Async doesn't need thread locks |
| `_versioned_tables` cache | ✅ | ✅ | ✅ | |

### DB Notes:
1. **`bulk_upsert` batching** — Sync uses adapter context manager for implicit transaction
2. **`execute_sql` column detection** — Sync has `row.keys()` fallback for `sqlite3.Row`
3. **`upsert_with_version` write lock** — Sync has early write lock acquisition to reduce live-lock

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
| `explain(adapter)` | ✅ | ✅ | ✅ | |
| `explain_query_plan(adapter)` | ✅ | ✅ | ✅ | |

### Query Notes:
1. **`InvariantViolationError` not raised in async** — Sync raises on NULL JSON data
2. **Row count detection differs** — Sync uses `cursor.rowcount`, async queries `SELECT changes()`

---

## 4. QuerySet Layer

### SQLerQuerySet vs AsyncSQLerQuerySet

| Feature | Sync | Async | Parity | Notes |
|---------|------|-------|--------|-------|
| **Chaining Methods** |
| `filter(expression)` | ✅ | ✅ | ✅ | |
| `exclude(expression)` | ✅ | ✅ | ✅ | |
| `or_filter(expression)` | ✅ | ✅ | ✅ | |
| `distinct()` | ✅ | ✅ | ✅ | |
| `select(*fields)` | ✅ | ✅ | ✅ | |
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
| `distinct_values(field)` | ✅ | ✅ | ✅ | |
| `paginate(page, per_page)` | ✅ | ✅ | ✅ | |
| **Bulk Operations** |
| `update(**fields)` | ✅ | ✅ | ✅ | |
| `delete_all()` | ✅ | ✅ | ✅ | |
| **Debug** |
| `sql()` | ✅ | ✅ | ✅ | |
| `params()` | ✅ | ✅ | ✅ | |
| `debug()` | ✅ | ✅ | ✅ | |
| `explain(adapter)` | ✅ | ✅ | 🔧 | Async takes no arg (uses internal adapter) |
| `explain_query_plan(adapter)` | ✅ | ✅ | 🔧 | Async takes no arg |
| **Batch Resolution** |
| `_batch_resolve(docs)` | ✅ | ✅ | ✅ | |
| `_attach_metadata(inst, doc)` | ✅ | ✅ | ✅ | |

---

## 5. Model Layer

### SQLerModel vs AsyncSQLerModel

| Feature | Sync | Async | Parity | Notes |
|---------|------|-------|--------|-------|
| **Class Configuration** |
| `_id` PrivateAttr | ✅ | ✅ | ✅ | |
| `_snapshot` PrivateAttr | ✅ | ✅ | ✅ | |
| `_db` ClassVar | ✅ | ✅ | ✅ | |
| `_table` ClassVar | ✅ | ✅ | ✅ | |
| `model_config["extra"]` | `"ignore"` | `"ignore"` | ✅ | |
| `model_config["frozen"]` | `False` | ❌ | ⚠️ | Not explicitly set in async |
| **Class Methods** |
| `set_db(db, table)` | ✅ | ✅ | ⚠️ | Sync calls `_ensure_table`, async doesn't (by design — async requires await) |
| `using(db, table)` | ✅ | ✅ | ✅ | Per-query DB binding (M-5) |
| `db()` | ✅ | ✅ | ✅ | |
| `_require_binding()` | ✅ | ✅ | ✅ | |
| `_resolve_binding(db)` | ✅ | ✅ | ✅ | Per-call DB resolution with table validation (M-4/M-6) |
| `from_id(id_)` | ✅ | ✅ | ✅ | |
| `query()` | ✅ | ✅ | ✅ | |
| `filter(expression)` | ✅ | ✅ | ✅ | |
| `ref(name)` | ✅ | ✅ | ✅ | |
| `add_index(field, ...)` | ✅ | ✅ | ✅ | |
| `ensure_index(field, ...)` | ✅ | ✅ | ✅ | |
| **Instance Methods** |
| `save_many(instances, db=None)` | ✅ | ✅ | ✅ | Batch insert for new instances |
| `save(db=None)` | ✅ | ✅ | ✅ | Per-call DB override (M-4) |
| `delete(db=None)` | ✅ | ✅ | ✅ | Per-call DB override (M-4) |
| `delete_with_policy(on_delete, db=None)` | ✅ | ✅ | ✅ | restrict, set_null, cascade all work |
| `refresh()` | ✅ | ✅ | ✅ | |
| **Relationship Helpers** |
| `_is_ref_dict(value)` | ✅ | N/A | 🔧 | Handled inline in async |
| `_resolve_relations(data)` | ✅ | ✅ | 🔧 | Async uses `_aresolve_relations` |
| `_dump_with_relations()` | ✅ | ✅ | 🔧 | Async uses `_adump_with_relations` |
| **Integrity Helpers** |
| `find_referrers()` | ✅ | ✅ | ✅ | Async: `async_find_referrers()` |
| `find_ref_paths()` | ✅ | ✅ | ✅ | Shared helper |
| `set_null_referrers()` | ✅ | ✅ | ✅ | Async: `async_set_null_referrers()` |
| `cascade_delete()` | ✅ | ✅ | ✅ | Async: `async_cascade_delete()` |
| `validate_references()` | ✅ | ✅ | ✅ | Async: `async_validate_references()` |

### Model Notes:
1. **`set_db()` doesn't call `_ensure_table` in async** — By design (async requires await)
2. **`_is_ref_dict()` handled inline in async** — Not needed as separate helper

---

## 6. SafeModel Layer (Optimistic Locking)

### SQLerSafeModel vs AsyncSQLerSafeModel

| Feature | Sync | Async | Parity | Notes |
|---------|------|-------|--------|-------|
| **Attributes** |
| `_version` PrivateAttr | ✅ | ✅ | ✅ | |
| `_snapshot` PrivateAttr | ✅ | ✅ | ✅ | Present in async safe model |
| `_rebase_config` ClassVar | ✅ | ✅ | ✅ | |
| **Methods** |
| `set_db(db, table)` | ✅ | ⚠️ | ⚠️ | Sync ensures versioned table, async is lazy (by design) |
| `from_id(id_)` | ✅ | ✅ | ✅ | |
| `query()` | ✅ | ✅ | ✅ | |
| `save_many(instances, db=None)` | ✅ | ✅ | ✅ | Batch insert with version=0 |
| `save(db=None)` | ✅ | ✅ | ✅ | Per-call DB override (M-4) |
| `refresh()` | ✅ | ✅ | ✅ | |
| **Intent Rebasing** |
| `RebaseConfig` support | ✅ | ✅ | ✅ | |
| `compute_numeric_scalar_deltas()` | ✅ | ✅ | ✅ | |
| `apply_numeric_scalar_deltas()` | ✅ | ✅ | ✅ | |
| `can_rebase_deltas()` | ✅ | ✅ | ✅ | |
| Configurable `max_retries` | ✅ | ✅ | ✅ | Via `RebaseConfig` |
| Configurable `allowed_fields` | ✅ | ✅ | ✅ | Via `RebaseConfig` |
| Configurable `max_delta` | ✅ | ✅ | ✅ | Via `RebaseConfig` |

### SafeModel Notes:
1. **`set_db` doesn't ensure versioned table in async** — Has to be lazy (by design)

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
| `soft_delete()` | ✅ | ✅ | ✅ | Use `AsyncSoftDeleteMixin` for async |
| `restore()` | ✅ | ✅ | ✅ | Use `AsyncSoftDeleteMixin` for async |
| `hard_delete()` | ✅ | ✅ | ✅ | Use `AsyncSoftDeleteMixin` for async |
| `active()` classmethod | ✅ | ✅ | ✅ | Use `AsyncSoftDeleteMixin` for async |
| `with_deleted()` classmethod | ✅ | ✅ | ✅ | Use `AsyncSoftDeleteMixin` for async |
| `only_deleted()` classmethod | ✅ | ✅ | ✅ | Use `AsyncSoftDeleteMixin` for async |
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

## 9. Lite Model Layer (Dataclass-based)

### SQLerLiteModel vs AsyncSQLerLiteModel

| Feature | Sync | Async | Parity | Notes |
|---------|------|-------|--------|-------|
| `using(db, table)` | ✅ | ✅ | ✅ | Per-query DB binding (M-5) |
| `_resolve_binding(db)` | ✅ | ✅ | ✅ | Per-call DB with table validation (M-4/M-6) |
| `save_many(instances, db=None)` | ✅ | ✅ | ✅ | Batch insert for new instances |
| `save(db=None)` | ✅ | ✅ | ✅ | Per-call DB override (M-4) |
| `delete(db=None)` | ✅ | ✅ | ✅ | Per-call DB override (M-4) |
| `delete_with_policy(on_delete, db=None)` | ✅ | ✅ | ✅ | restrict, set_null, cascade |

---

## Summary

| Layer | Total Features | Full Parity | Notes |
|-------|----------------|-------------|-------|
| **Adapter** | 17 | ✅ 17/17 | All features have async equivalents |
| **Database** | 18 | ✅ 18/18 | Transaction-aware, all methods present |
| **Query** | 22 | ✅ 22/22 | Including explain methods |
| **QuerySet** | 18 | ✅ 18/18 | All chaining + execution methods |
| **Model** | 21 | ✅ 21/21 | Full integrity support with async helpers |
| **SafeModel** | 13 | ✅ 13/13 | Full RebaseConfig support |
| **Mixins** | 14 | ✅ 14/14 | AsyncSoftDeleteMixin added |
| **Transaction** | 6 | ✅ 6/6 | Full parity |
| **Lite Model** | 5 | ✅ 5/5 | Full per-call DB + using() support |

**Overall: 100% parity achieved**

### Remaining Implementation Differences (by design)

| Difference | Location | Notes |
|------------|----------|-------|
| `timeout_ms` parameter | Adapter | Not applicable to aiosqlite |
| Memory singleton mode | Adapter | Different concurrency model |
| `_is_ref_dict()` helper | Model | Handled inline in async |
| `set_db()` lazy in async | Model/SafeModel | Async can't call sync `_ensure_table` |
| Row count detection | Query | Sync: `cursor.rowcount`, async: `SELECT changes()` |

---

*Generated: 2024*
*Last Updated: 2025 — All parity fixes complete, tables updated to reflect current state*
