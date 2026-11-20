# SQLer

**English | [日本語はこちら](README.ja.md)**

[![PyPI version](https://img.shields.io/pypi/v/sqler)](https://pypi.org/project/sqler/)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
[![Tests](https://github.com/gabu-quest/sqler/actions/workflows/ci.yml/badge.svg)](https://github.com/gabu-quest/sqler/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

sqler stores Python objects as JSON documents in SQLite and lets you query inside them. You write a model class; sqler keeps one table per model, one row per object, and builds the `json_extract` SQL for you. Sync and async, one file on disk, no server.

## Install

```bash
pip install 'sqler[pydantic]'   # Pydantic models (the examples below)
pip install sqler               # dataclass models only; no other dependency
pip install 'sqler[msgspec]'    # msgspec models
```

Python 3.12 or newer. SQLite needs the JSON1 functions, which the SQLite bundled with Python has.

## Quick start

```python
from sqler import F, SQLerDB, SQLerModel

class User(SQLerModel):
    name: str
    age: int
    tags: list[str] = []

db = SQLerDB.on_disk("app.db")        # or SQLerDB.in_memory()

User(name="Alice", age=30, tags=["admin"]).save(db=db)
User(name="Bob", age=25, tags=["user"]).save(db=db)

admins = User.using(db).filter(F("tags").contains("admin")).all()
assert [u.name for u in admins] == ["Alice"]

young = User.using(db).filter(F("age") < 28).order_by("name").all()
assert [u.name for u in young] == ["Bob"]

db.close()
```

`save(db=db)` writes one object. `User.using(db)` starts a query. Nothing is bound to the class.

## What is in the file

The quick start leaves this in `app.db`, readable with any SQLite tool:

```sql
CREATE TABLE users (
    _id  INTEGER PRIMARY KEY AUTOINCREMENT,
    data JSON NOT NULL
);
-- 1 | {"name":"Alice","age":30,"tags":["admin"]}
-- 2 | {"name":"Bob","age":25,"tags":["user"]}
```

The table name is the lowercase plural of the class name, or `__tablename__` if you set it. `_id` is the object's `_id` attribute after a save. Safe models (below) add a `_version` column. A reference to another model is stored as `{"_table": "...", "_id": ...}`.

## Queries

```python
from sqler import F, SQLerDB, SQLerModel

class Order(SQLerModel):
    customer: dict
    items: list[dict] = []

db = SQLerDB.in_memory()
Order(customer={"name": "Ada", "city": "Kyoto"}, items=[{"sku": "A1", "qty": 2}]).save(db=db)
Order(customer={"name": "Bob", "city": "Osaka"}, items=[{"sku": "B7", "qty": 1}]).save(db=db)

# a nested field
kyoto = Order.using(db).filter(F("customer")["city"] == "Kyoto").all()
assert [o.customer["name"] for o in kyoto] == ["Ada"]

# any element of an array
bulk = Order.using(db).filter(F(["items"]).any().where(F("qty") > 1)).all()
assert [o.customer["name"] for o in bulk] == ["Ada"]
```

Also: `exclude`, `order_by`, `paginate`, `count`, `first`, `isin`, `between`, `contains`. `debug()` and `explain_query_plan()` show the SQL and how SQLite runs it. A query on a JSON field scans the table unless you create an index for that field with `db.create_index(...)`.

## Async

The async classes mirror the sync ones.

```python
import asyncio
from sqler import AsyncSQLerDB, AsyncSQLerModel, F

class Job(AsyncSQLerModel):
    name: str
    done: bool = False

async def main():
    db = AsyncSQLerDB.on_disk("app.db")
    await db.connect()

    await Job(name="resize").save(db=db)
    await Job(name="email", done=True).save(db=db)

    pending = await Job.using(db).filter(F("done") == False).all()
    assert [j.name for j in pending] == ["resize"]

    await db.close()

asyncio.run(main())
```

## Three kinds of model

| Base class | Install | Use it when |
|---|---|---|
| `SQLerModel` | `sqler[pydantic]` | you want validation and Pydantic's serialization |
| `SQLerLiteModel` (a dataclass) | `sqler` | you want no dependency, or you run in Pyodide |
| `SQLerMsgspecModel` | `sqler[msgspec]` | decoding speed matters more than validation features |

```python
from dataclasses import dataclass
from sqler import SQLerDB
from sqler.models.lite import SQLerLiteModel

@dataclass
class User(SQLerLiteModel):
    __tablename__ = "users"
    name: str = ""
    age: int = 0

db = SQLerDB.in_memory()
User(name="Alice", age=30).save(db=db)
assert User.using(db).count() == 1
```

The query API is the same for all three.

## Safe models: two writers, one row

`SQLerSafeModel` keeps a version number per row. A save that started from an old version fails instead of overwriting.

```python
from sqler import SQLerDB, SQLerSafeModel, StaleVersionError

class Account(SQLerSafeModel):
    owner: str
    balance: int

db = SQLerDB.in_memory()
mine = Account(owner="Ada", balance=100).save(db=db)

theirs = Account.using(db).first()      # someone else loads the same row
theirs.balance = 50
theirs.save(db=db)

mine.balance = 120
try:
    mine.save(db=db)
except StaleVersionError:
    pass                                # reload, then decide
```

## Transactions

```python
with db.transaction():
    Account(owner="Bob", balance=50).save(db=db)
    raise RuntimeError("abort")         # the row is not written
```

## Relationships

A field typed as another model is stored as a reference and loaded as that model.

```python
from sqler import ReferentialIntegrityError, SQLerDB, SQLerModel

class Address(SQLerModel):
    city: str

class User(SQLerModel):
    name: str
    address: Address | None = None

db = SQLerDB.in_memory()
home = Address(city="Kyoto").save(db=db)
user = User(name="Alice", address=home).save(db=db)

assert User.from_id(user._id, db=db).address.city == "Kyoto"      # the reference is loaded

in_kyoto = User.using(db).filter(User.ref("address").field("city") == "Kyoto").all()
assert [u.name for u in in_kyoto] == ["Alice"]

try:
    home.delete(db=db)                  # Alice still refers to it
except ReferentialIntegrityError:
    pass
```

A delete is refused while another row refers to the object. `delete_with_policy(on_delete="cascade")` deletes the referring rows too; `on_delete="set_null"` clears their field.

## Full-text search

```python
from sqler import FTSIndex, SQLerDB, SQLerModel

class Note(SQLerModel):
    text: str

db = SQLerDB.in_memory()
Note(text="buy rice").save(db=db)

index = FTSIndex(Note, fields=["text"])     # SQLite FTS5
index.create(db)                            # indexes the rows already there
Note(text="cook rice").save(db=db)          # and follows every later write

assert sorted(n.text for n in index.search("rice")) == ["buy rice", "cook rice"]
```

## What sqler does not do

- **One writer at a time.** That is SQLite. Batch writes with `save_many()`, `bulk_upsert()` and transactions.
- **No JOINs.** A reference is loaded with a second query, not joined.
- **Fields are not columns.** Every field read goes through `json_extract`; an index on the field closes most of the gap.
- **Results are loaded into memory.** Use `paginate()` and `count()` on large tables.
- **One database engine.** For PostgreSQL or several engines, use SQLAlchemy. For plain relational SQLite work, use sqlite-utils.

## More

- Mixins for timestamps, soft delete and audit fields; lifecycle hooks; change tracking.
- Export and import: CSV, JSON, JSONL.
- Query caching, schema migrations, metrics.
- [API reference](docs/API.md) · [Examples](docs/EXAMPLES.md) · [Changelog](CHANGELOG.md)

## Interactive tour

Eleven [marimo](https://marimo.io) notebooks: **[open the tour](https://gabu-quest.github.io/sqler/)**. The dataclass versions run in the browser with nothing installed.

| Tour | Topics |
|------|--------|
| [01. Fundamentals](https://gabu-quest.github.io/sqler/lite/tour_01_fundamentals_lite/) | Models, CRUD, queries, aggregations |
| [02. Relationships](https://gabu-quest.github.io/sqler/lite/tour_02_relationships_lite/) | References, loading, cross-model queries |
| [03. Safe Models](https://gabu-quest.github.io/sqler/lite/tour_03_safe_models_lite/) | Optimistic locking, conflicts |
| [04. Transactions](https://gabu-quest.github.io/sqler/lite/tour_04_transactions_lite/) | Atomic operations, rollback |
| [05. Mixins](https://gabu-quest.github.io/sqler/lite/tour_05_mixins_lite/) | Timestamps, soft delete, hooks |
| [06. Advanced](https://gabu-quest.github.io/sqler/lite/tour_06_advanced_lite/) | Bulk operations, indexes, integrity, raw SQL |
| [07. Export/Import](https://gabu-quest.github.io/sqler/lite/tour_07_export_import_lite/) | CSV, JSON, JSONL |
| [08. Full-Text Search](https://gabu-quest.github.io/sqler/lite/tour_08_fulltext_search_lite/) | FTS5, boolean queries, ranking |
| [09. Change Tracking](https://gabu-quest.github.io/sqler/lite/tour_09_change_tracking_lite/) | Dirty checking, change detection |
| [10. Database Ops](https://gabu-quest.github.io/sqler/lite/tour_10_db_operations_lite/) | Health checks, stats, vacuum, logging |
| [11. Metrics & Caching](https://gabu-quest.github.io/sqler/lite/tour_11_metrics_caching_lite/) | Metrics, query caching |

To run the Pydantic versions locally: `uv sync --dev`, then `uv run marimo edit examples/tour_01_fundamentals.py`.

## The -ler family

| Package | What it is |
|---|---|
| **sqler** | This package: JSON documents in SQLite |
| [qler](https://github.com/gabu-quest/qler) | A background job queue that stores its jobs with sqler |
| [logler](https://github.com/gabu-quest/logler) | Log investigation; its `--db` option reads a sqler database file |

## Development

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run pytest -m "not perf"                                        # the whole suite; over twenty minutes
uv run python -m benchmarks run --scale medium --storage both      # benchmarks, with SQLite baselines
```

## License

MIT
