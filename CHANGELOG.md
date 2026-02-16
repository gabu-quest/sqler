# Changelog

All notable changes to sqler are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/).

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
