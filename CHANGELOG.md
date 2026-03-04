# Changelog

All notable changes to sqler are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/).

---

## [1.2026.3.4] - 2026-03-04

### Added

- **`SQLerMsgspecModel`** — high-performance model variant using msgspec Structs for 2x faster hydration on bulk reads
- **`queryset.as_dicts()`** — bypass Pydantic hydration entirely, return raw dicts for maximum throughput
- **`update_n(n, **fields)`** — atomic batch updates with `RETURNING` clause
- **`save_many()` / `asave_many()`** — batch INSERT with auto-chunking for fast bulk creation
- **Per-call DB binding** — `model.save(db=other_db)`, `model.delete(db=other_db)` for multi-database workflows
- **`Model.using(db)`** — context manager for temporary DB binding on any model class
- **SQL injection prevention** — field name validation (`validate_field_name`) across all query paths, DDL identifier validation
- **FTS security** — validate index names, field names, and tokenizer parameters; parameterize highlight/snippet tags
- **`execute_sql()` restriction** — limited to SELECT/EXPLAIN/PRAGMA/WITH statements only
- **Async resource safety** — `try/finally` around all cursor operations in `async_db.py`
- **Promoted column rewriting** — `delete()` and aggregate queries now correctly rewrite WHERE clauses for promoted columns
- **Connection pool for async adapter** — `using()` per-query DB binding in async context
- **Memory profiler** — `benchmarks.memprofile` with multi-run stats, setup/run split, regression detection
- **Benchmark v1.2** — complete rewrite with matched PRAGMAs, fixed SQL asymmetries, 4-arm charts, sqlite3 baselines for all 22 scenarios

### Changed

- **`set_db()` soft-deprecated** — emits `DeprecationWarning`; use `Model.using(db)` or pass `db=` to instance methods instead
- **Bulk insert rewritten** — `bulk_upsert()` now uses chunked multi-row INSERT for better throughput
- **FTS `search_ranked()`** — rewritten to single-JOIN query (was subquery + Python sort)
- **Query timing** — guarded behind `logger.enabled` check to eliminate overhead when not logging
- **JSON path queries** — use `json_each(data, path)` instead of `json_each(json_extract())` for cleaner SQL
- **Export functions** — bypass Pydantic hydration for faster CSV/JSON/JSONL export
- **Lite models** — now support `using()` and `set_db()` deprecation warnings

### Fixed

- `F("_id")` and `F("_version")` now rewrite to real SQLite columns instead of JSON paths
- Async cursor leak in `async_export_jsonl`
- Datetime serialization in `_adump_with_relations()` for async models
- Mixin `save()`/`delete()` overrides now forward `db=` parameter correctly
- Connection pool releases connections after read operations
- Benchmark fairness: 18 methodology issues fixed (deserialization asymmetry, connection handling, SQL patterns)

---

## [1.2026.2.1] - 2026-02-01

### Added

- **Benchmark suite v1.1** — 22 scenarios across 5 suites with sqlite3 baselines
- **Lite model tours** — 11 WASM-compatible marimo notebooks for browser-based learning
- **Japanese tours** — all 11 tours translated

---

## [1.2026.1.7] - 2026-01-07

### Added

- **Transaction-aware saves** — `model.save()` now respects explicit transactions; saves inside `with db.transaction():` are rolled back properly on error
- **Soft delete convenience methods** — `SoftDeleteMixin` class methods: `active()`, `only_deleted()`, `with_deleted()`
- **Extended query builder** — new field operations: `between`, `is_null`, `is_not_null`, `startswith`, `endswith`, `glob`, `in_list`
- **NULL-safe comparisons** — `F("field") == None` generates correct `IS NULL` (not `= NULL`)
- **Configurable intent rebasing** — `RebaseConfig`, `PERMISSIVE_REBASE_CONFIG`, `NO_REBASE_CONFIG` for automatic conflict resolution in safe models
- **Auto-calling lifecycle hooks** — `HooksMixin` automatically invokes `before_save`/`after_save` in `save()` and `delete()`
- **Index management** — `list_indexes()`, `index_exists()` for programmatic index introspection
- **Smart table naming** — proper English pluralization: `Category` -> `categories`, `Box` -> `boxes`, `Company` -> `companies`
- **Query caching** — `QueryCache` with TTL, LRU eviction, pattern/table invalidation, and `@cached_query` decorator
- **Data export/import** — CSV, JSON, JSONL formats with sync + async support, streaming, and transform hooks
- **Full-text search** — FTS5-based search via `FTSIndex` and `SearchableMixin`, with ranked results and highlighting
- **Connection pooling** — `PooledSQLerDB` for high-concurrency scenarios with pool stats
- **Schema migrations** — `MigrationRunner` with versioned up/down migrations (sync + async)
- **Metrics collection** — query metrics with histogram, Prometheus export, and custom callbacks
- **Database operations** — `backup()`, `restore()`, `health_check()`, `vacuum()`, `checkpoint()` (sync + async)
- **Change tracking** — `TrackedModel` for dirty field detection, `DiffMixin` for instance comparison and cloning
- **Benchmark suite** — 22 scenarios across 5 suites with automated chart generation

### Changed

- `model.save()` inside an explicit transaction no longer auto-commits (breaking change for code that relied on immediate commit behavior inside transactions)
