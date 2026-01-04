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

This started as a personal toolkit for **very fast prototyping**; small scripts that made it effortless to sketch data models, shove them into SQLite as JSON, and iterate. The result became SQLer: a tidy, dependency-light package that keeps that prototyping speed, but adds the pieces you need for real projects (indexes, relationships, integrity policies, and honest concurrency).

---

## Features

- **Document-style models** backed by SQLite JSON1
- **Fluent query builder**: `filter`, `exclude`, `contains`, `isin`, `.any().where(...)`
- **Extended field operations**: `between`, `is_null`, `is_not_null`, `startswith`, `endswith`, `glob`, `in_list`
- **NULL-safe comparisons**: `F("field") == None` generates proper `IS NULL`
- **Aggregations**: `sum`, `avg`, `min`, `max`, `exists`, `paginate`
- **Relationships** with simple reference storage and hydration
- **Safe models** with `_version` and optimistic locking (stale writes raise)
- **Configurable intent rebasing** for automatic conflict resolution
- **Bulk operations** (`bulk_upsert`, `bulk_insert`)
- **Transaction-aware saves**: `model.save()` respects explicit transactions (rollback works!)
- **Index management**: `create_index`, `drop_index`, `list_indexes`, `index_exists`
- **Model mixins**: timestamps, soft delete (with `active()`, `only_deleted()`, `with_deleted()`), lifecycle hooks
- **Auto-calling hooks**: `HooksMixin` automatically calls `before_save`/`after_save` in `save()`
- **Integrity policies** on delete: `restrict`, `set_null`, `cascade`
- **Query logging** for debugging and performance profiling
- **Raw SQL escape hatch** (parameterized), with model hydration when returning `_id, data`
- **Sync & Async** APIs with matching semantics
- **WAL-friendly concurrency** via thread-local connections (many readers, one writer)
- **Smart table naming**: proper English pluralization (category→categories, box→boxes)
- **Opt-in perf tests** and practical indexing guidance
- **Query caching** with TTL and LRU eviction
- **Data export/import**: CSV, JSON, JSONL (sync + async)
- **Full-text search** via FTS5 with SearchableMixin
- **Connection pooling** for high-concurrency scenarios
- **Schema migrations** with version tracking (sync + async)
- **Metrics collection** for monitoring and Prometheus export
- **Database operations**: backup, restore, health checks, vacuum
- **Change tracking** with dirty field detection and partial updates

---

## Tour Notebooks

