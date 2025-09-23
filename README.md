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

## Public API Contract

> Each subsection carries a **Contract ID**. The suite in `tests/test_readme.py` executes these snippets using only the documented public APIs. When the contract section changes, the tests must change with it — CI proves the README.

### [C01] Sync quickstart: models, save, query

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class Prefecture(SQLerModel):
    name: str
    region: str
    population: int
    foods: list[str] | None = None

class City(SQLerModel):
    name: str
    population: int
    prefecture: Prefecture | None = None

db = SQLerDB.in_memory()
Prefecture.set_db(db); City.set_db(db)

kyoto = Prefecture(name="Kyoto", region="Kansai", population=2_585_000, foods=["matcha","yudofu"]).save()
osaka = Prefecture(name="Osaka", region="Kansai", population=8_839_000, foods=["takoyaki"]).save()
shiga = Prefecture(name="Shiga", region="Kansai", population=1_413_000, foods=["funazushi"]).save()

City(name="Kyoto City", population=1_469_000, prefecture=kyoto).save()
City(name="Osaka City", population=2_750_000, prefecture=osaka).save()
City(name="Otsu", population=343_000, prefecture=shiga).save()

big = Prefecture.query().filter(F("population") > 1_000_000).order_by("population", desc=True).all()
assert [p.name for p in big][:2] == ["Osaka", "Kyoto"]
```

### [C02] Async quickstart (matching semantics)

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
    await AUser(name="Ada", age=36).save()
    adults = await AUser.query().filter(F("age") >= 18).order_by("age").all()
    assert any(u.name == "Ada" for u in adults)
    await db.close()

asyncio.run(main())
```

### [C03] Query builder: `.any().where(...)`

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class Order(SQLerModel):
    customer: str
    items: list[dict] | None = None

db = SQLerDB.in_memory()
Order.set_db(db)
Order(customer="C1", items=[{"sku":"RamenSet","qty":3}, {"sku":"Gyoza","qty":1}]).save()
Order(customer="C2", items=[{"sku":"RamenSet","qty":1}]).save()

expr = F(["items"]).any().where((F("sku") == "RamenSet") & (F("qty") >= 2))
hits = Order.query().filter(expr).all()
assert [h.customer for h in hits] == ["C1"]
```

### [C04] Relationships: hydration & cross-filtering

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
home = Address(city="Kyoto", country="JP").save()
user = User(name="Alice", address=home).save()

got = User.from_id(user._id)
assert got.address.city == "Kyoto"

qs = User.query().filter(User.ref("address").field("city") == "Kyoto")
assert any(row.name == "Alice" for row in qs.all())
```

### [C05] Index helpers, `debug()`, and `explain_query_plan()`

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

db = SQLerDB.in_memory()

class Prefecture(SQLerModel):
    name: str
    region: str
    population: int

Prefecture.set_db(db)
Prefecture(name="A", region="x", population=10).save()
Prefecture(name="B", region="x", population=2_000_000).save()

db.create_index("prefectures", "population")
Prefecture.ensure_index("population")

q = Prefecture.query().filter(F("population") >= 1_000_000)
sql, params = q.debug()
assert isinstance(sql, str) and isinstance(params, list)

plan = q.explain_query_plan(Prefecture.db().adapter)
assert plan and len(list(plan)) >= 1
```

### [C06] Safe models: optimistic versioning

```python
from sqler import SQLerDB, SQLerSafeModel, StaleVersionError

class Account(SQLerSafeModel):
    owner: str
    balance: int

db = SQLerDB.in_memory()
Account.set_db(db)

acc = Account(owner="Ada", balance=100).save()
acc.balance = 120
acc.save()

table = getattr(Account, "__tablename__", "accounts")
db.adapter.execute(
    f"UPDATE {table} SET data = json_set(data,'$._version', json_extract(data,'$._version') + 1) WHERE _id = ?;",
    [acc._id],
)
db.adapter.commit()

acc.balance = 130
try:
    acc.save()
except StaleVersionError:
    pass
else:
    raise AssertionError("stale writes must raise")
```

### [C07] `bulk_upsert` — one id per input, order preserved

```python
from sqler import SQLerDB, SQLerModel

class BU(SQLerModel):
    name: str
    age: int

db = SQLerDB.in_memory()
BU.set_db(db)

rows = [{"name":"A"}, {"name":"B"}, {"_id": 42, "name":"C"}]
ids = db.bulk_upsert("bus", rows)

assert ids[2] == 42
assert all(isinstance(i, int) and i > 0 for i in ids)
```

### [C08] Raw SQL escape hatch + `Model.from_id`

```python
rows = db.execute_sql(
    "SELECT _id FROM bus WHERE json_extract(data,'$.name') = ?",
    ["A"],
)
ids = [r.get("_id") if isinstance(r, dict) else r[0] for r in rows]
hydrated = [BU.from_id(i) for i in ids]
assert all(isinstance(h, BU) for h in hydrated)
```

### [C09] Delete policies: `restrict`

```python
from sqler import SQLerDB, SQLerModel, ReferentialIntegrityError

class U(SQLerModel):
    name: str

class Post(SQLerModel):
    title: str
    author: dict | None = None

db = SQLerDB.in_memory()
U.set_db(db); Post.set_db(db)

u = U(name="Writer").save()
Post(title="Post A", author={"_table":"u","_id":u._id}).save()

try:
    u.delete_with_policy(on_delete="restrict")
except ReferentialIntegrityError:
    pass
else:
    raise AssertionError("restrict deletes must block when referenced")
```

### [C10] Index variants: unique + partial

```python
from sqler import SQLerDB, SQLerModel

class X(SQLerModel):
    name: str
    email: str | None = None

db = SQLerDB.in_memory()
X.set_db(db)

db.create_index("xs", "email", unique=True)
db.create_index("xs", "name", where="json_extract(data,'$.name') IS NOT NULL")
```

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

---

## License

MIT © Contributors
