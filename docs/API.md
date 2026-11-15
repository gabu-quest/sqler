# sqler API Reference

> Every code block is tested in CI via `tests/test_readme.py`.

## Table of Contents

- [Core — Models, Save, Query](#core--models-save-query)
- [Query Builder](#query-builder)
- [Relationships](#relationships)
- [Safe Models & Concurrency](#safe-models--concurrency)
- [Bulk Operations & Transactions](#bulk-operations--transactions)
- [Data Integrity](#data-integrity)
- [Database](#database)
- [Mixins](#mixins)
- [Raw SQL](#raw-sql)
- [Advanced Features](#advanced-features)
- [Error Handling](#error-handling)
- [Troubleshooting](#troubleshooting)

---

## Core — Models, Save, Query

> **Deprecation notice:** `set_db()` is soft-deprecated. Prefer `using()` for class-level
> binding and `save(db=)` / `delete(db=)` for per-call DB targeting. `set_db()` still
> works but emits a `DeprecationWarning`. Examples below use `set_db()` for brevity;
> see [Per-Call DB Binding](#per-call-db-binding) for the recommended pattern.

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

### Per-Call DB Binding

The recommended way to bind models is `using()` (class-level) and `save(db=)` / `delete(db=)` (instance-level):

```python
# Class-level: using() returns a queryset on db (no global mutation)
users = User.using(db).filter(F("age") > 30).all()
users = User.from_ids([1, 2], db=db)

# One query on another table; the class binding is not changed
archived = User.using(db, "users_archive").all()

# Instance-level: target a specific DB for writes
user = User(name="Alice", age=30)
user.save(db=db1)       # writes to db1
user.delete(db=db2)     # deletes from db2

# Works with all model variants: SQLerModel, AsyncSQLerModel,
# SQLerLiteModel, AsyncSQLerLiteModel, SQLerMsgspecModel, SafeModel, AsyncSafeModel
```

Both patterns avoid global `set_db()` state and are safe for multi-DB scenarios.

**Which table a class uses.** Every route (`save(db=)`, `using(db)`, references,
`FTSIndex`, the registry) uses one rule: the table bound with `set_db()`, else
`__tablename__`, else the plural of the class name (`User` → `users`; a name that
already ends in `s` is kept). A class bound with `set_db(db, "a")` reads `a` from any
database given to `using()`.

**`using(db, table=None)`.** With `table`, that one query reads the named table; the
name is checked before any SQL (`InvalidTableNameError`). A row read this way
remembers its table: `save()`, `delete()` and `refresh()` use it, so a row is never
written to a different table than it was read from; `row.save(db=other_db)` updates
the row with the same `_id` in that table of `other_db`. `as_ref(row)`, and the
reference a model holding it stores, name that table. On the msgspec base the table
is kept outside the struct fields: it is in neither `msgspec.json.encode(row)` nor `==`.
A copy made with `model_copy()` (pydantic) keeps the table. A copy made with
`dataclasses.replace()` (lite) or `msgspec.structs.replace()` does not: it keeps the
`_id` and is a row of the class's own table, so read the row again instead of saving
such a copy.

**A read creates no table.** `using(db)`, `using(db, table)` and `query()` on an async
bound class run no `CREATE TABLE`: on a table nobody created, `all()` is `[]`,
`first()` is `None`, `count()` is 0, `exists()` is false, `sum()`/`avg()`/`min()`/`max()`
are `None` (`[]` with `group_by()`), `paginate()` gives an empty page, and a queryset
`update()` or `delete_all()` changes 0 rows; the table is still missing afterwards.
`from_id()`, `from_ids()` and `refresh()` read the same way (a missing table is a
missing row). "no such table" for any other table (a join, a subquery, a missing
search index) and "no such column" still raise. Writes (`save()`, the insert of
`first_or_create()`) create the table as before. A read never needs the write lock, so
it does not wait behind another connection's write.

**References without `set_db()`.** A model saved with `save(db=db)` can be held by
another model; read back through `using(db)`, the related model is in place. Model
classes are registered under their table when they are defined, so a stored reference
finds its class even when that class was defined after the row was written. A class
that is only defined never takes the place of a class bound to the same table with
`set_db()` (a later import of a same-named class, a class defined inside a function, a
subclass); `set_db()` always registers. When the referenced row cannot be read because
its table is missing, the stored reference stays the `{"_table", "_id"}` dict; any
other error (a row that does not fit its class) raises, sync and async alike.

A search index for such a class is given the database, `FTSIndex(Article, ["title"],
db=db)`, and reads the class's table by the same rule: `search()`,
`search_with_highlights()` and `search_ranked()` load the rows from that database, and
return `[]` when the class's table does not exist.

**`first_or_create(lookup, defaults=None, *, db=None)`.** Reads, then inserts. When a
unique index refuses the insert because another caller inserted the same lookup first,
it reads again and returns that row with `created` false. If the refusal came from a
unique index on a field outside the lookup, the `UniqueConstraintError` reaches you.

---

## Query Builder

### [C03] `.any().where(...)` for array-of-objects

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

### [C23] Aggregations (sum, avg, min, max)

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

### [C25] Pagination

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

### [C32] NULL-safe comparison

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

### [C33] in_list

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

## Relationships

### [C04] Hydration & cross-filtering

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

## Safe Models & Concurrency

### [C06] Optimistic versioning

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

# This write is stale -> raises
try:
    acc.balance = 130
    acc.save()
except StaleVersionError:
    acc.refresh()          # reloads both fields and _version
```

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

### [C36] RebaseConfig

```python
from sqler import SQLerDB, SQLerSafeModel, StaleVersionError
from sqler.models.utils import RebaseConfig, PERMISSIVE_REBASE_CONFIG, NO_REBASE_CONFIG

class Counter(SQLerSafeModel):
    value: int = 0
    count: int = 0
    # Allow rebasing any numeric field with delta +/-1
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

## Bulk Operations & Transactions

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

### [C18] Bulk upsert (detailed)

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

### Nested transactions whose end fails

A `db.transaction()` inside another one is a savepoint: its block ends with `RELEASE`, or
with `ROLLBACK TO` then `RELEASE` when it raised. If SQLite refuses one of them (for
example because a raw `RELEASE` statement already ended the savepoint), the transaction can
no longer be trusted, on the sync adapter as on the async one. After a failed `ROLLBACK TO`
no `RELEASE` is sent, since that would keep the rows the rollback was to undo. The nested
block raises `TransactionStateError` with the SQLite error as `__cause__` (a block that is
already raising keeps its own exception, with the same sentence added as a note, once
however many blocks it leaves; any other error raised by the end of a transaction while an
exception leaves is named on that exception as a note, "ending the transaction failed:
<type>: <message>"). From then
on every statement of that thread in the transaction (`execute`, `executemany`,
`executescript`, `commit`, a new nested transaction, `with adapter:`) raises the same error
and sends nothing; the transaction ends with `ROLLBACK` whatever its block asks for, and the
block that ends it raises the error too. An interrupt (`KeyboardInterrupt`) during a nested
end leaves as it is, and the transaction ends rolled back the same way. The same holds for
`adapter.begin_transaction()` and `adapter.end_transaction()` called directly.

### How long a statement waits for a lock

`timeout_ms` (default 5000) is how long a statement waits for another connection's lock
before it raises `sqlite3.OperationalError: database is locked`. Pass it to
`SQLiteAdapter(...)`, `AsyncSQLiteAdapter(...)`, their `on_disk()` and `in_memory()`, or
`SQLerDB`/`AsyncSQLerDB` `.on_disk()` and `.in_memory()`; it is SQLite's `busy_timeout` on
every connection the adapter opens (every connection of an async pool), and on a plain
sync `":memory:"` database also the wait for another thread's transaction. The async
adapter has a second wait, `acquire_timeout` (seconds): the wait for a connection of its
own pool and for a turn in a transaction tasks share; `timeout_ms` is the wait for a lock
another connection or process holds. A `PRAGMA busy_timeout = N` in `pragmas` says the same thing:
without `timeout_ms` it sets the number, and beside a `timeout_ms` that differs `connect()`
raises `ValueError`. `connect()` on the async adapter opens its pool once: on a connected
adapter, or beside another `connect()` or a statement's own connect, it opens nothing more;
a `connect()` that fails closes every connection it opened and raises the error that made it
fail. A shared-cache in-memory database does not wait: SQLite calls no busy
handler for its table locks, so such a statement raises "database table is locked" at once.

### Async transactions shared by tasks

Tasks started inside `async with db.transaction():` share its connection. Each task's
place in the transaction is its innermost nested transaction that is still open, else the
transaction itself. A task sends a statement only while no other task's nested transaction
is open above its place: otherwise the statement waits its turn, in the order asked, for at
most `acquire_timeout` seconds, then raises `LockTimeoutError` (the other nested transaction
is still running, or it waits for this task and can never end). A statement that has been
given its turn is sent before any other task's nested transaction opens on top of it. With
`acquire_timeout=None` there is no bound: a task inside a nested transaction that awaits a
statement of a sibling on the same connection waits for ever, because the sibling waits for
that nested transaction to end.

The transaction ends for every task when its `COMMIT` or `ROLLBACK` is sent. A task that
outlives it is in no transaction: its statements run on a pooled connection and are
committed, and its `db.transaction()` starts a new one. Await the tasks inside the block
(or use a `TaskGroup`) to make their work part of it. If the block ends while a nested
transaction of another task is still open, the transaction is rolled back; the block raises
`TransactionStateError` (when it raised an exception of its own, that one goes on), and the
other task gets `TransactionStateError` at its next statement or at the end of its nested
block (an exception of its own goes on with a note that nothing in it was committed). A task
that ends with its own nested transaction open has it rolled back.

If the end of a nested transaction fails in SQLite (for example because a raw `RELEASE`
statement already ended its savepoint), the transaction can no longer be trusted. After a
failed `ROLLBACK TO` no `RELEASE` is sent, since that would keep the rows the rollback was to
undo. The nested block raises `TransactionStateError` with the SQLite error as `__cause__`,
the transaction ends rolled back whatever its block asks for, and the block that ends it
raises `TransactionStateError` too. A block that is already raising keeps its own exception,
and the same sentence is added to it as a note. From the failed end on, every statement sent
in that transaction by any task (`execute`, `executemany`, a new nested transaction, also one
that was waiting its turn) raises the same `TransactionStateError` and sends nothing, and a
nested block that ends in it raises it too.

### Async cursors

The cursor `await adapter.execute(...)` returns reads only what its statement saw while the
connection was its task's. SQLite reads a statement's rows as they are fetched; so before
another task sends a statement on that connection (a task that took it from the pool, a
child task that shares the pin, a sibling in a shared transaction) or a rollback ends its
transaction, the rows the cursor has not returned yet are read, in the connection's thread
ahead of that statement, and the cursor serves them from memory. Rows its own transaction
wrote and then rolled back are served: they are what the statement saw. While only its own
task uses the connection it reads lazily, as SQLite does (a row the task inserts before the
first fetch is in the result), and a cursor nobody holds any more, or one that is closed or
has returned its last row, is never read ahead. A statement whose rows raise part-way (a
`json()` of a malformed value, say) raises at the same read either way. A cursor serving
rows from memory answers `fetchone`, `fetchmany` (`arraysize` honoured), `fetchall`,
`async for`, `async with`, `close`, `description`, `rowcount` and `lastrowid` as before;
`connection` and `row_factory` stay the wrapped cursor's (the connection the statement ran
on, the factory the rows were built with: setting it after the rows were read changes none
of them). Of two statements that two tasks sharing one connection send at the same moment,
the one handed to the connection first is answered whole, as if the other had not been
sent. A statement cancelled while the read of another cursor's rows that it set off is
running leaves that cursor all its rows. If the adapter is closed, or the connection
breaks so that the read itself fails (the connection is then replaced), before a cursor's
rows could be read, a later read raises `NotConnectedError` and touches no connection. A
statement sent on `adapter.connection` (the raw `aiosqlite` connection) is outside this
rule: the adapter does not see it, and a lazy cursor on that connection may read its work.

---

## Data Integrity

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

## Database

The database classes `SQLerDB` and `AsyncSQLerDB` have the same methods; the async
ones are awaited. The layout they write on disk is in [STORAGE.md](STORAGE.md).

### [D01] `ensure_table`: promoted columns, checks, version

```python
from sqler import SQLerDB

db = SQLerDB.in_memory()
db.ensure_table(
    "jobs",
    promoted={"status": "TEXT NOT NULL DEFAULT 'pending'", "priority": "INTEGER NOT NULL DEFAULT 0"},
    checks={"status_valid": "status IN ('pending', 'running', 'done')"},
    versioned=True,
)
db.ensure_table("jobs", promoted={"status": "TEXT NOT NULL DEFAULT 'pending'"}, versioned=True)  # safe to repeat
_id, version = db.upsert_with_version_promoted(
    "jobs", None, {"status": "running", "payload": 1}, None, ["status", "priority"]
)
assert db.find_document_with_version("jobs", _id)["status"] == "running"
```

`promoted` maps a column name to a definition (`TYPE [NOT NULL] [DEFAULT literal]`),
`checks` maps a name to a CHECK expression (written only when the table is created),
and `versioned=True` adds the `_version` column, also to a table that exists
without it. Table names, column names, definitions and checks are checked before
any SQL runs; the write methods with promoted columns check the table and the
promoted field names the same way.

### [D02] A unique index that refuses a row raises `UniqueConstraintError`

```python
import sqlite3
from sqler import SQLerDB, UniqueConstraintError

db = SQLerDB.in_memory()
db.create_index("users", "email", unique=True)
db.insert_document("users", {"email": "a@example.com"})
try:
    db.insert_document("users", {"email": "a@example.com"})
except UniqueConstraintError as err:
    assert err.index == "idx_users_email"     # an index on a JSON field: SQLite names the index
    assert isinstance(err, sqlite3.IntegrityError)
    assert isinstance(err.__cause__, sqlite3.IntegrityError)
else:
    raise AssertionError("expected UniqueConstraintError")
```

Every write raises it when SQLite reports `UNIQUE constraint failed` (a unique
index or a primary key): document writes, the promoted and versioned writes,
`insert_many`, a model's `save()`, and raw `adapter.execute`, `executemany` and
`executescript`, sync and async. It is also a `sqlite3.IntegrityError`, so
`except sqlite3.IntegrityError` still catches it. It carries what SQLite named:
`table` and `columns` (`field` when there is one column), or `index` for an index
on an expression. NOT NULL, CHECK and FOREIGN KEY violations stay
`sqlite3.IntegrityError`. A safe model's save does not retry it.

### [D03] Async: the wait for a pooled connection

```python
import asyncio
from sqler import AsyncSQLerDB

async def main():
    db = AsyncSQLerDB.in_memory(acquire_timeout=5.0)   # seconds; None waits forever
    await db.connect()
    await db.insert_document("t", {"a": 1})
    stats = db.pool_stats()
    assert (stats.max_connections, stats.in_use_connections) == (1, 0)
    await db.close()

asyncio.run(main())
```

`AsyncSQLerDB.in_memory()` and `on_disk()` take `acquire_timeout` (default 30
seconds) and hand it to the adapter; a wait that runs out raises
`ConnectionPoolExhaustedError`. `AsyncAdapterABC` declares `pool_stats()`; an
adapter written outside sqler that has no pool need not define it, and calling it
then raises `NotImplementedError`.

---

## Mixins

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

## Raw SQL

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

---

## Advanced Features

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

### [C38] Query caching

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

### [C39] Data export (CSV, JSON)

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

```python
from sqler import export_json, export_json_string, import_json

# Export to file
export_json(User, "users.json", indent=2)

# Export to string
json_string = export_json_string(User)

# Import from JSON array file
result = import_json(User, "users.json")
```

### [C40] JSONL export/import

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

On a plain `":memory:"` database (`SQLerDB.in_memory(shared=False)`), whose one connection
every thread shares, a statement's rows are read whole before another thread may use the
connection, so `stream_jsonl()`, `iter_dicts()` and `iter()` hold the whole result there.

```python
from sqler import async_export_jsonl, async_import_jsonl

# Async versions for high-throughput scenarios
await async_export_jsonl(User, "users.jsonl")
await async_import_jsonl(User, "users.jsonl")
```

### [C41] Full-text search (FTS5)

```python
from sqler import FTSIndex, SearchResult

class Article(SQLerModel):
    title: str
    content: str
    author: str

Article.set_db(db)

# Create FTS index on specific fields
fts = FTSIndex(Article, fields=["title", "content"])
fts.create(db)  # also indexes the rows already in the table

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

# Text from a user: words to find, not a query
results = fts.search("C++ it's foo-bar", plain=True)

# A new row is indexed by the triggers create() added
new_article = Article(title="New", content="content").save()
assert fts.count("New") == 1
```

**The search string** given to `search()`, `search_ranked()`, `search_with_highlights()`,
`snippet()` and `count()` (and to the mixin's `search()`, `search_ranked()` and
`search_count()`) is an [FTS5 query](https://sqlite.org/fts5.html#full_text_query_syntax):
`python tutorial` (both words), `python OR rust`, `python NOT java`, `"exact phrase"`,
`pyth*`, `title:python`. A string SQLite refuses as a query (`C++`, `it's`, `foo-bar`, an
empty string) raises `SearchQueryError`, a `QueryError` whose message names the query and
SQLite's reason; the `sqlite3` error is its `__cause__`.

**`plain=True`** reads the text as words to find: a row matches when it has every word,
where a word is what the index's tokenizer makes of the text between spaces. No character
is syntax, so `C++`, `it's`, `foo-bar`, `AT&T` and `100%` work, `OR` and `NOT` are words,
and the pieces of `foo-bar` must stand next to each other. Text with no word in it (empty,
spaces, `*`) finds nothing. It never raises `SearchQueryError`.

**`content_sync=True`** (the default): `create()` creates the model's table if it is
missing and adds three triggers (`<index>_ai`, `<index>_au`, `<index>_ad`) that update the
index on every insert, update and delete of the table, whoever writes: `save()`,
`update()`, `bulk_upsert()`, the document methods, raw SQL, another connection. The index
change commits or rolls back with the write. `index()` and `remove()` change nothing then:
an entry leaves only with its row. **`content_sync=False`** adds no trigger: the index
changes only through `index()`, `remove()`, `rebuild()` and the mixin's `save()` and
`delete()`.

**What a trigger costs:** each insert and delete of a row writes its index row as well;
an update writes it only when the row id or the stored value of an indexed field changed,
so an update of other fields costs one comparison of the indexed values per row.

**`create()`** makes the index what its definition (fields, their order, tokenizer,
`content_sync`) says, or refuses, in one transaction:

- No index table: it is created; with `content_sync=True` the triggers are added and the
  index is filled from the rows already in the table.
- An index table with the same fields in the same order and the same tokenizer is kept,
  however its statement is spelled (an index made by an older sqler is the same index).
- An index table with other fields, another order or another tokenizer: `create()` raises
  `SchemaError`, naming the index, what it has and what was asked, and changes nothing.
  To change an index, call `drop()` then `create()`.
- With `content_sync=True`, a trigger that is missing or is not the one the definition
  writes now (a field promoted to a column since, an older sqler) is replaced, and the
  index is filled again from the table. So an index made by an older sqler, without
  triggers, gets them and its missing rows on the first `create()` after the upgrade.
- With `content_sync=False`, triggers of the index are dropped.

A `create()` that fails leaves the index and its triggers as they were; one that finds
everything as defined writes nothing (it does not wait for another writer's lock).
`drop()` removes the triggers with the index. A raw `DROP TABLE` of the index leaves its
triggers, and every later write to the table then fails; `drop()` removes both, and
`create()` repairs them.

Every way in indexes a field as it is stored: a promoted column from its column, any other
field from `data` (a list or dict as its JSON text, a number as the number, `None` as
nothing). `index(model)` reads the saved row, not the instance's unsaved values.

A search index works on a `SQLerDB`: given an `AsyncSQLerDB` (through `db=` or the
model's binding), every method raises `TypeError` before it sends anything.

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

# Add posts: the index follows
Post(title="Python Tips", body="Learn Python").save()

# Search
results = Post.search("Python")
count = Post.search_count("Python")
results = Post.search("C++", plain=True)
```

### [C42] Connection pooling

`SQLerDB` gives each thread its own connection and returns it when the thread ends, so threads share one `SQLerDB`.
`AsyncSQLerDB` pools its connections; `db.pool_stats()` returns a `PoolStats` with the pool's size, free and in-use connections, checkouts, timeouts and wait times.

### [C43] Schema migrations

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

Each async migration runs in one transaction with its version row, so inside `up()` and `down()` use one `adapter.execute()` per statement: `adapter.executescript()` fails there with "cannot run inside a transaction" (SQLite commits before a script), and the failed `result.error` says so.

### [C44] Metrics collection

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

### [C45] Database operations (backup, restore, health)

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

```python
from sqler import vacuum, checkpoint

# Reclaim space and defragment
duration_ms = vacuum(db)

# Force WAL checkpoint
checkpoint(db)
```

```python
from sqler import async_health_check, async_backup, async_get_stats, async_vacuum

status = await async_health_check(db)
await async_backup(db, "/backups/mydb.bak")
stats = await async_get_stats(db)
await async_vacuum(db)
```

### [C46] Change tracking

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

On a tracked model `is_dirty` is a property (the tracker's answer), where the model bases
have an `is_dirty()` method: write `user.is_dirty`, and `SQLerModel.is_dirty(user)` for the
base's answer.

`TrackedModel` works with the sync pydantic and lite bases (`SQLerModel`, `SQLerSafeModel`,
`SQLerLiteModel`, `SQLerLiteSafeModel`). An async model uses `AsyncTrackedModel` (with
`AsyncSQLerModel`, `AsyncSQLerSafeModel` and their lite twins); `TrackedModel` on an async
model raises `TypeError` when the class is defined. A msgspec model cannot take either:
msgspec refuses the mixin when the class is defined.

```python
from sqler import PartialUpdateMixin, TrackedModel

class Config(PartialUpdateMixin, TrackedModel, SQLerModel):
    key: str
    value: str
    description: str = ""

config = Config(key="theme", value="dark").save(db=db)
config.value = "light"
config.save_partial(db=db)  # writes only `value`; `key` and `description` stay as the row has them
```

`save_partial(db=None)` writes the fields that differ from the row as the model last loaded
or saved it, and leaves the row's other fields as they are, so two instances of one row that
change different fields both land. It goes through `save()`:

- the row is the one `save()` writes: the database given or bound, and the table the row was
  read from (`using(db, table)`);
- each changed value is stored as `save()` stores it (a `datetime`, `Decimal`, `timedelta`, a
  reference, a nested model, a promoted column, ...); a value with no stored form raises
  `UnstorableValueError` naming its field. The changed fields go in one `UPDATE`;
- on a safe model the version is checked and bumped as `save()` does: a stale partial save
  raises `StaleVersionError` (or is rebased where `_rebase_config` allows it);
- the mixins around `save()` run as for `save()`: `TimestampMixin` moves `updated_at`, the
  audit log records the changed fields in the same transaction, `CacheAwareModel`
  invalidates its table, `SearchableMixin` re-indexes, hooks run;
- a row that is gone raises `NotFoundError` (a write of some fields cannot recreate it);
  a model never saved is saved in full; with no changed field nothing is sent;
- a key in the row that the model has no field for (a row written by another version of
  the program) is left as it is; a full `save()` writes the model's own document and drops
  it. A field the row did not hold yet is added at the end of the document, so the stored
  text can order its keys differently from a full save's; the values are the same;
- a field whose name holds a double quote or a backslash (possible only through
  `pydantic.create_model`) raises `InvalidFieldNameError` before anything is sent: save
  such a row with `save()`.

Your own `save()` override on such a model must take and pass on keyword arguments
(`def save(self, **kwargs): ... return super().save(**kwargs)`): the partial save reaches
the base through it, and an override that takes none raises `TypeError` on
`save_partial()`.

After it the model is clean (`is_dirty` is False); a partial save that raised leaves it dirty.
The async twin is `AsyncPartialUpdateMixin` with `AsyncTrackedModel`:
`await config.save_partial(db=db)`.

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

## Error Handling

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
  - `UniqueConstraintError` — unique constraint violated (also a `sqlite3.IntegrityError`; see [D02](#d02-a-unique-index-that-refuses-a-row-raises-uniqueconstrainterror))
- **Model errors**
  - `NotBoundError` — model not bound to database
  - `NotFoundError` — model instance not found
  - `ValidationError` — model validation failed
  - `UnstorableValueError` — a value has no stored form (a kind sqler does not store; a `Decimal` or `timedelta` no number holds exactly, a `timedelta` being stored as its number of seconds; or `bytes` in a JSON document: promote the field to a `BLOB` column, or store text); `value` is the value and `field` its place in the document, where known; also a `TypeError`
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
- Increase `timeout_ms` (default 5000), e.g. `SQLerDB.on_disk("app.db", timeout_ms=15000)` or
  `AsyncSQLerDB.on_disk("app.db", timeout_ms=15000)` (`acquire_timeout` is a different wait)
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