Learn SQLer through [marimo](https://marimo.io) notebooks! View the pre-rendered versions online, or run them interactively on your own machine.

**[View Online Tour →](https://gabu-quest.github.io/sqler/)**

| Tour | Topics |
|------|--------|
| [01. Fundamentals](https://gabu-quest.github.io/sqler/tour_01_fundamentals/) | Setup, models, CRUD, queries, aggregations |
| [02. Relationships](https://gabu-quest.github.io/sqler/tour_02_relationships/) | Model references, hydration, cross-model queries |
| [03. Safe Models](https://gabu-quest.github.io/sqler/tour_03_safe_models/) | Optimistic locking, StaleVersionError, conflict resolution |
| [04. Transactions](https://gabu-quest.github.io/sqler/tour_04_transactions/) | Atomic operations, rollback, nested transactions |
| [05. Mixins](https://gabu-quest.github.io/sqler/tour_05_mixins/) | Timestamps, soft delete, lifecycle hooks |
| [06. Advanced](https://gabu-quest.github.io/sqler/tour_06_advanced/) | Bulk ops, indexes, integrity policies, raw SQL |

### Run Locally (Interactive)

Clone the repo and run any tour interactively with marimo:

```bash
git clone https://github.com/gabu-quest/sqler.git
cd sqler
uv sync --dev
uv run marimo edit examples/tour_01_fundamentals.py
```

---

## What's New (Latest)

### Transaction-Aware Saves

`model.save()` now respects explicit transactions. Previously, saves would commit immediately even inside a transaction block, breaking rollback behavior. Now:

```python
with db.transaction():
    User(name="Alice").save()  # NOT committed yet
    User(name="Bob").save()    # NOT committed yet
    raise RuntimeError("Oops!")
# Both saves are rolled back!
```

### Soft Delete Convenience Methods

`SoftDeleteMixin` now provides class methods for querying:

```python
User.active()       # Only non-deleted records
User.only_deleted() # Only deleted records
User.with_deleted() # All records including deleted
```

### Extended Query Builder

New field operations for more expressive queries:

```python
F("age").between(18, 65)           # BETWEEN ? AND ?
F("email").is_null()               # IS NULL
F("email").is_not_null()           # IS NOT NULL
F("name").startswith("Al")         # LIKE 'Al%' ESCAPE '\'
F("email").endswith("@gmail.com")  # LIKE '%@gmail.com' ESCAPE '\'
F("path").glob("/home/*")          # GLOB pattern
F("status").in_list(["a", "b"])    # IN (?, ?)
```

### NULL-Safe Comparisons

`F("field") == None` now generates `IS NULL` (not `= NULL`):

```python
# Before: broken SQL "= NULL" (always false)
# After: correct SQL "IS NULL"
User.query().filter(F("deleted_at") == None)  # Works correctly!
```

### Configurable Intent Rebasing

Control automatic conflict resolution for safe models:

```python
from sqler import RebaseConfig, PERMISSIVE_REBASE_CONFIG, NO_REBASE_CONFIG

class Counter(SQLerSafeModel):
    count: int = 0
    _rebase_config = PERMISSIVE_REBASE_CONFIG  # Allow rebasing any numeric field
```

### Auto-Calling Lifecycle Hooks

`HooksMixin` now automatically calls hooks in `save()` and `delete()`:

```python
class AuditedUser(HooksMixin, SQLerModel):
    email: str

    def before_save(self) -> bool:
        self.email = self.email.lower()
        return True  # False aborts save

user = AuditedUser(email="ALICE@TEST.COM")
user.save()  # Hooks called automatically!
```

### Index Management

Query and manage indexes programmatically:

```python
db.list_indexes()                    # All indexes
db.list_indexes("users")             # Indexes on 'users' table
db.index_exists("idx_users_email")   # Check if exists
```

### Smart Table Naming

Proper English pluralization for table names:

```python
class Category(SQLerModel): ...  # → categories (not categorys)
class Box(SQLerModel): ...       # → boxes (not boxs)
class Company(SQLerModel): ...   # → companies (not companys)
```

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
Prefecture.set_db(db)
City.set_db(db)

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
Address.set_db(db)
User.set_db(db)
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
U.set_db(db)
Post.set_db(db)

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

### [C11] Create, query, close

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

### [C12] Async match to sync

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

### [C13] Safe model collision handling

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

Store references to other models, hydrate them automatically, and filter across JSON references.

### [C14] Store and query relationships

```python
from sqler import SQLerDB, SQLerModel

class Address(SQLerModel):
    city: str
    country: str

class User(SQLerModel):
    name: str
    address: Address | None = None

db = SQLerDB.in_memory()
Address.set_db(db)
User.set_db(db)

home = Address(city="Kyoto", country="JP").save()
user = User(name="Alice", address=home).save()

loaded = User.from_id(user._id)
assert loaded.address.city == "Kyoto"

q = User.query().filter(User.ref("address").field("city") == "Kyoto")
assert [row.name for row in q.all()] == ["Alice"]
```

---

## Query Builder

- **Fields:** `F("age")`, `F(["items","qty"])`
- **Predicates:** `==`, `!=`, `<`, `<=`, `>`, `>=`, `contains`, `isin`
- **Boolean ops:** `&` (AND), `|` (OR), `~` (NOT)
- **Exclude:** invert a predicate set
- **Arrays:** `.any()` and scoped `.any().where(...)`

When you call `Model.query()`, introspection helpers include `.debug()` (returns `(sql, params)`), plus `.sql()` and `.params()` methods that mirror the underlying `SQLerQuery` properties.

### [C15] Query builder patterns

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class QueryUser(SQLerModel):
    name: str
    age: int
    tags: list[str] | None = None
    tier: int | None = None

class QueryOrder(SQLerModel):
    customer: str
    items: list[dict] | None = None

db = SQLerDB.in_memory()
QueryUser.set_db(db)
QueryOrder.set_db(db)

QueryUser(name="Ada", age=36, tags=["pro", "python"], tier=1).save()
QueryUser(name="Bob", age=20, tags=["hobby"], tier=3).save()

QueryOrder(customer="Ada", items=[{"sku": "ABC", "qty": 3}]).save()
QueryOrder(customer="Bob", items=[{"sku": "XYZ", "qty": 1}]).save()

q1 = QueryUser.query().filter(F("tags").contains("pro"))
assert [u.name for u in q1.all()] == ["Ada"]

q2 = QueryUser.query().filter(F("tier").isin([1, 2]))
assert [u.name for u in q2.all()] == ["Ada"]

q3 = QueryUser.query().exclude(F("name").like("test%"))
assert {u.name for u in q3.all()} == {"Ada", "Bob"}

expr = F(["items"]).any().where((F("sku") == "ABC") & (F("qty") >= 2))
q4 = QueryOrder.query().filter(expr)
assert [o.customer for o in q4.all()] == ["Ada"]

sql, params = QueryUser.query().filter(F("age") >= 18).debug()
assert isinstance(sql, str) and params == [18]

plan = QueryUser.query().filter(F("age") >= 18).explain_query_plan(QueryUser.db().adapter)
assert plan and len(list(plan)) >= 1
```

---

## Data Integrity

### Delete Policies (`restrict`, `set_null`, `cascade`)

Control how deletions affect JSON references in related rows.

- `restrict` (default): prevent deletion if anything still references the row
- `set_null`: null out the JSON field that holds the reference (field must be nullable)
- `cascade`: recursively delete referrers (depth-first, cycle-safe)

### [C16] Delete policies in action

```python
from sqler import SQLerDB, SQLerModel, ReferentialIntegrityError

class DIUser(SQLerModel):
    name: str

class Post(SQLerModel):
    title: str
    author: dict | None = None

# restrict: raises while references exist
restrict_db = SQLerDB.in_memory()
DIUser.set_db(restrict_db)
Post.set_db(restrict_db)
writer = DIUser(name="Writer").save()
Post(title="Post A", author={"_table": "diusers", "_id": writer._id}).save()
try:
    writer.delete_with_policy(on_delete="restrict")
except ReferentialIntegrityError:
    pass

# set_null: clears JSON ref before delete
set_null_db = SQLerDB.in_memory()
DIUser.set_db(set_null_db)
Post.set_db(set_null_db)
nullable = DIUser(name="Nullable").save()
post = Post(title="Post B", author={"_table": "diusers", "_id": nullable._id}).save()
nullable.delete_with_policy(on_delete="set_null")
assert Post.from_id(post._id).author is None

# cascade: remove dependents recursively
cascade_db = SQLerDB.in_memory()
DIUser.set_db(cascade_db)
Post.set_db(cascade_db)
cascade = DIUser(name="Cascade").save()
Post(title="Post C", author={"_table": "diusers", "_id": cascade._id}).save()
cascade.delete_with_policy(on_delete="cascade")
assert Post.query().count() == 0
```

### Reference Validation

Detect orphans proactively:

### [C17] Reference validation

```python
from sqler import SQLerDB, SQLerModel

class RefUser(SQLerModel):
    name: str

class RefPost(SQLerModel):
    title: str
    author: dict | None = None

db = SQLerDB.in_memory()
RefUser.set_db(db)
RefPost.set_db(db)

user = RefUser(name="Ada").save()
dangling = RefPost(title="Lost", author={"_table": RefUser.__tablename__, "_id": user._id}).save()
db.delete_document(RefUser.__tablename__, user._id)  # simulate manual deletion

broken = RefPost.validate_references()
assert broken and broken[0].row_id == dangling._id

# Returned items are sqler.models.BrokenRef dataclasses
```

---

## Bulk Operations

Write many documents efficiently.

### [C18] Bulk upsert

```python
from sqler import SQLerDB, SQLerModel

class BulkUser(SQLerModel):
    name: str
    age: int | None = None

db = SQLerDB.in_memory()
BulkUser.set_db(db)

rows = [{"name": "A"}, {"name": "B"}, {"_id": 42, "name": "C"}]
ids = db.bulk_upsert(BulkUser.__tablename__, rows)
assert len(ids) == 3 and 42 in ids
```

Notes:

- If SQLite supports `RETURNING`, SQLer uses it; otherwise a safe fallback is used.
- For sustained heavy writes, favor a single-process writer (SQLite has a single writer at a time).

---

## Transactions

Use explicit transactions for atomic multi-operation batches.

### [C22] Transaction context manager

```python
from sqler import SQLerDB, SQLerModel

class TxUser(SQLerModel):
    name: str
    balance: int = 0

db = SQLerDB.in_memory()
TxUser.set_db(db)

# Verify transaction API exists and works
tx = db.transaction()
assert hasattr(tx, "__enter__")
assert hasattr(tx, "__exit__")
assert hasattr(tx, "commit")
assert hasattr(tx, "rollback")

# Transaction context manager usage
with db.transaction():
    db.insert_document("txusers", {"name": "Alice", "balance": 100})
    db.insert_document("txusers", {"name": "Bob", "balance": 200})

assert TxUser.query().count() == 2

# Explicit commit/rollback methods
tx = db.transaction()
tx.__enter__()
db.insert_document("txusers", {"name": "Charlie", "balance": 300})
tx.commit()

assert TxUser.query().count() == 3
```

---

## Query Aggregations

Perform aggregate calculations directly in the database.

### [C23] Sum, avg, min, max

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class Product(SQLerModel):
    name: str
    price: float
    quantity: int

db = SQLerDB.in_memory()
Product.set_db(db)

Product(name="Apple", price=1.50, quantity=100).save()
Product(name="Banana", price=0.75, quantity=150).save()
Product(name="Cherry", price=3.00, quantity=50).save()

q = Product.query()
assert q.sum("quantity") == 300
assert q.avg("price") == 1.75
assert q.min("price") == 0.75
assert q.max("price") == 3.00

# With filters
expensive = Product.query().filter(F("price") > 1.0)
assert expensive.sum("quantity") == 150  # Apple + Cherry
```

### [C24] Exists check

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class Item(SQLerModel):
    name: str
    active: bool = True

db = SQLerDB.in_memory()
Item.set_db(db)

Item(name="Widget", active=True).save()

assert Item.query().filter(F("active") == True).exists() == True
assert Item.query().filter(F("name") == "Missing").exists() == False
```

---

## Pagination

Built-in pagination with navigation helpers.

### [C25] Paginate results

```python
from sqler import SQLerDB, SQLerModel, PaginatedResult

class Article(SQLerModel):
    title: str
    views: int = 0

db = SQLerDB.in_memory()
Article.set_db(db)

for i in range(25):
    Article(title=f"Article {i}", views=i * 10).save()

# Get page 2 with 10 items per page
page = Article.query().order_by("views", desc=True).paginate(page=2, per_page=10)

assert isinstance(page, PaginatedResult)
assert len(page.items) == 10
assert page.page == 2
assert page.total == 25
assert page.total_pages == 3
assert page.has_next == True
assert page.has_prev == True
assert page.next_page == 3
assert page.prev_page == 1
```

---

## Model Mixins

Reusable mixins for common functionality.

### [C26] Timestamps

```python
from sqler import SQLerDB, SQLerModel, TimestampMixin

class Post(TimestampMixin, SQLerModel):
    title: str
    content: str

db = SQLerDB.in_memory()
Post.set_db(db)

post = Post(title="Hello", content="World")
post._set_timestamps()  # Call before save for auto-timestamps
post = post.save()

assert post.created_at is not None
assert post.updated_at is not None
```

### [C27] Soft delete

```python
from sqler import SQLerDB, SQLerModel, SoftDeleteMixin

class Comment(SoftDeleteMixin, SQLerModel):
    text: str

db = SQLerDB.in_memory()
Comment.set_db(db)

c = Comment(text="Nice post!").save()
assert c.is_deleted == False

c.soft_delete()
assert c.is_deleted == True
assert c.deleted_at is not None

c.restore()
assert c.is_deleted == False
assert c.deleted_at is None

# Verify the comment was restored and can be queried
all_comments = Comment.query().all()
assert len(all_comments) == 1
assert all_comments[0].is_deleted == False
```

### [C28] Lifecycle hooks

```python
from sqler import SQLerDB, SQLerModel, HooksMixin

class AuditedUser(HooksMixin, SQLerModel):
    email: str
    normalized: bool = False

    def before_save(self) -> bool:
        self.email = self.email.lower().strip()
        self.normalized = True
        return True  # Continue with save

    def after_save(self) -> None:
        pass  # Log, notify, etc.

db = SQLerDB.in_memory()
AuditedUser.set_db(db)

# Hooks are called manually by caller
u = AuditedUser(email="  ALICE@Example.COM  ")
if u.before_save():
    u = u.save()
    u.after_save()

assert u.email == "alice@example.com"
assert u.normalized == True
```

---

## Query Logging

Debug and profile queries with the built-in logger.

### [C29] Query logging

```python
from sqler import SQLerDB, SQLerModel, query_logger
from sqler.query import SQLerField as F

class LoggedUser(SQLerModel):
    name: str
    age: int

db = SQLerDB.in_memory()
LoggedUser.set_db(db)

# Enable logging
query_logger.enable()

LoggedUser(name="Ada", age=36).save()
LoggedUser(name="Bob", age=25).save()

# Note: query_logger captures queries when integrated with adapter
# Here we demonstrate the logger API
query_logger.log("SELECT * FROM loggedusers", [], 0.5)
query_logger.log("SELECT * FROM loggedusers WHERE age > ?", [30], 1.2)

# Get logged queries
logs = query_logger.logs
assert len(logs) >= 2

# Get slow queries
slow = query_logger.get_slow_queries(threshold_ms=1.0)
assert len(slow) >= 1

# Get stats
stats = query_logger.get_stats()
assert "count" in stats
assert "avg_time_ms" in stats

query_logger.disable()
query_logger.clear()
```

---

## Transaction-Aware Operations

Model operations now respect explicit transactions, allowing proper rollback behavior.

### [C30] Transaction-aware model.save()

```python
from sqler import SQLerDB, SQLerModel

class TxItem(SQLerModel):
    name: str

db = SQLerDB.in_memory()
TxItem.set_db(db)

# Saves inside transaction respect rollback
try:
    with db.transaction():
        TxItem(name="A").save()
        TxItem(name="B").save()
        raise RuntimeError("abort!")
except RuntimeError:
    pass

# Nothing was saved due to rollback
assert TxItem.query().count() == 0

# Without transaction, saves commit immediately
TxItem(name="C").save()
TxItem(name="D").save()
assert TxItem.query().count() == 2
```

---

## Extended Query Builder

New field operations for more expressive queries.

### [C31] Field operations: between, startswith, endswith, glob

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class Employee(SQLerModel):
    name: str
    age: int
    email: str

db = SQLerDB.in_memory()
Employee.set_db(db)

Employee(name="Alice", age=25, email="alice@example.com").save()
Employee(name="Bob", age=35, email="bob@test.org").save()
Employee(name="Charlie", age=45, email="charlie@example.com").save()

# between (inclusive)
mid_age = Employee.query().filter(F("age").between(30, 40)).all()
assert [e.name for e in mid_age] == ["Bob"]

# startswith
alice = Employee.query().filter(F("name").startswith("Al")).all()
assert [e.name for e in alice] == ["Alice"]

# endswith
example_emails = Employee.query().filter(F("email").endswith("@example.com")).all()
assert len(example_emails) == 2

# is_null / is_not_null
Employee(name="NoEmail", age=30, email="").save()  # empty but not null
all_with_email = Employee.query().filter(F("email").is_not_null()).all()
assert len(all_with_email) == 4
```

### [C32] NULL-safe comparison with == None

```python
from sqler import SQLerDB, SQLerModel, SoftDeleteMixin
from sqler.query import SQLerField as F

class SoftUser(SoftDeleteMixin, SQLerModel):
    name: str

db = SQLerDB.in_memory()
SoftUser.set_db(db)

active = SoftUser(name="Active").save()
deleted = SoftUser(name="Deleted").save()
deleted.soft_delete()

# F("field") == None generates IS NULL (correct SQL)
# F("field") != None generates IS NOT NULL
active_users = SoftUser.query().filter(F("deleted_at") == None).all()
deleted_users = SoftUser.query().filter(F("deleted_at") != None).all()

assert len(active_users) == 1 and active_users[0].name == "Active"
assert len(deleted_users) == 1 and deleted_users[0].name == "Deleted"
```

### [C33] in_list for multiple value matching

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class Status(SQLerModel):
    code: str
    label: str

db = SQLerDB.in_memory()
Status.set_db(db)

Status(code="A", label="Active").save()
Status(code="P", label="Pending").save()
Status(code="C", label="Closed").save()
Status(code="D", label="Draft").save()

# in_list with values
open_statuses = Status.query().filter(F("code").in_list(["A", "P"])).all()
assert len(open_statuses) == 2

# Empty list returns nothing
empty = Status.query().filter(F("code").in_list([])).all()
assert len(empty) == 0
```

---

## Soft Delete Convenience Methods

Query soft-deleted records easily with class methods.

### [C34] SoftDeleteMixin class methods

```python
from sqler import SQLerDB, SQLerModel, SoftDeleteMixin

class Document(SoftDeleteMixin, SQLerModel):
    title: str

db = SQLerDB.in_memory()
Document.set_db(db)

doc1 = Document(title="Active Doc").save()
doc2 = Document(title="Deleted Doc").save()
doc3 = Document(title="Another Active").save()
doc2.soft_delete()

# active() - only non-deleted
active = Document.active().all()
assert len(active) == 2
assert all(d.deleted_at is None for d in active)

# only_deleted() - only deleted
deleted = Document.only_deleted().all()
assert len(deleted) == 1
assert deleted[0].title == "Deleted Doc"

# with_deleted() - all records
all_docs = Document.with_deleted().all()
assert len(all_docs) == 3
```

---

## Index Management

Query and manage database indexes programmatically.

### [C35] list_indexes and index_exists

```python
from sqler import SQLerDB, SQLerModel

class Product(SQLerModel):
    sku: str
    price: float

db = SQLerDB.in_memory()
Product.set_db(db)

# Create indexes
db.create_index("products", "sku", unique=True, name="idx_products_sku")
db.create_index("products", "price", name="idx_products_price")

# List all indexes
all_indexes = db.list_indexes()
assert len(all_indexes) >= 2

# List indexes for specific table
product_indexes = db.list_indexes("products")
assert len(product_indexes) == 2

# Check if index exists
assert db.index_exists("idx_products_sku") == True
assert db.index_exists("nonexistent_index") == False

# Index info includes uniqueness
sku_idx = next(i for i in product_indexes if i["name"] == "idx_products_sku")
assert sku_idx["unique"] == True
```

---

## Configurable Intent Rebasing

Control how safe models handle concurrent numeric field updates.

### [C36] RebaseConfig for safe models

```python
from sqler import SQLerDB, SQLerSafeModel, StaleVersionError
from sqler.models.utils import RebaseConfig, PERMISSIVE_REBASE_CONFIG, NO_REBASE_CONFIG

class Counter(SQLerSafeModel):
    value: int = 0
    count: int = 0
    # Allow rebasing any numeric field with delta ±1
    _rebase_config = PERMISSIVE_REBASE_CONFIG

class StrictCounter(SQLerSafeModel):
    value: int = 0
    # No rebasing - any conflict raises
    _rebase_config = NO_REBASE_CONFIG

db = SQLerDB.in_memory()
Counter.set_db(db)
StrictCounter.set_db(db)

# Permissive: increments can be rebased
c = Counter(value=0, count=0).save()
c.value += 1
c.count += 1
c.save()  # Works even if version changed (for small deltas)

# Strict: no automatic rebasing
s = StrictCounter(value=0).save()
s.value += 1
s.save()
assert s._version == 1
```

---

## Auto-Calling Lifecycle Hooks

`HooksMixin` automatically invokes hooks when using `save()` and `delete()`.

### [C37] HooksMixin auto-calling

```python
from sqler import SQLerDB, SQLerModel, HooksMixin

class AuditedItem(HooksMixin, SQLerModel):
    name: str
    normalized: bool = False
    save_count: int = 0

    def before_save(self) -> bool:
        self.name = self.name.strip().lower()
        self.normalized = True
        return True  # Continue with save

    def after_save(self) -> None:
        self.save_count += 1

db = SQLerDB.in_memory()
AuditedItem.set_db(db)

# Hooks are called automatically in save()
item = AuditedItem(name="  HELLO WORLD  ")
item = item.save()

assert item.name == "hello world"
assert item.normalized == True
assert item.save_count == 1

# before_save returning False aborts the save
class AbortableItem(HooksMixin, SQLerModel):
    name: str
    valid: bool = True

    def before_save(self) -> bool:
        return self.valid  # Abort if not valid

db2 = SQLerDB.in_memory()
AbortableItem.set_db(db2)

try:
    AbortableItem(name="test", valid=False).save()
except RuntimeError as e:
    assert "before_save() returned False" in str(e)
```

---

## Advanced Usage

### Raw SQL (`execute_sql`)

Run parameterized SQL. To hydrate models later, return `_id` and `data` columns.

### [C19] Raw SQL (`execute_sql`)

```python
from sqler import SQLerDB, SQLerModel

class ReportUser(SQLerModel):
    name: str
    email: str | None = None

db = SQLerDB.in_memory()
ReportUser.set_db(db)
ReportUser(name="Ada", email="ada@example.com").save()
ReportUser(name="Bob", email="bob@example.com").save()

rows = db.execute_sql("""
  SELECT u._id, u.data
  FROM reportusers u
  WHERE json_extract(u.data,'$.name') LIKE ?
""", ["A%"])
assert len(rows) == 1 and rows[0]["_id"] == 1
```

### Indexes (JSON paths)

Build indexes for fields you filter/sort on.

### [C20] Index helpers

```python
from sqler import SQLerDB, SQLerModel

class IndexedUser(SQLerModel):
    name: str
    age: int | None = None
    email: str | None = None
    address: dict | None = None

db = SQLerDB.in_memory()
IndexedUser.set_db(db)

# DB-level indexes on JSON paths
db.create_index("indexedusers", "age")
db.create_index("indexedusers", "email", unique=True)
db.create_index(
    "indexedusers",
    "age",
    where="json_extract(data,'$.age') IS NOT NULL",
)

# Relationship-friendly indexes
db.create_index("indexedusers", "address._id")
db.create_index("indexedusers", "address.city")
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

### [C21] FastAPI stale version

```python
try:
    from fastapi import HTTPException
except ImportError:  # pragma: no cover - docs fallback
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            self.status_code = status_code
            self.detail = detail

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

SQLer provides a unified exception hierarchy under `sqler.exceptions`:

- **Connection errors**
  - `NotConnectedError` — adapter closed / not connected
  - `ConnectionPoolExhaustedError` — no connections available
- **Query errors**
  - `NoAdapterError` — query executed without adapter
  - `InvariantViolationError` — malformed row invariant (e.g., NULL JSON)
  - `QueryTimeoutError` — query exceeded timeout
- **Concurrency errors**
  - `StaleVersionError` — optimistic check failed (HTTP 409)
  - `DeadlockError` — deadlock detected
  - `LockTimeoutError` — unable to acquire lock
- **Integrity errors**
  - `ReferentialIntegrityError` — foreign key constraint violated
  - `UniqueConstraintError` — unique constraint violated
- **Model errors**
  - `NotBoundError` — model not bound to database
  - `NotFoundError` — model instance not found
  - `ValidationError` — model validation failed
- **Hook errors**
  - `BeforeSaveError`, `AfterSaveError` — save hook failures
  - `BeforeDeleteError`, `AfterDeleteError` — delete hook failures

All exceptions inherit from `SQLerError` for unified catching. SQLite exceptions (`sqlite3.*`) bubble with context.

### Error Handling Patterns

```python
from sqler import (
    SQLerDB, SQLerModel, SQLerSafeModel,
    StaleVersionError, ReferentialIntegrityError, NotBoundError,
)
from sqler.exceptions import SQLerError

# Pattern 1: Catch all SQLer errors
try:
    user.save()
except SQLerError as e:
    print(f"Database error: {e}")

# Pattern 2: Handle optimistic locking (HTTP 409)
def update_with_retry(model, max_retries=3):
    for attempt in range(max_retries):
        try:
            model.save()
            return model
        except StaleVersionError:
            if attempt < max_retries - 1:
                model.refresh()  # Reload from database
            else:
                raise

# Pattern 3: Safe deletion with referential integrity
def safe_delete(model):
    try:
        model.delete_with_policy(on_delete="restrict")
    except ReferentialIntegrityError as e:
        # Either cascade, set_null, or inform user
        print(f"Cannot delete: still referenced by other records")
        return False
    return True
```

---

## Troubleshooting

### Common Issues

**"database is locked"**

SQLite allows only one writer at a time. If you see this error:
- Reduce concurrent write operations
- Use transactions to batch writes: `with db.transaction(): ...`
- Increase `busy_timeout` pragma (default is 5000ms)
- For high write loads, consider a write queue or single-writer pattern

**"Model X is not bound"**

Call `Model.set_db(db)` before using the model:
```python
db = SQLerDB.in_memory()
User.set_db(db)  # Must call this before save/query
```

**StaleVersionError on save**

Using `SQLerSafeModel`, the row was modified by another process:
```python
try:
    model.save()
except StaleVersionError:
    model.refresh()  # Reload and decide: retry or abort
```

**Query returns empty but data exists**

Check field path syntax for nested fields:
```python
# Correct: use list for nested paths
F(["address", "city"]) == "Kyoto"

# Wrong: string doesn't traverse nested objects
F("address.city") == "Kyoto"  # Only works for index creation
```

**Async adapter not connecting**

Ensure you explicitly connect and close:
```python
db = AsyncSQLerDB.on_disk("app.db")
await db.connect()  # Required!
# ... operations ...
await db.close()    # Clean up
```

### Debug Tools

```python
# See generated SQL
q = User.query().filter(F("age") > 30)
print(q.sql())     # SELECT _id, data FROM users WHERE ...
print(q.params())  # [30]

# Full debug tuple
sql, params = q.debug()

# Query execution plan
plan = q.explain_query_plan(db.adapter)
for row in plan:
    print(row)

# Enable query logging
from sqler import query_logger
query_logger.enable()
# ... run queries ...
print(query_logger.get_slow_queries(threshold_ms=10.0))
```

---

## Query Caching

<!-- contract: C38 -->

Cache query results to avoid repeated database calls:

```python
from sqler import QueryCache, cached_query

# Create a cache with max 1000 entries, 5 minute TTL
cache = QueryCache(max_size=1000, default_ttl_seconds=300)

# Cache query results
cache.set("users:active", active_users, table="users")
result = cache.get("users:active")

# Check cache stats
stats = cache.stats
print(f"Hit rate: {stats.hit_rate:.2%}")

# Invalidate by pattern or table
cache.invalidate_pattern("users:*")
cache.invalidate_table("users")

# Use the decorator for automatic caching
@cached_query(ttl_seconds=60)
def get_active_users():
    return User.query().filter(F("active") == True).all()
```

---

## Data Export & Import

<!-- contract: C39 -->

Export and import data in various formats:

### CSV Export/Import

```python
from sqler import export_csv, export_csv_string, import_csv

# Export to file
result = export_csv(User, "users.csv")
print(f"Exported {result.count} records ({result.size_bytes} bytes)")

# Export specific fields
export_csv(User, "users.csv", fields=["name", "email"], include_id=False)

# Export to string
csv_string = export_csv_string(User)

# Import from CSV
result = import_csv(User, "users.csv")
print(f"Imported {result.succeeded}/{result.count} records")

# Import with transform
def normalize(row):
    row["email"] = row["email"].lower()
    return row

import_csv(User, "users.csv", transform=normalize)
```

### JSON Export/Import

```python
from sqler import export_json, export_json_string, import_json

# Export to file
export_json(User, "users.json", indent=2)

# Export to string
json_string = export_json_string(User)

# Import from JSON array file
result = import_json(User, "users.json")
```

### JSONL (JSON Lines) Export/Import

<!-- contract: C40 -->

```python
from sqler import export_jsonl, import_jsonl, stream_jsonl

# Export one record per line (streaming-friendly)
export_jsonl(User, "users.jsonl")

# Import JSONL
import_jsonl(User, "users.jsonl")

# Stream records without loading all into memory
for record_json in stream_jsonl(User):
    process(record_json)
```

### Async Export/Import

```python
from sqler import async_export_jsonl, async_import_jsonl

# Async versions for high-throughput scenarios
await async_export_jsonl(User, "users.jsonl")
await async_import_jsonl(User, "users.jsonl")
```

---

## Full-Text Search

<!-- contract: C41 -->

SQLer provides FTS5-based full-text search:

### Using FTSIndex Directly

```python
from sqler import FTSIndex, SearchResult

class Article(SQLerModel):
    title: str
    content: str
    author: str

Article.set_db(db)

# Create FTS index on specific fields
fts = FTSIndex(Article, fields=["title", "content"])
fts.create(db)
fts.rebuild()  # Index existing records

# Search
results = fts.search("Python")
for article in results:
    print(article.title)

# Ranked search with scores
ranked = fts.search_ranked("Python programming")
for result in ranked:
    print(f"{result.model.title} (score: {result.score})")

# Count matches
count = fts.count("Python")

# Index a new document
new_article = Article(title="New", content="content").save()
fts.index(new_article)
```

### Using SearchableMixin

```python
from sqler import SearchableMixin

class Post(SearchableMixin, SQLerModel):
    title: str
    body: str

    class FTS:
        fields = ["title", "body"]

Post.set_db(db)

# Create index once
Post.create_search_index(db)

# Add posts
Post(title="Python Tips", body="Learn Python").save()
Post.rebuild_search_index()

# Search
results = Post.search("Python")
count = Post.search_count("Python")
```

---

## Connection Pooling

<!-- contract: C42 -->

For high-concurrency scenarios, use connection pooling:

```python
from sqler import PooledSQLerDB, PoolStats

# Create pooled database with 10 connections
db = PooledSQLerDB.on_disk("mydb.db", pool_size=10)

class User(SQLerModel):
    name: str

User.set_db(db)

# Use normally - connections are managed automatically
User(name="Alice").save()
users = User.query().all()

# Check pool stats
stats: PoolStats = db.pool_stats()
print(f"Active: {stats.active_connections}/{stats.pool_size}")
print(f"Waiting: {stats.waiting_requests}")

db.close()
```

---

## Schema Migrations

<!-- contract: C43 -->

Manage database schema changes with versioned migrations:

```python
from sqler import Migration, MigrationRunner

# Define migrations
migrations = [
    Migration(
        version=1,
        name="create_users",
        up=lambda db: db.adapter.execute(
            "CREATE TABLE users (_id INTEGER PRIMARY KEY, data JSON)"
        ),
        down=lambda db: db.adapter.execute("DROP TABLE users"),
    ),
    Migration(
        version=2,
        name="add_posts",
        up=lambda db: db.adapter.execute(
            "CREATE TABLE posts (_id INTEGER PRIMARY KEY, data JSON)"
        ),
        down=lambda db: db.adapter.execute("DROP TABLE posts"),
    ),
]

# Create runner and apply
runner = MigrationRunner(db, migrations)

# Check status
status = runner.status()
print(f"Current: v{status['current_version']}, Pending: {status['pending_count']}")

# Migrate to latest
result = runner.migrate()
if result.success:
    print(f"Applied {len(result.applied)} migrations")

# Migrate to specific version
runner.migrate(target_version=1)

# Rollback
runner.rollback(target_version=0)
```

### Async Migrations

```python
from sqler import AsyncMigration, AsyncMigrationRunner

migrations = [
    AsyncMigration(
        version=1,
        name="create_users",
        up=lambda db: db.adapter.execute("CREATE TABLE users ..."),
    ),
]

runner = AsyncMigrationRunner(db, migrations)
result = await runner.migrate()
```

---

## Metrics Collection

<!-- contract: C44 -->

Collect performance metrics for monitoring:

```python
from sqler import metrics

# Enable metrics collection
metrics.enable()

# ... run queries (metrics collected automatically) ...
User(name="Alice").save()
User.query().all()

# Get metrics data
data = metrics.get_metrics()
print(f"Total queries: {data['queries']['total_queries']}")
print(f"Histogram: {data['queries']['histogram']}")

# Get Prometheus-format output
prometheus_text = metrics.prometheus_export()
# Returns:
# sqler_queries_total 150
# sqler_query_duration_ms_bucket{le="1"} 50
# sqler_query_duration_ms_bucket{le="10"} 100
# ...

# Add custom callback for real-time monitoring
metrics.add_callback(lambda log: print(f"Query: {log.sql[:50]}"))

# Reset metrics
metrics.reset()

# Disable when done
metrics.disable()
```

---

## Database Operations

<!-- contract: C45 -->

Production-ready database operations:

### Health Checks

```python
from sqler import health_check, is_healthy, HealthStatus

# Quick boolean check
if is_healthy(db):
    print("Database OK")

# Detailed health check
status: HealthStatus = health_check(db)
print(f"Healthy: {status.healthy}")
print(f"Latency: {status.latency_ms:.2f}ms")
print(f"Details: {status.details}")

# Serialize for API response
return status.to_dict()
```

### Database Statistics

```python
from sqler import get_stats, DatabaseStats

stats: DatabaseStats = get_stats(db)
print(f"Tables: {stats.table_count}")
print(f"Indexes: {stats.index_count}")
print(f"Page size: {stats.page_size}")
print(f"Page count: {stats.page_count}")

# Serialize for monitoring
return stats.to_dict()
```

### Backup and Restore

```python
from sqler import backup, restore, BackupResult

# Create backup
result: BackupResult = backup(db, "/backups/mydb.bak")
if result.success:
    print(f"Backup created: {result.size_bytes} bytes in {result.duration_ms}ms")

# Restore from backup
result = restore(db, "/backups/mydb.bak")
if result.success:
    print("Database restored")
```

### Maintenance Operations

```python
from sqler import vacuum, checkpoint

# Reclaim space and defragment
duration_ms = vacuum(db)

# Force WAL checkpoint
checkpoint(db)
```

### Async Operations

```python
from sqler import async_health_check, async_backup, async_get_stats, async_vacuum

status = await async_health_check(db)
await async_backup(db, "/backups/mydb.bak")
stats = await async_get_stats(db)
await async_vacuum(db)
```

---

## Change Tracking

<!-- contract: C46 -->

Track field changes and detect dirty models:

### TrackedModel

```python
from sqler import TrackedModel

class User(TrackedModel, SQLerModel):
    name: str
    email: str
    age: int

User.set_db(db)

user = User(name="Alice", email="alice@test.com", age=30)
user.save()
user.mark_clean()

# Modify fields
user.name = "Bob"
user.age = 31

# Check dirty state
print(user.is_dirty)  # True
print(user.changed_fields)  # {'name', 'age'}

# Get detailed changes (old_value, new_value)
changes = user.get_changes()
# {'name': ('Alice', 'Bob'), 'age': (30, 31)}

# Revert unsaved changes
user.revert_changes()
print(user.name)  # 'Alice'
```

### DiffMixin

```python
from sqler import DiffMixin

class Item(DiffMixin, SQLerModel):
    name: str
    quantity: int

Item.set_db(db)

item1 = Item(name="Apple", quantity=10)
item2 = Item(name="Apple", quantity=15)

# Compare two instances
diff = item1.diff(item2)
# {'quantity': (10, 15)}

# Check equality
item1.is_equal(item2)  # False

# Clone with overrides
cloned = item1.clone(quantity=20)
# Item with same name, quantity=20, no _id
```

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
