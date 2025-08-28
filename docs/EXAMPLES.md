# Examples Cookbook

All examples use in-memory SQLite for speed and no side effects. Run with `uv`:

```bash
uv run python examples/01_quickstart_sync.py
```

## 01 — Quickstart (sync)
- File: `examples/01_quickstart_sync.py`
- Defines a model, saves rows, queries with a filter and ordering.

## 02 — Querying (arrays)
- File: `examples/02_queries.py`
- `contains`, `isin`, and `.any()` for arrays and arrays of objects.

## 03 — Relationships
- File: `examples/03_relationships.py`
- Save refs (child first), filter via `SQLerModelField` and `User.ref(...).field(...)`, hydration toggle with `.resolve(False)`.

### Scoped any().where(...)

Filter within a specific element of an array-of-objects by scoping a mid-chain predicate:

```python
from sqler.query import SQLerField as F

# Match rows where any read has note == 'good' and, for that read, any mass.val > 10
expr = F(["reads"]).any().where(F(["note"]) == "good")["masses"].any()["val"] > 10
```

## 04 — Safe Models (optimistic locking)
- File: `examples/04_safe_models.py`
- Demonstrates `_version` bump and `StaleVersionError`.

## 05 — Async Quickstart
- File: `examples/05_async_quickstart.py`
- Async DB + model; query chaining with `await`.

## 06 — Indexes + Explain
- File: `examples/06_indexes_and_explain.py`
- Ensuring an index and inspecting the plan with `EXPLAIN QUERY PLAN`.

## 07 — FastAPI App
- Files: `examples/fastapi/app.py`, `examples/fastapi/models.py`, `examples/fastapi/db.py`
- Run:

```bash
uv run uvicorn examples.fastapi.app:app --reload
```
