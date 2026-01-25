# SQLer Lite Tours - Pyodide/WASM Compatible

These interactive notebooks teach SQLer using **dataclass-based models** that work in browser environments like Pyodide and marimo WASM.

## Why Lite?

Standard SQLer uses Pydantic for type validation, but Pydantic 2.x requires `pydantic-core` (a Rust extension) which cannot run in WebAssembly environments. SQLer Lite provides the same ORM features using Python's standard `dataclasses` module.

## Notebooks

| Tour | Topic | What You'll Learn |
|------|-------|-------------------|
| [01](./tour_01_fundamentals_lite.py) | Fundamentals | Database setup, CRUD operations, queries |
| [02](./tour_02_relationships_lite.py) | Relationships | Model references, hydration |
| [03](./tour_03_safe_models_lite.py) | Safe Models | Optimistic locking, version control |
| [04](./tour_04_transactions_lite.py) | Transactions | Atomic operations, rollback |

## Running Locally

```bash
# Run any notebook with marimo
uv run marimo edit examples/lite/tour_01_fundamentals_lite.py
```

## Running in Browser (WASM)

These notebooks can run in marimo's WASM playground without any server:

1. Go to [marimo.app](https://marimo.app)
2. Upload a notebook file
3. The notebook runs entirely in your browser!

## Key Differences from Pydantic Version

```python
# Pydantic version
from sqler import SQLerModel

class User(SQLerModel):
    _table = "users"
    name: str
    email: str

# Lite version (Pyodide compatible)
from dataclasses import dataclass
from sqler import SQLerLiteModel

@dataclass
class User(SQLerLiteModel):
    __tablename__ = "users"
    name: str
    email: str
```

| Feature | Pydantic | Lite |
|---------|----------|------|
| Decorator | None | `@dataclass` |
| Base class | `SQLerModel` | `SQLerLiteModel` |
| Table name | `_table` | `__tablename__` |
| Safe model | `SQLerSafeModel` | `SQLerLiteSafeModel` |
| Validation | Runtime type checking | Type hints only |

## What Works in Lite Mode

- All CRUD operations (save, delete, from_id, all)
- Query builder with F() expressions
- Relationships between models
- Optimistic locking (_version)
- Transactions
- Dirty tracking

## What Requires Pydantic

- Mixins (TimestampMixin, SoftDeleteMixin, etc.)
- Runtime type validation
- JSON Schema generation
- Field validators
