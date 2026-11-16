# sqler API Reference

> The examples `[C01]` to `[C37]` have a hand-written test each in `tests/test_readme.py`; the
> blocks of the [Database](#database) section are run as written by `tests/test_ensure_table.py`.

## Table of Contents

- [Core — Models, Save, Query](#core--models-save-query)
- [Model Backends](#model-backends)
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

> **Nothing is bound to a class.** A query goes through `Model.using(db)`; every other class
> call takes `db=` (`save(db=)`, `delete(db=)`, `Model.from_id(id, db=)`,
> `Model.add_index(field, db=)`, ...). Every example below passes the database that way; see
> [Per-Call DB Binding](#per-call-db-binding). Binding a class to a database is deprecated:
> see [`set_db()` and `bind()` (deprecated)](#set_db-and-bind-deprecated).

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

kyoto = Prefecture(name="Kyoto", region="Kansai", population=2_585_000, foods=["matcha","yudofu"]).save(db=db)
osaka = Prefecture(name="Osaka", region="Kansai", population=8_839_000, foods=["takoyaki"]).save(db=db)
shiga = Prefecture(name="Shiga", region="Kansai", population=1_413_000, foods=["funazushi"]).save(db=db)

City(name="Kyoto City", population=1_469_000, prefecture=kyoto).save(db=db)
City(name="Osaka City", population=2_750_000, prefecture=osaka).save(db=db)
City(name="Otsu", population=343_000, prefecture=shiga).save(db=db)

big = Prefecture.using(db).filter(F("population") > 1_000_000).order_by("population", desc=True).all()
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
    await AUser(name="Ada", age=36).save(db=db)
    adults = await AUser.using(db).filter(F("age") >= 18).order_by("age").all()
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

# Create / save: the table is "users" (set `__tablename__` on the class to name another)
u = User(name="Alice", age=30)
u.save(db=db)
print(u._id)  # assigned _id

# Query
adults = User.using(db).filter(F("age") >= 18).order_by("age").all()
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

    u = AUser(name="Ada", age=36)
    await u.save(db=db)

    adults = await AUser.using(db).filter(F("age") >= 18).order_by("age").all()
    print([a.name for a in adults])

    await db.close()

asyncio.run(main())
```

### Per-Call DB Binding

A class is not bound to a database: the call names it, with `using()` (queries) and `save(db=)` / `delete(db=)` (instance writes):

```python
# Class-level: using() returns a queryset on db (no global mutation)
users = User.using(db).filter(F("age") > 30).all()
users = User.from_ids([1, 2], db=db)
user = User.from_id(1, db=db)

# One query on another table; the class binding is not changed
archived = User.using(db, "users_archive").all()

# Instance-level: target a specific DB for writes
user = User(name="Alice", age=30)
user.save(db=db1)       # writes to db1
user.delete(db=db2)     # deletes from db2

# Every other class call takes db= too
User.add_index("email", unique=True, db=db)
broken = User.validate_references(db=db)
live = Document.active(db=db).all()       # SoftDeleteMixin
hits = Post.search("python", db=db)       # SearchableMixin
import_jsonl(User, "users.jsonl", db=db)  # every import that takes a model class

# Works with all model variants: SQLerModel, AsyncSQLerModel,
# SQLerLiteModel, AsyncSQLerLiteModel, SQLerMsgspecModel, SafeModel, AsyncSafeModel
```

Both patterns keep the database in the call, so they are safe for multi-DB scenarios.

**Every class call names its database.** A query goes through `using(db)` (`query()`,
`all()`, `first()`, `count()` and `filter()` are its bound forms). Every other class call
takes `db=None`, keyword-only, after its other parameters:

- every base: `from_id(id_, *, db=None)`, `from_ids(ids, *, db=None)`,
  `save_many(instances, *, db=None)`, `first_or_create(lookup, defaults=None, *, db=None)`,
  `add_index(field, *, unique=False, name=None, where=None, db=None)`,
  `ensure_index(field, *, unique=False, name=None, where=None, db=None)`;
- the pydantic bases: `validate_references(*, db=None)`;
- `SoftDeleteMixin` and `AsyncSoftDeleteMixin`: `active(*, db=None)`,
  `with_deleted(*, db=None)`, `only_deleted(*, db=None)`, each the queryset of
  `using(db)` narrowed;
- `SearchableMixin`: `search(query, *, limit=100, offset=0, plain=False, db=None)`,
  `search_ranked(query, *, limit=100, offset=0, plain=False, db=None)`,
  `search_count(query, *, plain=False, db=None)`, `rebuild_search_index(*, db=None)`,
  `drop_search_index(*, db=None)`; `create_search_index(db=None)` takes it positionally too;
- `sqler.export`: `import_csv`, `import_json`, `import_jsonl`, `import_csv_string`,
  `import_json_string`, `import_jsonl_string` and `async_import_jsonl`, each `..., db=None)`.

Given `db`, a call reads and writes the class's table (the rule below) in that database,
also on a class bound to another one, whose database it leaves as it was. The one thing
read from elsewhere is a referenced row: it is read from the database its own class is
bound to when it has one, else from `db` (see [C47]). Without `db`, the call uses the
class's `set_db()` binding as before; with neither it raises `NotBoundError`, whose
message says this rule (the search calls raise `ValueError`, as `FTSIndex` does). An async
class given anything but an `AsyncSQLerDB`, and a sync class given anything but a `SQLerDB`
(a subclass of either is taken as its parent is), raise `TypeError` naming the class, the
type given and the database the class takes, before any statement runs and with the
instance and the class as they were: on every call above but the search calls (which
raise as `FTSIndex` does), and on `save()`, `delete()`, `refresh()`, `using()`, `set_db()`
and `bind()` (these two let `None` through, as before).

The search calls given `db` use an index of the class's definition (the class's index's
fields, name, tokenizer and `content_sync`, else its `FTS` configuration) on that database,
and never create it: `create_search_index(db)` does. On a database that does not have the
index they raise what a search of a missing index raises, `sqlite3.OperationalError`
("no such table"); the class's own index, made on another database, is not used.
`rebuild_search_index(db=)` on a database that has no table of the class indexes nothing
and returns 0, as `FTSIndex.rebuild()` does. `drop_search_index(db=)` drops the index of `db` and forgets the class's index when that
index is the one of `db`.

**What "the async classes match the sync ones" means.** One test holds it:
`tests/test_public_names.py::test_an_async_class_has_every_public_name_of_its_sync_twin`. It
takes 14 pairs of sync and async classes (`SQLerDB`/`AsyncSQLerDB`,
`SQLiteAdapter`/`AsyncSQLiteAdapter`, `SQLerQuery`/`AsyncSQLerQuery`,
`SQLerQuerySet`/`AsyncSQLerQuerySet`, the four pydantic and lite model bases with their async
twins, `MigrationRunner`, `Migration`, `TrackedModel`, `PartialUpdateMixin`, `HooksMixin` and
`AuditLogMixin` with their async twins) and checks that every public name (one with no
leading underscore) of the sync class is on the async class, under the same name or with an
`a` in front. It checks names only: not signatures, not behaviour, not private names, not
names that only the async class has, and not classes or functions outside those pairs.

**An instance belongs to the database it was read from or written to.** Every way of
loading a row (`from_id()`, `from_ids()`, a queryset's `all()`, `first()`, `iter()`,
`update_one()`, `update_n()`, `refresh()`, a referenced row loaded with its parent, a
search) gives the instance its home: the database and table of the row. Each successful
`save()`, `save_partial()` and `save_many()` makes the database and table written to its
home; a write that fails leaves the home as it was. A `save()` of an instance with an
`_id` whose row is not in the database written to (deleted since it was read, or never
there, as with `save(db=other)`) writes no row and raises `NotFoundError` naming the
table and the id (a safe model raises `StaleVersionError`); the instance keeps its `_id`,
its changes and its home. To put a copy of a row into another database, save a new
instance without the `_id` there (`Model(**fields).save(db=other)`). `save()`, `refresh()`, `delete()`
and `get_audit_log()` use, in this order: the `db=` given, else the instance's home,
else the class's bound database, else they raise `NotBoundError`. So a class bound
to one database never writes a row read from another into its own, and a class bound to
none needs `db=` only on the call that first reads or saves an instance. `refresh(db=)`
reads from that database and moves the home there. A deleted instance has no `_id` and
keeps its home: a later `save()` inserts it there again. A new instance (`Model(...)`)
has no home. The home is not a field: it is in no `==`, `hash`, `repr`, dump or stored
document; `copy.copy()`, `copy.deepcopy()` and `model_copy()` keep the same database
object; a pickled instance has none. `dataclasses.replace()` (lite) gives a new instance
with no `_id`; `msgspec.structs.replace()` keeps the `_id` but not the home, so read the
row again instead of saving such a copy.

**Which table a class uses.** Every route (`save(db=)`, `using(db)`, references,
`FTSIndex`, the registry) uses one rule: the table the class is bound to (see
[`set_db()` and `bind()` (deprecated)](#set_db-and-bind-deprecated)), else
`__tablename__`, else the plural of the class name (`User` → `users`; a name that
already ends in `s` is kept). A class bound to the table `a` reads `a` from any
database given to `using()`.

**`using(db, table=None)`.** With `table`, that one query reads the named table; the
name is checked before any SQL (`InvalidTableNameError`). The table is part of the
home of a row read this way, so a row is never written to a different table than it was
read from; `row.save(db=other_db)` updates the row with the same `_id` in that table of
`other_db`. `as_ref(row)`, and the reference a model holding it stores, name that table.
On a versioned (safe) class, `using()` reads each row's version as `query()` does, so a
set-based update through either moves the version and a stale `save()` is refused.

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

**References.** A model saved with `save(db=db)` can be held by
another model; read back through `using(db)`, the related model is in place. Model
classes are registered under their table when they are defined, so a stored reference
finds its class even when that class was defined after the row was written. A class
that is only defined never takes the place of a class bound to the same table
(a later import of a same-named class, a class defined inside a function, a
subclass); binding a class always registers it. When the referenced row cannot be read because
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

### `set_db()` and `bind()` (deprecated)

`Model.set_db(db, table=None)` binds a class to one database and table, and the lite models'
`bind()` is its alias. Both are deprecated: each emits a `DeprecationWarning` that states the
rule of "Every class call names its database". Pass the database to the call instead:

| Instead of | Write |
|------------|-------|
| `Model.set_db(db)` then `Model.query()` | `Model.using(db)` |
| `Model.set_db(db)` then `obj.save()` | `obj.save(db=db)` |
| `Model.set_db(db)` then `Model.from_id(1)` | `Model.from_id(1, db=db)` |
| `Model.set_db(db, "archive")` | `__tablename__ = "archive"` on the class, or `Model.using(db, "archive")` for one query |

`set_db()` also creates the class's table, registers the class under it and, with
`__promoted__`, adds the promoted columns; a class that is never bound does the same at its
first save. A class that is bound keeps working: a call given no `db` uses its binding, and a
call given `db` uses `db` and leaves the binding as it was.

## Model Backends

A model class has one of three kinds of base, each with the names below. An empty cell means
that base does not exist.

| Backend (needs) | Plain, sync | Safe, sync | Plain, async | Safe, async |
|-----------------|-------------|------------|--------------|-------------|
| pydantic | `SQLerModel` | `SQLerSafeModel` | `AsyncSQLerModel` | `AsyncSQLerSafeModel` |
| lite (nothing) | `SQLerLiteModel` | `SQLerLiteSafeModel` | `AsyncSQLerLiteModel` | `AsyncSQLerLiteSafeModel` |
| msgspec | `SQLerMsgspecModel` | | | |

A safe model keeps a version for [optimistic locking](#c06-optimistic-versioning). Lite models
are dataclasses and need neither pydantic nor msgspec (for Pyodide, say). `SQLerLiteModelBase`
and `SQLerMsgspecModelBase` are the classes the lite and msgspec models above are built on:
they hold the pydantic-like part of a model (`_id`, the stored snapshot, `model_validate()`,
`model_dump()`, `get_field_names()`) and no database methods (`save()`, `query()`, ...). Subclass
`SQLerLiteModel` or `SQLerMsgspecModel` for a model you store; `is_lite_model()` and
`is_msgspec_model()` test against the base, so any subclass of it counts.

`PYDANTIC_AVAILABLE` and `MSGSPEC_AVAILABLE` are `True` when the package imports, and
`PYDANTIC_VERSION` and `MSGSPEC_VERSION` hold its version string (`None` when it is missing).
`get_model_backend()` returns `"pydantic"` when pydantic is installed and `"dataclass"` when it
is not; it does not look for msgspec. `is_pydantic_model(cls)`, `is_lite_model(cls)` and
`is_msgspec_model(cls)` say whether a class is a pydantic model, a lite model or a msgspec
model; each gives `False` for any other class, and `is_pydantic_model()` and
`is_msgspec_model()` give `False` when their package is missing. `require_pydantic(feature)`
and `require_msgspec(feature)` return `None` when the package is installed and raise
`ImportError` naming `feature` when it is not.

```python
from dataclasses import dataclass
from sqler import (
    SQLerModel, SQLerLiteModel, SQLerMsgspecModel,
    get_model_backend, is_pydantic_model, is_lite_model, is_msgspec_model,
    require_pydantic, require_msgspec,
)

print(get_model_backend())  # pydantic

class P(SQLerModel):
    name: str

@dataclass
class L(SQLerLiteModel):
    name: str

class M(SQLerMsgspecModel):
    name: str

print([(c.__name__, is_pydantic_model(c), is_lite_model(c), is_msgspec_model(c)) for c in (P, L, M, dict)])
# [('P', True, False, False), ('L', False, True, False), ('M', False, False, True), ('dict', False, False, False)]

require_pydantic("My feature")  # returns None: pydantic is installed
require_msgspec("My feature")
```

Without pydantic, `sqler.SQLerModel` and the other pydantic names are placeholders: calling
one, or subclassing it, calls `require_pydantic()` and raises `ImportError`.

---

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
Order(customer="C1", items=[{"sku":"RamenSet","qty":3}, {"sku":"Gyoza","qty":1}]).save(db=db)
Order(customer="C2", items=[{"sku":"RamenSet","qty":1}]).save(db=db)

expr = F(["items"]).any().where((F("sku") == "RamenSet") & (F("qty") >= 2))
hits = Order.using(db).filter(expr).all()
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

Prefecture(name="A", region="x", population=10).save(db=db)
Prefecture(name="B", region="x", population=2_000_000).save(db=db)

db.create_index("prefectures", "population")
Prefecture.ensure_index("population", db=db)

q = Prefecture.using(db).filter(F("population") >= 1_000_000)
sql, params = q.debug()
assert isinstance(sql, str) and isinstance(params, list)

plan = q.explain_query_plan(db.adapter)
assert plan and len(list(plan)) >= 1
```

`add_index(field, *, unique=False, name=None, where=None, db=None)` and `ensure_index(...)`
with the same parameters index the class's table in `db` when it is given, else in the
class's bound database: `Prefecture.ensure_index("population", db=db)` needs no binding.

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

QueryUser(name="Ada", age=36, tags=["pro", "python"], tier=1).save(db=db)
QueryUser(name="Bob", age=20, tags=["hobby"], tier=3).save(db=db)

QueryOrder(customer="Ada", items=[{"sku": "ABC", "qty": 3}]).save(db=db)
QueryOrder(customer="Bob", items=[{"sku": "XYZ", "qty": 1}]).save(db=db)

q1 = QueryUser.using(db).filter(F("tags").contains("pro"))
assert [u.name for u in q1.all()] == ["Ada"]

q2 = QueryUser.using(db).filter(F("tier").isin([1, 2]))
assert [u.name for u in q2.all()] == ["Ada"]

q3 = QueryUser.using(db).exclude(F("name").like("test%"))
assert {u.name for u in q3.all()} == {"Ada", "Bob"}

expr = F(["items"]).any().where((F("sku") == "ABC") & (F("qty") >= 2))
q4 = QueryOrder.using(db).filter(expr)
assert [o.customer for o in q4.all()] == ["Ada"]

sql, params = QueryUser.using(db).filter(F("age") >= 18).debug()
assert isinstance(sql, str) and params == [18]

plan = QueryUser.using(db).filter(F("age") >= 18).explain_query_plan(db.adapter)
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

Product(name="Apple", price=1.50, quantity=100).save(db=db)
Product(name="Banana", price=0.75, quantity=150).save(db=db)
Product(name="Cherry", price=3.00, quantity=50).save(db=db)

q = Product.using(db)
assert q.sum("quantity") == 300
assert q.avg("price") == 1.75
assert q.min("price") == 0.75
assert q.max("price") == 3.00

# With filters
expensive = Product.using(db).filter(F("price") > 1.0)
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

Item(name="Widget", active=True).save(db=db)

assert Item.using(db).filter(F("active") == True).exists() == True
assert Item.using(db).filter(F("name") == "Missing").exists() == False
```

### [C25] Pagination

```python
from sqler import SQLerDB, SQLerModel, PaginatedResult

class Article(SQLerModel):
    title: str
    views: int = 0

db = SQLerDB.in_memory()

for i in range(25):
    Article(title=f"Article {i}", views=i * 10).save(db=db)

# Get page 2 with 10 items per page
page = Article.using(db).order_by("views", desc=True).paginate(page=2, per_page=10)

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

Employee(name="Alice", age=25, email="alice@example.com").save(db=db)
Employee(name="Bob", age=35, email="bob@test.org").save(db=db)
Employee(name="Charlie", age=45, email="charlie@example.com").save(db=db)

# between (inclusive)
mid_age = Employee.using(db).filter(F("age").between(30, 40)).all()
assert [e.name for e in mid_age] == ["Bob"]

# startswith
alice = Employee.using(db).filter(F("name").startswith("Al")).all()
assert [e.name for e in alice] == ["Alice"]

# endswith
example_emails = Employee.using(db).filter(F("email").endswith("@example.com")).all()
assert len(example_emails) == 2

# is_null / is_not_null
Employee(name="NoEmail", age=30, email="").save(db=db)  # empty but not null
all_with_email = Employee.using(db).filter(F("email").is_not_null()).all()
assert len(all_with_email) == 4
```

### [C32] NULL-safe comparison

```python
from sqler import SQLerDB, SQLerModel, SoftDeleteMixin
from sqler.query import SQLerField as F

class SoftUser(SoftDeleteMixin, SQLerModel):
    name: str

db = SQLerDB.in_memory()

active = SoftUser(name="Active").save(db=db)
deleted = SoftUser(name="Deleted").save(db=db)
deleted.soft_delete()

# F("field") == None generates IS NULL (correct SQL)
# F("field") != None generates IS NOT NULL
active_users = SoftUser.using(db).filter(F("deleted_at") == None).all()
deleted_users = SoftUser.using(db).filter(F("deleted_at") != None).all()

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

Status(code="A", label="Active").save(db=db)
Status(code="P", label="Pending").save(db=db)
Status(code="C", label="Closed").save(db=db)
Status(code="D", label="Draft").save(db=db)

# in_list with values
open_statuses = Status.using(db).filter(F("code").in_list(["A", "P"])).all()
assert len(open_statuses) == 2

# Empty list returns nothing
empty = Status.using(db).filter(F("code").in_list([])).all()
assert len(empty) == 0
```

### Expressions and querysets: `SQLerExpression`, `SQLerQuerySet`, `AsyncSQLerQuerySet`, `AsyncSQLerQuery`

A comparison on a field (`F("age") >= 21`, `F("name").like("A%")`, ...) is a
`SQLerExpression`: a piece of SQL (`.sql`) with its parameters (`.params`). `&` joins two
expressions with AND, `|` with OR and `~` negates one; the parameters follow in order.
`filter()`, `exclude()`, `or_filter()` and `having()` take one.

`Model.query()` and `Model.using(db)` return a `SQLerQuerySet` on a sync model of any base
(pydantic, lite, msgspec), and an `AsyncSQLerQuerySet` on an async model (its `all()`,
`first()`, `count()` and the others are awaited). A queryset wraps a query on one table and
gives back model instances. The query it wraps is a `SQLerQuery`, or on the async side an
`AsyncSQLerQuery`, which `db.query(table)` also gives (`await db.query(table)` on an
`AsyncSQLerDB`): the same chain of `filter()`, `order_by()`, `limit()` and so on, but `all()` and
`first()` return the stored JSON text of each row, and `all_dicts()` the parsed documents.

```python
from sqler import SQLerDB, SQLerModel, SQLerExpression, SQLerQuerySet
from sqler.query import SQLerField as F

class User(SQLerModel):
    name: str
    age: int

db = SQLerDB.in_memory()
User(name="Ada", age=36).save(db=db)
User(name="Bob", age=20).save(db=db)

adult = F("age") >= 21
named = F("name") == "Ada"
assert isinstance(adult, SQLerExpression)
both = adult & named          # AND
neither = ~adult              # NOT
print(both.sql, both.params)
# (JSON_EXTRACT(data, '$.age') >= ?) AND (JSON_EXTRACT(data, '$.name') = ?) [21, 'Ada']
print(neither.sql, neither.params)
# NOT (JSON_EXTRACT(data, '$.age') >= ?) [21]

qs = User.using(db).filter(both)
assert isinstance(qs, SQLerQuerySet)
print([u.name for u in qs.all()])  # ['Ada']
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
home = Address(city="Kyoto", country="JP").save(db=db)
user = User(name="Alice", address=home).save(db=db)

got = User.from_id(user._id, db=db)
assert got.address.city == "Kyoto"

qs = User.using(db).filter(User.ref("address").field("city") == "Kyoto")
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

home = Address(city="Kyoto", country="JP").save(db=db)
user = User(name="Alice", address=home).save(db=db)

loaded = User.from_id(user._id, db=db)
assert loaded.address.city == "Kyoto"

q = User.using(db).filter(User.ref("address").field("city") == "Kyoto")
assert [row.name for row in q.all()] == ["Alice"]
```

### [C47] How references are loaded

A stored reference is a dict of exactly two keys, `{"_table": <str>, "_id": <int>}` (an
`_id` that is a `bool`, a float or a string makes it no reference); any other dict, one
with these keys and more too, is data: stored and read whole, never followed, never a
referrer (`sqler.utils.is_ref_dict`). In a field whose type hint
names a model class (`Address | None`, `list[Address]`, `dict[str, Address]`), a reference
into a table of that class is loaded as the model of its row. Every way of loading
(`from_id`, `refresh`, `from_ids`, `query().all()`, `query().first()`, sync and async, on
every model base) loads the same way:

- **Followed to the end.** A chain of rows loads whole, however long: one `SELECT` per table
  per level, never one per row of a level.
- **One object per row per load.** A row reached twice is the same object: a diamond shares
  its child, a cycle closes (`a.next.next is a`), and a row of the result that another row
  references is that result. `refresh()` counts its own instance as the object of its row.
- **A row that is gone** reads as `None` in a field and is left out of a list.
- **A row its class refuses** raises what loading that row by its own `from_id` raises, with
  a note naming the row, its table and the field that referenced it.
- **Without loading** (`.resolve(False)`, `iter()`) the field holds the stored reference,
  whatever its type hint says; saving the model writes it back unchanged.
- A referenced row of a class bound to a database is read from that database, where its
  `save()` writes it back; a row of a class bound to none from the database of the row that
  references it.

In a cycle the model that closes it is the object its row becomes once its own fields are
validated: a `mode="before"` validator of the referring field receives that object before
its fields are set (a pydantic one holds each field's default, a lite one no field, a
msgspec one `None` in every field), a
`mode="after"` one sees it validated. A required reference field closes the same way. The
row is then built on that object, so it holds every field of its row whatever way its class
keeps its state (`slots=True`, frozen, private attributes), and a `model_post_init`,
`__post_init__` or after-validator that keeps `self` keeps the object the graph holds.
pydantic runs a class's after-validators on every instance of it that it validates as a
field value, so such a validator also sees the closing object before it is built. A
msgspec model can only be made new by its class, so its closing object is made without
`__post_init__` running on it, the row is then built as msgspec builds it, and its state is
moved onto the closing object: `__post_init__` runs once for the row, on the row's values,
and a hook that keeps `self` there keeps the object msgspec built, not the one the graph
holds. A class that defines its own `model_validate`, and a pydantic validator that returns
another object than the one it was given, are treated the same way: the model is built the
class's way and its state is moved.
`repr()` prints a model met again inside itself as `...`; `model_dump()` of a cycle raises
`ValueError` (pydantic's), on the lite bases too. Two limits: on a msgspec model in a cycle
`repr()` and `model_dump()` recurse until `RecursionError`, and `refresh()` fails on a
frozen pydantic or frozen msgspec model (every other way of loading a frozen class works).

```python
from typing import Optional

from sqler import SQLerDB, SQLerModel

class Node(SQLerModel):
    name: str = ""
    next: Optional["Node"] = None

Node.model_rebuild()
db = SQLerDB.in_memory()
a = Node(name="a").save(db=db)
b = Node(name="b", next=a).save(db=db)
a.next = b
a.save()

got = Node.from_id(a._id, db=db)
assert got.next.name == "b" and got.next.next is got
assert Node.using(db).resolve(False).first().next == {"_table": "nodes", "_id": b._id}
```

### `register()`: the class a table's rows belong to

Defining a model class registers it under its table (see "References" above).
`register(table, cls)` does that by hand: afterwards `resolve(table)` gives
`cls` (and `tables()` holds it), which is the class a stored reference to that table loads as.
A table registered again keeps the latest class.

```python
from sqler import SQLerModel, register, resolve, tables

class Note(SQLerModel):
    text: str

register("notes_table", Note)
print(resolve("notes_table") is Note, tables()["notes_table"] is Note, resolve("nothing"))
# True True None
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

acc = Account(owner="Ada", balance=100).save(db=db)
acc.balance = 120
acc.save()

table = "accounts"
db.adapter.execute(f"UPDATE {table} SET _version = _version + 1 WHERE _id = ?;", [acc._id])
db.adapter.commit()

acc.balance = 130
try:
    acc.save()
except StaleVersionError:
    pass
else:
    raise AssertionError("stale writes must raise")
```

The version of a row is its table's `_version` column, and every write sqler makes of a row
of a table that has it moves it by one, whatever writes it: a safe or a plain class's
`save()` and queryset `update()`, `update_one()`, `update_n()`, `db.query(table).update()`,
the same three of a query built from the adapter alone (`SQLerQuery(table, db.adapter)`,
which reads the table's columns before each update), `upsert_document()`, `bulk_upsert()`,
`upsert_with_version()`, a delete policy's `set_null`. An insert (`save_many()`,
`insert_document()`) writes 0. So a later `save()` of an instance loaded before any of them
raises `StaleVersionError`; an instance loaded after them, or returned by `update_one()` /
`update_n()`, holds the new version and saves. A `_version` key in a document written to
such a table is not stored and sets nothing, and `update(_version=...)` on it is refused.
Every read of such a table through a database object or a model (`find_document`, the
dicts of `db.query(table)` or of a model's query set, a model) gives `_version` from the
column. A statement sent by hand with `db.adapter.execute()` moves it only if it
says so, as above.

A table made before versioning (by a plain model, by SQL, or copied without its `_version`
column) is read as it is, through every door (`from_id`, `from_ids`, `refresh`, a
reference, `query()`, `using(db)`): each row at version 0, and no read changes the schema,
so it works on a database that cannot be written. The first versioned write adds the
column: a `save()` (a holder who read at version 0 then saves) or a set-based update. A
database object asks
once whether a table has the column, with the table's other columns (see "Promoted columns"
in `docs/STORAGE.md`), and learns again when SQLite refuses a statement built for them.

### [C13] Safe model collision handling

```python
from sqler import SQLerDB, SQLerSafeModel, StaleVersionError

class Account(SQLerSafeModel):
    owner: str
    balance: int

db = SQLerDB.on_disk("bank.db")

acc = Account(owner="Ada", balance=100)
acc.save(db=db)            # _version == 0

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

# Permissive: increments can be rebased
c = Counter(value=0, count=0).save(db=db)
c.value += 1
c.count += 1
c.save()  # Works even if version changed (for small deltas)

# Strict: no automatic rebasing
s = StrictCounter(value=0).save(db=db)
s.value += 1
s.save()
assert s._version == 1
```

A safe model with no `_rebase_config` uses `DEFAULT_REBASE_CONFIG`: rebasing is on, but only for
the field named `count`, and only for a change of 1 (`max_delta=1`). `PERMISSIVE_REBASE_CONFIG`
widens that to any field (still a change of 1), and `NO_REBASE_CONFIG` turns it off.

```python
from sqler import DEFAULT_REBASE_CONFIG

print(DEFAULT_REBASE_CONFIG.allowed_fields, DEFAULT_REBASE_CONFIG.max_delta)  # {'count'} 1
print(DEFAULT_REBASE_CONFIG.can_rebase("count", 1))  # True
print(DEFAULT_REBASE_CONFIG.can_rebase("count", 2))  # False
print(DEFAULT_REBASE_CONFIG.can_rebase("value", 1))  # False
```

---

## Bulk Operations & Transactions

### [C07] `bulk_upsert` — one id per input, order preserved

```python
from sqler import SQLerDB, SQLerModel

class BU(SQLerModel):
    name: str
    age: int | None = None

db = SQLerDB.in_memory()

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

rows = [{"name": "A"}, {"name": "B"}, {"_id": 42, "name": "C"}]
ids = db.bulk_upsert("bulkusers", rows)
assert len(ids) == 3 and 42 in ids
```

### [C22] Transaction context manager

```python
from sqler import SQLerDB, SQLerModel

class TxUser(SQLerModel):
    name: str
    balance: int = 0

db = SQLerDB.in_memory()

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

assert TxUser.using(db).count() == 2

# Explicit commit/rollback methods
tx = db.transaction()
tx.__enter__()
db.insert_document("txusers", {"name": "Charlie", "balance": 300})
tx.commit()

assert TxUser.using(db).count() == 3
```

### [C30] Transaction-aware model.save()

```python
from sqler import SQLerDB, SQLerModel

class TxItem(SQLerModel):
    name: str

db = SQLerDB.in_memory()

# Saves inside transaction respect rollback
try:
    with db.transaction():
        TxItem(name="A").save(db=db)
        TxItem(name="B").save(db=db)
        raise RuntimeError("abort!")
except RuntimeError:
    pass

# Nothing was saved due to rollback
assert TxItem.using(db).count() == 0

# Without transaction, saves commit immediately
TxItem(name="C").save(db=db)
TxItem(name="D").save(db=db)
assert TxItem.using(db).count() == 2
```

An instance is not part of your transaction. After a rollback it keeps what the write gave it:
an instance first saved inside the transaction has an `_id` that no row has, and an instance
deleted inside it has no `_id` while its row is still there. Read the row again
(`from_id()`, a query) before going on with it.

### `commit()`, `auto_commit()` and the adapter block

Only the end of a transaction opened by `begin_transaction` (the `db.transaction()` block,
or `adapter.end_transaction()`) ends it; the same rule holds on the sync and the async
adapter. Outside such a transaction, `adapter.commit()` commits what the thread (async:
the task) wrote on its connection, and the adapter block (`with db.adapter:` /
`async with db.adapter:`) commits its writes when it ends, or rolls them back when an
exception leaves it. Inside one (a transaction of this thread; async: of this task or
one it was started inside):

- `adapter.commit()` raises `TransactionStateError` and commits nothing; the transaction
  goes on and can still be used. A transaction whose nested end failed raises that
  failure's error instead.
- `adapter.auto_commit()` does nothing: it is the call for "commit unless a transaction
  is open".
- The adapter block neither commits nor rolls back when it ends; an exception leaves
  through it to the transaction's block, and what the block wrote is the transaction's
  (it is kept if the transaction commits).

### Raw statements and transactions

A raw statement sent through `adapter.execute()` or `adapter.executemany()` may do anything
SQLite allows except end or open a transaction behind the adapter; the same holds on the sync
and the async adapter, and SQLite's own state after the statement decides, not its text.

- Inside a transaction: a statement that ends it (a raw `COMMIT`, `END` or `ROLLBACK`, or a
  failure SQLite answers by rolling the whole transaction back, such as `ON CONFLICT
  ROLLBACK`) raises `TransactionStateError` at that statement. What was written before it
  was committed or rolled back by it and cannot be put back; every later statement in the
  transaction and the block's end raise the same error and send nothing. End a transaction
  by leaving its `db.transaction()` block, and raise in the block to roll it back. Raw
  `SAVEPOINT` work that leaves the transaction open is not refused.
- Outside a transaction: raw work that SQLite holds in a transaction (a raw `BEGIN`, or a
  raw write not committed yet) stays pending on the connection (async: on the task's pinned
  connection, which no other task gets). `db.transaction()` and `begin_transaction()` then
  raise `TransactionStateError` and send nothing: keep the work with `db.adapter.commit()`
  or drop it with `db.adapter.execute("ROLLBACK")`, then begin again. A write that opens no
  transaction of its own (a plain `save()`) commits the pending work with its own, as
  `sqlite3` does. Outside a transaction, what was sent since the last commit is one unit: a
  statement that fails rolls all of it back (the raw work with it) and raises its error.

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

### A rollback that fails

When the `ROLLBACK` that ends a transaction fails in SQLite (a progress handler or
`interrupt()` stops it, say), at the end of a block, after a failed commit or after a failed
statement, the sync adapter tries it once more at once. If that fails too, no later
statement is ever sent into that transaction. A thread's own connection (a file, a named
shared-cache memory database) is closed, which rolls back and frees the write lock, and a
warning is logged; the thread opens a new connection at its next statement, and the block's
own exception leaves as it is. (A cursor of the closed connection that still has rows to
read keeps SQLite from letting go of it: until that cursor is closed or dropped, writers
get `database is locked`; the block's rows are not committed either way.) An interrupt
(`KeyboardInterrupt`) that arrives between the two tries is raised on after the same has
been done. The one connection of a plain `":memory:"` database cannot be
closed without losing the database: it keeps the transaction, and its thread stays the
holder. The end raises `TransactionStateError` with SQLite's error as `__cause__` (a block
that is already raising keeps its own exception, with the sentence as a note); every later
statement of any thread tries the rollback again first: until one succeeds, the holder's
statements raise the same error and the other threads wait as for any open transaction. A
thread that ends as the holder is treated the same way. The async adapter closes such a
connection and puts a fresh one in its place in the pool.

### How long a statement waits for a lock

`timeout_ms` (default 5000) is how long a statement waits for another connection's lock
before it raises `sqlite3.OperationalError: database is locked`. Pass it to
`SQLiteAdapter(...)`, `AsyncSQLiteAdapter(...)`, their `on_disk()` and `in_memory()`, or
`SQLerDB`/`AsyncSQLerDB` `.on_disk()` and `.in_memory()`; it is SQLite's `busy_timeout` on
every connection the adapter opens (every connection of an async pool), and on a plain
sync `":memory:"` database also the wait for another thread's transaction. On a
shared-cache in-memory database (the default of `in_memory()`), where SQLite calls no busy
handler, the adapter waits for another connection's table lock itself, sync and async
alike: the statement goes through once the lock is released, or raises
`sqlite3.OperationalError: database table is locked` once `timeout_ms` has passed, and is
never run twice. `timeout_ms=0` waits for nothing. `executescript()`, and `executemany()`
given rows from an iterator rather than a list or tuple, raise at once: their earlier
statements or rows have run or been taken and cannot be sent again. The async
adapter's `close()` ends such a wait with `NotConnectedError`. The async
adapter has a second wait, `acquire_timeout` (seconds): the wait for a connection of its
own pool and for a turn in a transaction tasks share; `timeout_ms` is the wait for a lock
another connection or process holds. A `PRAGMA busy_timeout = N` in `pragmas` says the same thing:
without `timeout_ms` it sets the number, and beside a `timeout_ms` that differs `connect()`
raises `ValueError`. `connect()` on the async adapter opens its pool once: on a connected
adapter, or beside another `connect()` or a statement's own connect, it opens nothing more;
a `connect()` that fails closes every connection it opened and raises the error that made it
fail.

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

The cursor `await adapter.execute(...)` returns reads only what its statement saw. SQLite
reads a statement's rows as they are fetched; so before any statement is sent on that
connection (by the cursor's own task, a task that took it from the pool, a child task that
shares the pin, a sibling in a shared transaction) or a rollback ends its transaction, the
rows the cursor has not returned yet are read, in the connection's thread ahead of that
statement, and the cursor serves them from memory. Rows its own transaction wrote and then
rolled back are served: they are what the statement saw, and a row its own task inserts
before the first fetch is not. While nothing else is sent on the connection it reads
lazily, as SQLite does, and a cursor nobody holds any more, or one that is closed or has
returned its last row, is never read ahead. A statement whose rows raise part-way (a
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

`delete()`, `delete_with_policy()` and a queryset's `delete_all()` (each with the default
`on_delete="restrict"`) raise `ReferentialIntegrityError` and delete nothing while a row that
stays refers to a row that goes, on every base, sync and async (`await a.delete()` too).
`set_null` writes `None` in place of each such reference; `cascade` deletes the referring
rows too, and theirs.

- A row refers to another when it holds the reference at a position its class declares as a
  model: `author: U | None`, `items: list[U]`, `by_key: dict[str, U]`, nested, or a base
  class of `U`. These are the positions a load follows. Only a field declared as a model
  counts: a reference in a plain `dict`, `list` or `Any` field is data, and it neither
  refuses a delete nor is nulled or cascaded; a dict with a key more than a reference is
  data at every position. `validate_references()` still reports a reference in any field
  whose row is gone, and never a dict that is not a reference.
- The fields that count for a table are those of every class whose rows live in it: the
  class registered for it, a subclass that inherits its table, another class that names it
  (`__tablename__`) and a class whose rows were read or written there
  through `using(db, table)`. A class defined later counts from then on;
  a class bound elsewhere still counts for the table it wrote to. Only classes this
  process has defined are known: import the modules of the classes that refer to a table
  before deleting from it.
- Rows deleted together are no referrers of each other: a row that refers only to itself,
  or a parent and its children in one `delete_all()`, go through under `restrict`.
- The rows go, the look for referring rows runs and the policy acts in one transaction (a
  savepoint inside yours); a refusal or a failure leaves every row as it was, and no other
  connection writes a referrer in between.
- The look reads only the tables whose classes can hold a reference to the table, once each
  for any number of rows. A delete of a row of a class no class can refer to reads no other
  table. A class whose annotations do not resolve yet is read whole.
- A field that is the reference itself is looked up with
  `json_extract(data, '$.<field>._id')`: on a large referring table, an index on it
  (`db.create_index("posts", "author._id")`) lets the look skip the scan of the table. A
  list or a dict of references is read row by row.
- A row written by hand is read as a load reads it. One that SQLite's JSON functions
  cannot read (`NaN`, `Infinity`, text that is no JSON) is not passed over: the look then
  reads the table's rows in Python, which is slower. The one exception is a document that
  repeats a key, which sqler never writes: the look, like every query and index, reads the
  first, and a load reads the last.

```python
from sqler import SQLerDB, SQLerModel, ReferentialIntegrityError

class U(SQLerModel):
    name: str

class Post(SQLerModel):
    title: str
    author: U | None = None

db = SQLerDB.in_memory()

u = U(name="Writer").save(db=db)
Post(title="Post A", author=u).save(db=db)

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
X(name="a").save(db=db)

db.create_index("xs", "email", unique=True)
db.create_index("xs", "name", where="json_extract(data,'$.name') IS NOT NULL")
```

### [C16] Delete policies in action

The delete and what its policy does to referring rows are one unit (a savepoint inside your
transaction, if one is open): if the delete of the row is refused, the referring rows are as
they were, also when you catch the error inside your transaction and commit.

```python
from sqler import SQLerDB, SQLerModel, ReferentialIntegrityError

class DIUser(SQLerModel):
    name: str

class Post(SQLerModel):
    title: str
    author: DIUser | None = None

# restrict: raises while references exist
restrict_db = SQLerDB.in_memory()
writer = DIUser(name="Writer").save(db=restrict_db)
Post(title="Post A", author=writer).save(db=restrict_db)
try:
    writer.delete_with_policy(on_delete="restrict")
except ReferentialIntegrityError:
    pass

# set_null: clears JSON ref before delete
set_null_db = SQLerDB.in_memory()
nullable = DIUser(name="Nullable").save(db=set_null_db)
post = Post(title="Post B", author=nullable).save(db=set_null_db)
nullable.delete_with_policy(on_delete="set_null")
assert Post.from_id(post._id, db=set_null_db).author is None

# cascade: remove dependents recursively
cascade_db = SQLerDB.in_memory()
cascade = DIUser(name="Cascade").save(db=cascade_db)
Post(title="Post C", author=cascade).save(db=cascade_db)
cascade.delete_with_policy(on_delete="cascade")
assert Post.using(cascade_db).count() == 0
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

user = RefUser(name="Ada").save(db=db)
dangling = RefPost(title="Lost", author={"_table": "refusers", "_id": user._id}).save(db=db)
db.delete_document("refusers", user._id)  # simulate manual deletion

broken = RefPost.validate_references(db=db)
assert broken and broken[0].row_id == dangling._id

# Returned items are sqler.models.BrokenRef dataclasses
```

`validate_references(*, db=None)` checks `db` when it is given, else the class's
bound database.

### [C20] Index helpers

```python
from sqler import SQLerDB, SQLerModel

class IndexedUser(SQLerModel):
    name: str
    age: int | None = None
    email: str | None = None
    address: dict | None = None

db = SQLerDB.in_memory()
IndexedUser(name="Ada").save(db=db)

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
Product(sku="a-1", price=1.0).save(db=db)

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

**The document writers on a table with columns.** `insert_document`, `upsert_document`,
`insert_many`, `insert_many_with_version`, `bulk_upsert` and `upsert_with_version` put each
key the document holds that names a column beside `data` (promoted by a class, by
`ensure_table`, or added with plain SQL) into that column, the rest into `data`, so a read
gives back what was written. A column whose key the document does not hold is not written:
an update leaves its value, an insert gives it the column's `DEFAULT` (or NULL); a key held
as `None` writes NULL. A model class without `__promoted__` saves through these methods, so
its saves leave a column it has no field for as it is. `upsert_document` with an `_id`
updates that row only: when the table has no row with that `_id` it writes no row and
raises `NotFoundError`. In one call, rows that hold different
keys each get their own (consecutive rows with the same keys share one statement). A value
a column refuses (`NOT NULL`, `CHECK`) raises SQLite's `IntegrityError` and writes nothing of
the call; `bulk_upsert` of an existing row whose document lacks the key of a `NOT NULL`
column without a `DEFAULT` raises it too, because SQLite checks the column before it finds
the row. A key in another case than the column (`Status`, column `status`) stays in `data`.
A database object learns a table's columns at its first ensure, read or write of it; a
column added later by other means is seen by a new database object (see
[STORAGE.md](STORAGE.md)).

**Promoting a field on a table that has rows.** A column added to an existing table
(by `ensure_table` or the first save of a model class whose
`__promoted__` names it) takes each row's value out of the row's document and puts it
in the column, in one transaction with the `ALTER TABLE` (one pass over the table, once;
inside an open `transaction()` it is part of it). The column then holds what a `save()`
would have written there. A row whose document has no key for the field gets the default
of the model's field, as it read before; without a model, or without a default, it gets
the column's `DEFAULT` or NULL. A JSON `null` gives NULL, never the `DEFAULT`.

Where that cannot keep what a row reads, the promotion raises `SchemaError` and changes
nothing (no column, no document touched); `details` names the `table`, the `column`, how
many `rows` and the `first_id`: a list, a dict or a reference under the key; an integer
past 64 bits; `null`, or no key and a default the column cannot hold, in a `NOT NULL`
column. SQLite cannot add a `NOT NULL` column without a `DEFAULT` to an existing table:
that raises `SchemaError` too.

**A table an earlier version promoted.** Earlier versions added the column and left the
values in the documents, and a later write may have changed either. sqler records in a
table of its own, `_sqler_promoted`, which columns hold their values (see
[STORAGE.md](STORAGE.md)). A database object checks a table whose columns beside `data`
are not all recorded there once, at whichever comes first: an ensure (`ensure_table`,
the first save of a model class), a read (`find_document`,
`find_documents`, `from_id`, `from_ids`, and a model query's `all()`, `first()`,
`iter()`, `count()` and every other call that runs it, `update()` and `delete_all()`
included) or a document write (above). The check reads the table once for documents that
still hold a column's key: none, and the object remembers the table as settled; some, and the call raises
`SchemaError` (`details["columns"]` holds the rows and the first `_id` per column) and
changes nothing, because a read sets the column over the document and would hide the
document's value. That holds for a column added by other means too. Settle each column
with `move_promoted(table, column, keep=...)`: `keep="document"` puts each document's
value into the column, `keep="column"` keeps the column's values; both take the key out of
the documents (under the refusals above) and record the column.

A read writes no record. An ensure records what it found settled; when the database
takes no write then (opened read-only, or another connection holds the write lock past
the busy timeout) the table is used all the same and the next ensure writes the record. A
column that has to be added, or values that have to move, still need the write lock.
`execute_sql()` runs your own SQL and is not checked. A search index made with
`content_sync` on the table gets its triggers made again, inside the promotion, so that it
reads a moved field from its column and still finds the rows.

```python
import json
import sqlite3
import tempfile
from pathlib import Path

from sqler import SchemaError, SQLerDB

path = str(Path(tempfile.mkdtemp()) / "old.db")
conn = sqlite3.connect(path)  # a table as an earlier version left it
conn.execute("CREATE TABLE tickets (_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)")
conn.execute("INSERT INTO tickets (data) VALUES (?)", [json.dumps({"name": "a", "status": "open"})])
conn.execute("ALTER TABLE tickets ADD COLUMN status TEXT")
conn.commit()
conn.close()

db = SQLerDB.on_disk(path)
try:
    db.ensure_table("tickets", promoted={"status": "TEXT"})
except SchemaError as err:
    assert err.details == {"table": "tickets", "columns": {"status": {"rows": 1, "first_id": 1}}}
db.move_promoted("tickets", "status", keep="document")
db.ensure_table("tickets", promoted={"status": "TEXT"})
assert db.find_document("tickets", 1) == {"name": "a", "_id": 1, "status": "open"}
db.close()
```

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

### Which writes meet the mixins

Every way of writing an instance meets the rules of its class, sync and async, on every
base: `save()`, `save_partial()`, `save_many()`, `delete()`, `delete_with_policy()`,
`soft_delete()`, `restore()` and `first_or_create()` (when it creates).

| Mixin | What each of those writes does |
|-------|--------------------------------|
| `TimestampMixin` | sets `created_at` (first write) and `updated_at` on each instance written |
| `AuditMixin` | the same times, and `created_by` / `updated_by` from the current user |
| `AuditLogMixin` | one audit row per row written (`create`, `update` where the stored document changed, `delete`), in one unit with the row: both are there or neither is. Inside your transaction the unit is a savepoint: both follow your commit or rollback, and a write that fails leaves neither, also when you catch the error and commit |
| `HooksMixin` | `before_save` / `after_save` around each instance saved, `before_delete` / `after_delete` around each delete |

`updated_at` is a time of day, not the order of writes. A `save()` gives its instance a
stamp later than the one that instance held, also when the clock has not moved. A save by
another instance of the same row, and a set-based update, store the clock's reading, so two
writes of a row inside one tick of the clock (about 15 ms on Windows before Python 3.13, far
less elsewhere) can leave equal stamps. What orders the writes of a row is its `_version`:
on a table with that column every write moves it by one.

Order: `before_save()` runs first, then the timestamps are set, then the row is written; a
`before_save()` that returns False leaves the instance as it was. `after_save()` and
`after_delete()` run once the write is committed (or, inside your transaction, once it is
written). `save_many()` calls `before_save()` on every instance, in order, before anything is
stamped or written (one that returns False raises `RuntimeError` naming its index, and
nothing is written), then `after_save()` on each once the batch is written.
`delete_with_policy()` is the delete: `delete()` is the same call with
`on_delete="restrict"`, and the delete with what its policy does to referring rows is one
unit. An unsaved instance or an unknown policy raises `ValueError` before any hook.

A write that fails leaves the instance as it was before the call (`_id`, the stored snapshot
and, on a safe model, `_version`), so it can be made again once the cause is gone.

A delete's audit row holds the row as it was stored: an instance edited and not saved before
its `delete()` logs the stored values, not its edits.

### The set-based ways and the rows a delete's policy changes

A queryset's `update()`, `update_one()`, `update_n()` and `delete_all()`, and the rows that
`delete_with_policy(on_delete="set_null")` nulls or `on_delete="cascade"` deletes (each by
the rules of its own table's registered class), meet the same rules but the hooks:

| Rule | Set-based ways and a policy's rows |
|------|------------------------------------|
| version (a table with the `_version` column) | every update moves `_version` by one, in the same statement, and so does the update that nulls a reference (a holder who loaded the row before cannot save the reference back); `_version` cannot be named in an update (`ValueError`) |
| `TimestampMixin` / `AuditMixin` | each updated (or nulled) row gets `updated_at` (and `updated_by`) in the same statement, stored as `save()` stores it; one time for all rows of the statement (the strictly-later promise of `save()` is per instance). A field you name in the update is yours: `update(updated_at=x)` writes `x` |
| `AuditLogMixin` | one `update` row per row changed (`changes` holds the old and new of each key that differs; the new value is what the row holds afterwards, F-expressions included) and one `delete` row per row deleted, in one unit with the write; each row is read and written inside that unit, so no other connection writes it in between |
| `HooksMixin` | calls no hook; use a loop of `save()` where a hook must run |

A class with no rule sends the one statement it sent before. `delete_all(on_delete=...)`
meets the delete policy as `delete()` does ([C09]): the default `restrict` refuses while a
row outside the set refers to a row in it, and nothing is deleted. A class with no rule that
no class can refer to sends its one `DELETE`; otherwise the `DELETE` returns the rows it
deleted, and the look, the policy and the audit rows join it in one unit. A queryset's
order and limit do not narrow the `DELETE`: it deletes every row the filter matches.

A write made with `db.*` directly (`db.bulk_upsert`, `db.insert_document`, `db.execute`) is
below the model and meets no rule of any class.

### [C26] Timestamps

```python
from sqler import SQLerDB, SQLerModel, TimestampMixin

class Post(TimestampMixin, SQLerModel):
    title: str
    content: str

db = SQLerDB.in_memory()

post = Post(title="Hello", content="World")
post._set_timestamps()  # Call before save for auto-timestamps
post = post.save(db=db)

assert post.created_at is not None
assert post.updated_at is not None
```

### [C27] Soft delete

```python
from sqler import SQLerDB, SQLerModel, SoftDeleteMixin

class Comment(SoftDeleteMixin, SQLerModel):
    text: str

db = SQLerDB.in_memory()

c = Comment(text="Nice post!").save(db=db)
assert c.is_deleted == False

c.soft_delete()
assert c.is_deleted == True
assert c.deleted_at is not None

c.restore()
assert c.is_deleted == False
assert c.deleted_at is None

# Verify the comment was restored and can be queried
all_comments = Comment.using(db).all()
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

# Hooks are called manually by caller
u = AuditedUser(email="  ALICE@Example.COM  ")
if u.before_save():
    u = u.save(db=db)
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

doc1 = Document(title="Active Doc").save(db=db)
doc2 = Document(title="Deleted Doc").save(db=db)
doc3 = Document(title="Another Active").save(db=db)
doc2.soft_delete()

# active() - only non-deleted
active = Document.active(db=db).all()
assert len(active) == 2
assert all(d.deleted_at is None for d in active)

# only_deleted() - only deleted
deleted = Document.only_deleted(db=db).all()
assert len(deleted) == 1
assert deleted[0].title == "Deleted Doc"

# with_deleted() - all records
all_docs = Document.with_deleted(db=db).all()
assert len(all_docs) == 3
```

`active(*, db=None)`, `with_deleted(*, db=None)` and `only_deleted(*, db=None)` read `db`
through `using(db)` when it is given, else the class's bound database.

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

# Hooks are called automatically in save()
item = AuditedItem(name="  HELLO WORLD  ")
item = item.save(db=db)

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

try:
    AbortableItem(name="test", valid=False).save(db=db2)
except RuntimeError as e:
    assert "before_save() returned False" in str(e)
```

### Async mixins and the combined mixins

For an async model (`AsyncSQLerModel`, `AsyncSQLerSafeModel` and their lite twins) the mixins
with I/O have async twins; the rules in the two tables above hold for them as for the sync ones.

| Sync | Async | What differs |
|------|-------|--------------|
| `SoftDeleteMixin` | `AsyncSoftDeleteMixin` | `await row.soft_delete()` and `await row.restore()`; `active()`, `with_deleted()` and `only_deleted()` return async querysets |
| `HooksMixin` | `AsyncHooksMixin` | the hooks are `async def` |
| `AuditMixin` | `AsyncAuditMixin` | its current user is its own: set it with `AsyncAuditMixin.set_current_user(...)`; `AuditMixin.set_current_user(...)` does not reach it |
| `AuditLogMixin` | `AsyncAuditLogMixin` | `await row.get_audit_log()`; the user of each audit row is the one set on `AsyncAuditMixin` |

`TimestampMixin` has no async twin: an async model uses it as it is.

`FullMixin` is `TimestampMixin`, `SoftDeleteMixin` and `HooksMixin` in one base, and
`AsyncFullMixin` is `TimestampMixin`, `AsyncSoftDeleteMixin` and `AsyncHooksMixin`. Neither adds
an audit mixin: add `AuditMixin` or `AuditLogMixin` (or their async twins) beside it.

```python
import asyncio
from sqler import AsyncSQLerDB, AsyncSQLerModel, AsyncFullMixin, AsyncAuditMixin, AsyncAuditLogMixin

class Post(AsyncFullMixin, AsyncSQLerModel):
    title: str

    async def before_save(self) -> bool:
        self.title = self.title.strip()
        return True

class Entry(AsyncAuditMixin, AsyncAuditLogMixin, AsyncSQLerModel):
    text: str

async def main():
    db = AsyncSQLerDB.in_memory()
    await db.connect()

    post = await Post(title="  hello ").save(db=db)
    print(repr(post.title), post.created_at is not None)  # 'hello' True
    await post.soft_delete()
    print(post.is_deleted, len(await Post.active(db=db).all()), len(await Post.with_deleted(db=db).all()))
    # True 0 1

    AsyncAuditMixin.set_current_user("ada")
    entry = await Entry(text="x").save(db=db)
    print(entry.created_by, [(r["action"], r["user"]) for r in await entry.get_audit_log()])
    # ada [('create', 'ada')]
    await db.close()

asyncio.run(main())
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
hydrated = [BU.from_id(i, db=db) for i in ids]
assert all(isinstance(h, BU) for h in hydrated)
```

### [C19] Raw SQL (`execute_sql`)

```python
from sqler import SQLerDB, SQLerModel

class ReportUser(SQLerModel):
    name: str
    email: str | None = None

db = SQLerDB.in_memory()
ReportUser(name="Ada", email="ada@example.com").save(db=db)
ReportUser(name="Bob", email="bob@example.com").save(db=db)

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

# Enable logging
query_logger.enable()

LoggedUser(name="Ada", age=36).save(db=db)
LoggedUser(name="Bob", age=25).save(db=db)

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

`query_logger` is a `QueryLogger`, the one instance the library logs to; make your own with
`QueryLogger(max_logs=1000)` to keep a separate log (only `query_logger` is wired to the
adapters). Each entry in `.logs` is a `QueryLog`: `sql`, `params`, `duration_ms`, `timestamp`,
`rows_affected` and `error`; `str(log)` prints it as one line. `get_failed_queries()` returns
the entries that have an `error`, and `capture()` is a context manager that collects the
queries logged inside its block.

`timed_query()` is a context manager that times a block: `timer.duration_ms` is the time since
the block began, or its length once it ended.

```python
import time
from sqler import QueryLog, QueryLogger, timed_query

logger = QueryLogger(max_logs=10)
logger.enable(log_to_logger=False)
logger.log("SELECT 1", [], 0.5, rows_affected=1)
logger.log("SELECT oops", [], 2.0, error="boom")

first = logger.logs[0]
print(isinstance(first, QueryLog), first.sql, first.duration_ms, first.rows_affected, first.error)
# True SELECT 1 0.5 1 None
print(str(logger.logs[1]))  # [2.00ms] SELECT oops | params=[] | ERROR: boom
print(len(logger.get_failed_queries()), logger.get_stats()["count"])  # 1 2

with timed_query() as timer:
    time.sleep(0.01)
print(timer.duration_ms >= 10)  # True
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
@cached_query(ttl_seconds=60, table=User)
def get_active_users():
    return User.using(db).filter(F("active") == True).all()
```

`get_cache()` returns the process-wide `QueryCache` that a decorator without `cache=` uses,
creating it with the defaults (1000 entries, 300 s) on first use; `configure_cache(max_size=1000,
default_ttl_seconds=300, cleanup_interval_seconds=60)` replaces it with a new one and returns it.
`async_cached_query` is `cached_query` for `async def` functions (same arguments; the result of
the awaited call is cached).

`cache.stats` is a `CacheStats`: `hits`, `misses`, `evictions`, `expirations`, `invalidations`,
`size`, `max_size`, the `hit_rate` property and `to_dict()`. `misses` counts the lookups
that found nothing: a `get()`, and a call of a decorated function that had to run.
`cache.get_entry(key)` returns the
`CacheEntry` of a key: `value`, `created_at`, `expires_at`, `hits`, `table`, and the properties
`is_expired`, `age_seconds` and `ttl_remaining` (`None` for an entry that never expires).

```python
import asyncio
from sqler import QueryCache, async_cached_query, cached_query, configure_cache, get_cache

cache = configure_cache(max_size=50, default_ttl_seconds=60)
print(get_cache() is cache)  # True

calls = []

@cached_query(ttl_seconds=30)
def slow():
    calls.append(1)
    return [1, 2]

slow()
slow()
print(len(calls), get_cache().stats.hits)  # 1 1

c = QueryCache()
c.set("k", "v", ttl_seconds=10, table="t")
entry = c.get_entry("k")
print(entry.value, entry.table, entry.is_expired, entry.ttl_remaining > 0)  # v t False True

runs = []

@async_cached_query(ttl_seconds=30, cache=c)
async def double(x):
    runs.append(x)
    return x * 2

async def main():
    print(await double(2), await double(2), len(runs))  # 4 4 1

asyncio.run(main())
```

`table=` names what a cached function reads: a table name, or a model class, which stands
for its table (the name its rows are written to; a name that differs only by case is the
same table, as in SQLite). With `CacheAwareModel` on the class, a cached result of its table
is never kept from rows older than the last write of the table:

1. Every way of writing the class's rows drops the entries of its table in every
   `QueryCache` (and those tagged with `Meta.cache_table`, where the class sets one):
   `save()`, `save_partial()`, `save_many()`, `delete()`, `delete_with_policy()` and the rows
   a delete's policy nulls or deletes (their own class's table), and a queryset's `update()`,
   `update_one()`, `update_n()` and `delete_all()`, sync and async. An async write tells the
   cache when it has run, not when its coroutine is made. It sends no statement.
2. While a transaction of this process that wrote the table is open (`db.transaction()`,
   `with db:`), no entry of the table
   is served or stored (`get`, `has` and `set` too): the writer reads its own uncommitted
   rows through, and other threads and tasks read the committed rows through. Caching of
   the table resumes when the transaction ends, committed or rolled back.
3. A result is stored only if the table was not written while its function ran.

An entry with no table is never dropped by a write, only by its lifetime. A write made with
`db.*` directly (`insert_many`, `bulk_upsert`, the document methods, raw SQL) is below the
model and drops nothing: call `invalidate_table()` after one. A transaction opened on the
adapter directly (`adapter.begin_transaction()`) is not seen either: a write inside it drops
the entries at the write, not again at its end. The cache lives in this
process: a write by another process is not seen, and an entry's lifetime is the bound on how
long it can serve rows that write replaced.

### [C39] Data export (CSV, JSON)

```python
from sqler import export_csv, export_csv_string, import_csv

# Export to file: the source is a queryset of the database
result = export_csv(User.using(db), "users.csv")
print(f"Exported {result.count} records ({result.size_bytes} bytes)")

# Export specific fields
export_csv(User.using(db), "users.csv", fields=["name", "email"], include_id=False)

# Export to string
csv_string = export_csv_string(User.using(db))

# Import from CSV
result = import_csv(User, "users.csv", db=db)
print(f"Imported {result.succeeded}/{result.count} records")

# Import with transform
def normalize(row):
    row["email"] = row["email"].lower()
    return row

import_csv(User, "users.csv", transform=normalize, db=db)
```

```python
from sqler import export_json, export_json_string, import_json

# Export to file
export_json(User.using(db), "users.json", indent=2)

# Export to string
json_string = export_json_string(User.using(db))

# Import from JSON array file
result = import_json(User, "users.json", db=db)
```

An export takes a queryset (`User.using(db)`, narrowed with `filter()` when only some rows
go out) and reads the database it was made on. Every export function returns an `ExportResult` (`path`, `format`, `count`, `size_bytes`,
`to_dict()`); every import function returns an `ImportResult`: `count` rows read, `succeeded`
and `failed` (also `success_count` and `error_count`), `success_rate`, and `errors`, a list with
one dict per failed row (`row`, `data`, `error`, `error_type`). By default the first
row that fails ends the import; with `skip_errors=True` the import goes on and `errors` holds
every failed row.

```python
from sqler import export_csv, import_csv

result = export_csv(User.using(db), "users.csv")
print(result.format, result.count, result.size_bytes > 0)  # csv 2 True

result = import_csv(User, "users.csv", db=db)
print(result.count, result.succeeded, result.failed, round(result.success_rate, 2))
```

### [C40] JSONL export/import

```python
from sqler import export_jsonl, import_jsonl, stream_jsonl

# Export one record per line (streaming-friendly)
export_jsonl(User.using(db), "users.jsonl")

# Import JSONL
import_jsonl(User, "users.jsonl", db=db)

# Stream records without loading all into memory
for record_json in stream_jsonl(User.using(db)):
    process(record_json)
```

`stream_jsonl()`, `iter_dicts()` and `iter()` read their rows as they give them. Writing
inside the loop (a `save()` of each model, say) is safe: the first statement the thread
sends on its connection, a read as much as a write, reads the rows not given yet first, so each row comes once with the
values its statement saw, and the rest of the result is held in memory from then on. A
statement another thread sends on its own connection reads nothing of this one. A rollback
reads nothing either: a cursor or iterator left part-read when its transaction (or nested
transaction) is rolled back raises `sqlite3.OperationalError` at its next read. On a plain
`":memory:"` database (`SQLerDB.in_memory(shared=False)`), whose one connection every thread
shares, a statement's rows are read whole before another thread may use the connection, so
the three hold the whole result there.

The async query and query set stream with `async for`:

```python
async for doc in (await db.query("users")).iter_dicts():
    process(doc)

async for user in User.using(db).filter(F("age") > 30).iter():
    process(user)
```

They give what `all_dicts()` and `all()` give, a chunk of rows read at a time; `iter()`
follows no reference (as `.resolve(False)`). Outside a transaction a suspended iteration
holds one pooled connection, from its first step until it ends: its rows run out, it is
closed (`await it.aclose()`, or `contextlib.aclosing`), or the event loop closes it once it
was dropped or left by an exception or a cancellation. Then the connection goes back to the
pool, whichever task ends it. It stays with its task when that task sent another statement
on it after the iteration's and has not committed it: the task's `auto_commit()` (or its
end) gives it back. Stopping early reads none of the rows left. Inside
`async with db.transaction()` the iteration sees the transaction's writes and the
transaction's end gives the connection back; an iterator left unfinished when it ends goes
on giving what its statement saw. Writing inside the loop (a `save()` of each model, say) is
safe: the first statement sent on the iteration's connection reads the rows not given yet
first, so each row comes once with the values its statement saw, and the rest of the result
is held in memory from then on.

```python
from sqler import async_export_jsonl, async_import_jsonl

# Async versions for high-throughput scenarios
await async_export_jsonl(User.using(db), "users.jsonl")
await async_import_jsonl(User, "users.jsonl", db=db)
```

Every import that takes a model class (`import_csv`, `import_json`, `import_jsonl`, their
`_string` forms and `async_import_jsonl`) takes `db=None` as its last keyword: the rows are
written to `db` and, with `update_existing`, read from it; without it, to the class's
database. `import_jsonl(User, "users.jsonl", db=db)` needs no class binding.

### [C41] Full-text search (FTS5)

```python
from sqler import FTSIndex, SearchResult

class Article(SQLerModel):
    title: str
    content: str
    author: str

# Create FTS index on specific fields
fts = FTSIndex(Article, fields=["title", "content"], db=db)
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
new_article = Article(title="New", content="content", author="x").save(db=db)
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
`delete()`. The mixin keeps the entry in the database the row is written to or deleted from
(inside that database's transaction), and only for a row of the index's table: a database
that has no table of this index is left alone.

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
class's bound database), every method raises `TypeError` before it sends anything.

```python
from sqler import SearchableMixin

class Post(SearchableMixin, SQLerModel):
    title: str
    body: str

    class FTS:
        fields = ["title", "body"]

# Create index once
Post.create_search_index(db)

# Add posts: the index follows
Post(title="Python Tips", body="Learn Python").save(db=db)

# Search
results = Post.search("Python", db=db)
count = Post.search_count("Python", db=db)
results = Post.search("C++", plain=True, db=db)
```

Every search call takes the database: `Post.create_search_index(db)`, then
`Post.search("Python", db=db)`, `search_ranked(..., db=db)`, `search_count(..., db=db)`,
`rebuild_search_index(db=db)` and `drop_search_index(db=db)` (see "Every class call names
its database").

`fts.stats()` returns an `FTSStats`: `table_name` (the index table), `indexed_rows`, `fields`
and `total_tokens`, an estimate made from the length of the first indexed field.

```python
from sqler import FTSStats

stats = fts.stats()
print(isinstance(stats, FTSStats), stats.table_name, stats.indexed_rows, stats.fields)
# True articles_fts 1 ['title', 'content']
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

`migrate()` and `rollback()` return a `MigrationResult`: `success`, `applied` and `rolled_back`
(lists of `MigrationRecord`), `current_version`, `target_version`, and `error` and
`error_at_version` (both `None` unless a migration failed); `to_dict()` serialises it. A
`MigrationRecord` is one migration that ran: `version`, `name`, `applied_at`, `duration_ms` and
`checksum`.

```python
res = runner.migrate()
print(res.success, res.current_version, res.target_version, res.error)  # True 1 1 None
rec = res.applied[0]
print(rec.version, rec.name, rec.duration_ms >= 0)  # 1 t True
print(runner.rollback(target_version=0).rolled_back[0].version)  # 1
```

### [C44] Metrics collection

```python
from sqler import metrics

# Enable metrics collection
metrics.enable()

# ... run queries (metrics collected automatically) ...
User(name="Alice").save(db=db)
User.using(db).all()

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

`metrics` is a `MetricsCollector`, the instance the examples above use. Make another with
`MetricsCollector()` to keep separate counts: each collector reads the same query logger, and
`enable()` turns that logger on if it was off (`disable()` puts it back as it was).
`enable(slow_threshold_ms=100.0, labels=None)` takes the slow-query threshold and extra
Prometheus labels.

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

`async_is_healthy(db, timeout_ms=5000)` is the boolean form of `async_health_check()`, as
`is_healthy()` is of `health_check()`. `async_checkpoint(db, mode="PASSIVE")` is the async
`checkpoint()`: it returns `{"busy": ..., "log": ..., "checkpointed": ...}`, and a `mode` other
than `PASSIVE`, `FULL`, `RESTART` or `TRUNCATE` (in either case) raises `ValueError`.

```python
from sqler import async_checkpoint, async_is_healthy

print(await async_is_healthy(db))  # True
print(await async_checkpoint(db, mode="TRUNCATE"))  # {'busy': 0, 'log': 0, 'checkpointed': 0}
```

### [C46] Change tracking

```python
from sqler import TrackedModel

class User(TrackedModel, SQLerModel):
    name: str
    email: str
    age: int

user = User(name="Alice", email="alice@test.com", age=30)
user.save(db=db)
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

Under the model is a `ChangeTracker`, kept on the instance as `_tracker`: it holds the values
from the last snapshot and a list of `FieldChange` records, one per assignment that changed a
value (`field`, `old_value`, `new_value`, `changed_at`, and `to_dict()`).
`user.get_change_history()` returns that list in order; `ChangeTracker.has_changes` is true once
it holds one.

```python
from sqler import ChangeTracker, FieldChange

user.mark_clean()
user.name = "Bob"
user.name = "Cy"
history = user.get_change_history()
print([(c.field, c.old_value, c.new_value) for c in history])
# [('name', 'Alice', 'Bob'), ('name', 'Bob', 'Cy')]
print(isinstance(history[0], FieldChange), type(user._tracker) is ChangeTracker)  # True True
```

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
- **Concurrency errors** — all are a `ConcurrencyError`, so `except ConcurrencyError` catches them
  - `StaleVersionError` — optimistic check failed (HTTP 409)
  - `DeadlockError` — deadlock detected
  - `LockTimeoutError` — unable to acquire lock
  - `TransactionStateError` — a transaction can no longer be trusted to commit what it was told to (see "Nested transactions whose end fails")
- **Integrity errors**
  - `ReferentialIntegrityError` — foreign key constraint violated
  - `UniqueConstraintError` — unique constraint violated (also a `sqlite3.IntegrityError`; see [D02](#d02-a-unique-index-that-refuses-a-row-raises-uniqueconstrainterror))
- **Model errors** — all are a `ModelError`, so `except ModelError` catches them
  - `NotBoundError` — model not bound to database
  - `NotFoundError` — model instance not found
  - `ValidationError` — model validation failed
  - `UnstorableValueError` — a value has no stored form (a kind sqler does not store; a `Decimal` or `timedelta` no number holds exactly, a `timedelta` being stored as its number of seconds; or `bytes` in a JSON document: promote the field to a `BLOB` column, or store text); `value` is the value and `field` its place in the document, where known; also a `TypeError`
- **Batch errors**
  - `BatchOperationError` — carries `succeeded` (a list of ids) and `failed` (a list of
    `(index, message)` pairs) for a batch that partly failed. Nothing in sqler raises it today;
    you can raise it yourself.
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

**"Model X is not bound"** (`NotBoundError`)

A class call needs the database it works on. Pass it:
```python
db = SQLerDB.in_memory()
User(name="Ada", age=36).save(db=db)   # a write
users = User.using(db).all()           # a query
user = User.from_id(1, db=db)          # every other class call
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
q = User.using(db).filter(F("age") > 30)
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
