# SQLer

**English | [日本語はこちら](README.ja.md)**

[![PyPI version](https://img.shields.io/pypi/v/sqler)](https://pypi.org/project/sqler/)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
[![Tests](https://github.com/gabu-quest/SQLer/actions/workflows/ci.yml/badge.svg)](https://github.com/gabu-quest/SQLer/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**A lightweight, JSON-first micro-ORM for SQLite (sync + async).**
Define Pydantic-style models, persist them as JSON, and query with a fluent API — with optional _safe models_ that enforce optimistic versioning.

---

## Why SQLer?

This started as a personal toolkit for **very fast prototyping** — small scripts that made it effortless to sketch data models, shove them into SQLite as JSON, and iterate. The result became SQLer: a tidy, dependency-light package that keeps that prototyping speed, but adds the pieces you need for real projects (indexes, relationships, integrity policies, and honest concurrency).

---

## Features

- **Document-style models** backed by SQLite JSON1
- **Fluent query builder**: `filter`, `exclude`, `contains`, `isin`, `.any().where(...)`
- **Relationships** with simple reference storage and hydration
- **Safe models** with `_version` and optimistic locking (stale writes raise)
- **Bulk operations** (`bulk_upsert`)
- **Integrity policies** on delete: `restrict`, `set_null`, `cascade`
- **Raw SQL escape hatch** (parameterized), with model hydration when returning `_id, data`
- **Sync & Async** APIs with matching semantics
- **WAL-friendly concurrency** via thread-local connections (many readers, one writer)
- **Opt-in perf tests** and practical indexing guidance

---

## Install

```bash
pip install sqler
```

Requires Python **3.12+** and SQLite with JSON1 (bundled on most platforms).

---

## Quickstart (Sync)

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class User(SQLerModel):
    name: str
    age: int

db = SQLerDB.on_disk("app.db")
User.set_db(db)  # binds model to table "users" (override with table="...")

# Create / save
u = User(name="Alice", age=30)
u.save()
print(u._id)  # assigned _id

# Query
adults = User.query().filter(F("age") >= 18).order_by("age").all()
print([a.name for a in adults])

db.close()
```

---

## Quickstart (Async)

```python
import asyncio
from sqler import AsyncSQLerDB, AsyncSQLerModel
from sqler.query import SQLerField as F

class AUser(AsyncSQLerModel):
    name: str
    age: int

async def main():
    db = AsyncSQLerDB.in_memory()
    await db.connect()
    AUser.set_db(db)

    u = AUser(name="Ada", age=36)
    await u.save()

    adults = await AUser.query().filter(F("age") >= 18).order_by("age").all()
    print([a.name for a in adults])

    await db.close()

asyncio.run(main())
```

---

## Safe Models & Optimistic Versioning

Use `SQLerSafeModel` when you need concurrency safety. New rows start with `_version = 0`. Updates require the in-memory `_version`; on success it bumps by 1. If the row changed underneath you, a `StaleVersionError` is raised.

```python
from sqler import SQLerDB, SQLerSafeModel, StaleVersionError

class Account(SQLerSafeModel):
    owner: str
    balance: int

db = SQLerDB.on_disk("bank.db")
Account.set_db(db)

acc = Account(owner="Ada", balance=100)
acc.save()                 # _version == 0

acc.balance = 120
acc.save()                 # _version == 1

# Simulate concurrent change
db.adapter.execute("UPDATE accounts SET _version = _version + 1 WHERE _id = ?;", [acc._id])
db.adapter.commit()

# This write is stale → raises
try:
    acc.balance = 130
    acc.save()
except StaleVersionError:
    acc.refresh()          # reloads both fields and _version
```

---

## Relationships

Store references to other models and hydrate them on load/refresh.

```python
from sqler import SQLerDB, SQLerModel

class Address(SQLerModel):
    city: str
    country: str

class User(SQLerModel):
    name: str
    address: Address | None = None

db = SQLerDB.in_memory()
Address.set_db(db); User.set_db(db)

home = Address(city="Kyoto", country="JP"); home.save()
user = User(name="Alice", address=home);   user.save()

u = User.from_id(user._id)
print(u.address.city)  # "Kyoto"
```

**Filtering by referenced fields**

```python
from sqler.query import SQLerField as F
# Address city equals "Kyoto"
q = User.query().filter(F(["address","city"]) == "Kyoto")
```

---

## Query Builder

- **Fields:** `F("age")`, `F(["items","qty"])`
- **Predicates:** `==`, `!=`, `<`, `<=`, `>`, `>=`, `contains`, `isin`
- **Boolean ops:** `&` (AND), `|` (OR), `~` (NOT)
- **Exclude:** invert a predicate set
- **Arrays:** `.any()` and scoped `.any().where(...)`

```python
from sqler.query import SQLerField as F

# containments
q1 = User.query().filter(F("tags").contains("pro"))

# membership
q2 = User.query().filter(F("tier").isin([1, 2]))

# exclude
q3 = User.query().exclude(F("name").like("test%")).order_by("name")

# arrays of objects
expr = F(["items"]).any().where((F("sku") == "ABC") & (F("qty") >= 2))
q4 = Order.query().filter(expr)
```

**Debug & explain**

```python
sql, params = User.query().filter(F("age") >= 18).debug()
plan = User.query().filter(F("age") >= 18).explain_query_plan(User.db().adapter)
```

---

## Data Integrity

### Delete Policies (`restrict`, `set_null`, `cascade`)

Control how deletions affect JSON references in related rows.

- `restrict` (default): prevent deletion if anything still references the row
- `set_null`: null out the JSON field that holds the reference (field must be nullable)
- `cascade`: recursively delete referrers (depth-first, cycle-safe)

```python
# Prevent delete if posts still reference the user
user.delete_with_policy(on_delete="restrict")

# Null-out JSON refs before deleting
post.delete_with_policy(on_delete="set_null")
user.delete_with_policy(on_delete="restrict")

# Cascade example (pseudo)
user.delete_with_policy(on_delete=("cascade", {"Post": "author"}))
```

### Reference Validation

Detect orphans proactively:

```python
broken = Post.validate_references({"author": ("users", "id")})
if broken:
    for table, rid, ref in broken:
        print("Broken ref:", table, rid, "→", ref)
```

---

## Bulk Operations

Write many documents efficiently.

```python
rows = [{"name": "A"}, {"name": "B"}, {"_id": 42, "name": "C"}]
ids = db.bulk_upsert("users", rows)   # returns list of _ids in input order
```

Notes:

- If SQLite supports `RETURNING`, SQLer uses it; otherwise a safe fallback is used.
- For sustained heavy writes, favor a single-process writer (SQLite has a single writer at a time).

---

## Advanced Usage

### Raw SQL (`execute_sql`)

Run parameterized SQL. To hydrate models later, return `_id` and `data` columns.

```python
rows = db.execute_sql("""
  SELECT u._id, u.data
  FROM users u
  WHERE json_extract(u.data,'$.name') LIKE ?
""", ["A%"])
```

### Indexes (JSON paths)

Build indexes for fields you filter/sort on.

```python
# DB-level
db.create_index("users", "age")  # -> json_extract(data,'$.age')
db.create_index("users", "email", unique=True)
db.create_index("users", "age", where="json_extract(data,'$.age') IS NOT NULL")
```

For relationships, consider indexes on reference paths:

```python
db.create_index("users", "address._id")
db.create_index("users", "address.city")
```

---

## Concurrency Model (WAL)

- SQLer uses **thread-local connections** and enables **WAL**:

  - `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`
  - Many readers in parallel; one writer (SQLite rule)

- **Safe models** perform optimistic writes:

  ```sql
  UPDATE ... SET data=json(?), _version=_version+1
  WHERE _id=? AND _version=?;
  ```

  If no rows match, a `StaleVersionError` is raised.

- Under bursts, SQLite may report “database is locked”. SQLer uses `BEGIN IMMEDIATE` and a small backoff to reduce thrash.
- `refresh()` always re-hydrates `_version`.

**HTTP mapping (FastAPI)**

```python
from fastapi import HTTPException
from sqler.models import StaleVersionError

try:
    obj.save()
except StaleVersionError:
    raise HTTPException(409, "Version conflict")
```

---

## Performance Tips

- Index hot JSON paths (e.g., `users.age`, `orders.items.sku`)
- Batch writes with `bulk_upsert`
- For heavy write loads, serialize writes via one process / queue
- Perf suite is opt-in:

  ```bash
  pytest -q -m perf
  pytest -q -m perf --benchmark-save=baseline
  pytest -q -m perf --benchmark-compare=baseline
  ```

---

## Errors

- `StaleVersionError` — optimistic check failed (HTTP 409)
- `InvariantViolationError` — malformed row invariant (e.g., NULL JSON)
- `NotConnectedError` — adapter closed / not connected
- SQLite exceptions (`sqlite3.*`) bubble with context

---

## Examples

See `examples/` for end-to-end scripts:

- `sync_model_quickstart.py`
- `sync_safe_model.py`
- `async_model_quickstart.py`
- `async_safe_model.py`
- `model_arrays_any.py`

Run:

```bash
uv run python examples/sync_model_quickstart.py
```

### Running the FastAPI Example

SQLer ships with a minimal FastAPI demo under `examples/fastapi/app.py`.

To run it:

```bash
pip install fastapi uvicorn
uv run uvicorn examples.fastapi.app:app --reload
```

---

## Testing

```bash
# Unit
uv run pytest -q

# Perf (opt-in)
uv run pytest -q -m perf
```

---

## Contributing

- Format & lint:

  ```bash
  uv run ruff format .
  uv run ruff check .
  ```

- Tests:

  ```bash
  uv run pytest -q --cov=src --cov-report=term-missing
  ```

Issue templates and guidelines live in `CONTRIBUTING.md`. PRs welcome.

---

## License

MIT © Contributors
