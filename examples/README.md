# SQLer Examples

This directory contains examples demonstrating SQLer's features.

## Interactive Tours (Marimo Notebooks)

Comprehensive walkthroughs using [marimo](https://marimo.io) reactive notebooks:

| Tour | Topic | Description |
|------|-------|-------------|
| `tour_01_fundamentals.py` | Basics | Creating databases, models, CRUD operations |
| `tour_02_relationships.py` | Relationships | Foreign keys, one-to-many, many-to-many |
| `tour_03_safe_models.py` | Safe Models | Optimistic locking, version control |
| `tour_04_transactions.py` | Transactions | ACID guarantees, rollback handling |
| `tour_05_mixins.py` | Mixins | Timestamps, soft delete, audit trails |
| `tour_06_advanced.py` | Advanced | Custom fields, JSON operations, raw SQL |
| `tour_07_export_import.py` | Export/Import | JSON/CSV serialization, data migration |
| `tour_08_fulltext_search.py` | Full-Text Search | FTS5 integration, search ranking |
| `tour_09_change_tracking.py` | Change Tracking | Dirty fields, change history |
| `tour_10_db_operations.py` | DB Operations | Backup, vacuum, integrity checks |
| `tour_11_metrics_caching.py` | Metrics & Caching | Query stats, result caching |

**Run a tour:**
```bash
uv run marimo edit examples/tour_01_fundamentals.py
```

## Quick Reference Scripts

Focused standalone examples that run without marimo:

| Script | Topic |
|--------|-------|
| `01_quickstart_sync.py` | Basic sync usage |
| `02_queries.py` | Query builder patterns |
| `03_relationships.py` | Model relationships |
| `04_safe_models.py` | Optimistic locking |
| `05_async_quickstart.py` | Async adapter usage |
| `06_indexes_and_explain.py` | Index optimization |
| `07_pagination.py` | Paginated queries |
| `08_mixins.py` | Built-in mixins |
| `09_aggregates_and_bulk.py` | Bulk operations |

**Run a script:**
```bash
uv run python examples/01_quickstart_sync.py
```

## FastAPI Demo

Full-stack web application in `fastapi/`:

- REST API with SQLer models
- **Vue 3 + Naive UI** frontend (pre-built, zero setup)
- Optimistic locking with ETags
- Soft delete, audit trails, change tracking
- i18n support (English/Japanese)

**Run immediately:**
```bash
uv run python -m examples.fastapi.app --auto-port
```

See [`fastapi/README.md`](fastapi/README.md) for details.

## Japanese Translations

All tour notebooks translated to Japanese in `ja/`:

```bash
uv run marimo edit examples/ja/tour_01_fundamentals.py
```

## WASM/Lite Versions

Browser-compatible examples in `lite/` for running SQLer in Pyodide/WASM:

- `tour_01_fundamentals_lite.py`
- `tour_02_relationships_lite.py`
- `tour_03_safe_models_lite.py`
- `tour_04_transactions_lite.py`
