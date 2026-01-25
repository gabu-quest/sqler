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

## 08 — Transactions

Use transactions for atomic operations:

```python
from sqler import SQLerDB, SQLerModel

class Account(SQLerModel):
    name: str
    balance: int

db = SQLerDB.in_memory()
Account.set_db(db)

# Atomic batch operation
with db.transaction():
    Account(name="Alice", balance=1000).save()
    Account(name="Bob", balance=500).save()

# Rollback on error
try:
    with db.transaction():
        Account(name="Charlie", balance=200).save()
        raise ValueError("Abort!")
except ValueError:
    pass  # Charlie not saved
```

## 09 — Aggregations

Perform calculations in the database:

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class Sale(SQLerModel):
    product: str
    amount: float
    quantity: int

db = SQLerDB.in_memory()
Sale.set_db(db)

Sale(product="A", amount=100.0, quantity=5).save()
Sale(product="B", amount=50.0, quantity=10).save()
Sale(product="C", amount=75.0, quantity=3).save()

q = Sale.query()
print(f"Total quantity: {q.sum('quantity')}")      # 18
print(f"Average amount: {q.avg('amount')}")        # 75.0
print(f"Min amount: {q.min('amount')}")            # 50.0
print(f"Max amount: {q.max('amount')}")            # 100.0
print(f"Has sales > 80: {q.filter(F('amount') > 80).exists()}")  # True
```

## 10 — Pagination

Built-in pagination for large result sets:

```python
from sqler import SQLerDB, SQLerModel

class Post(SQLerModel):
    title: str
    views: int

db = SQLerDB.in_memory()
Post.set_db(db)

# Create 100 posts
for i in range(100):
    Post(title=f"Post {i}", views=i * 10).save()

# Get page 3 with 10 items per page
page = Post.query().order_by("views", desc=True).paginate(page=3, per_page=10)

print(f"Page {page.page} of {page.total_pages}")
print(f"Items: {len(page.items)}")
print(f"Has next: {page.has_next}, Has prev: {page.has_prev}")
print(f"Next page: {page.next_page}, Prev page: {page.prev_page}")
```

## 11 — Model Mixins

### Timestamps

```python
from sqler import SQLerDB, SQLerModel, TimestampMixin

class Article(TimestampMixin, SQLerModel):
    title: str

db = SQLerDB.in_memory()
Article.set_db(db)

article = Article(title="Hello World")
article._set_timestamps()
article = article.save()

print(f"Created: {article.created_at}")
print(f"Updated: {article.updated_at}")
```

### Soft Delete

```python
from sqler import SQLerDB, SQLerModel, SoftDeleteMixin
from sqler.query import SQLerField as F

class Document(SoftDeleteMixin, SQLerModel):
    name: str

db = SQLerDB.in_memory()
Document.set_db(db)

doc = Document(name="Report").save()
doc.soft_delete()  # Sets deleted_at, doesn't remove

# Query excluding deleted
active = Document.query().filter(F("deleted_at") == None).all()

doc.restore()  # Clears deleted_at
```

### Lifecycle Hooks

```python
from sqler import SQLerDB, SQLerModel, HooksMixin

class User(HooksMixin, SQLerModel):
    email: str

    def before_save(self) -> bool:
        self.email = self.email.lower().strip()
        return True  # Continue with save

    def after_save(self) -> None:
        print(f"Saved user: {self.email}")

db = SQLerDB.in_memory()
User.set_db(db)

u = User(email="  ADMIN@EXAMPLE.COM  ")
if u.before_save():
    u = u.save()
    u.after_save()
# Output: Saved user: admin@example.com
```

## 12 — Query Logging

Debug and profile queries:

```python
from sqler import SQLerDB, SQLerModel, query_logger

class LoggedModel(SQLerModel):
    name: str

db = SQLerDB.in_memory()
LoggedModel.set_db(db)

# Enable logging
query_logger.enable()

# ... perform queries ...
LoggedModel(name="Test").save()

# Manually log for demonstration
query_logger.log("SELECT * FROM loggedmodels", [], 1.5)

# Get statistics
stats = query_logger.get_stats()
print(f"Query count: {stats['count']}")
print(f"Avg time: {stats['avg_time_ms']:.2f}ms")

# Find slow queries
slow = query_logger.get_slow_queries(threshold_ms=1.0)
for q in slow:
    print(f"Slow query: {q.sql} ({q.duration_ms:.2f}ms)")

query_logger.disable()
query_logger.clear()
```
