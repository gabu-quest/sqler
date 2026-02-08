# SQLer

**English | [日本語はこちら](README.ja.md)**

[![PyPI version](https://img.shields.io/pypi/v/sqler)](https://pypi.org/project/sqler/)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)
[![Tests](https://github.com/gabu-quest/SQLer/actions/workflows/ci.yml/badge.svg)](https://github.com/gabu-quest/SQLer/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**A lightweight, JSON-first micro-ORM for SQLite.** Define Pydantic models, persist them as JSON documents, query with a fluent API. Sync and async.

## Install

```bash
pip install sqler
```

Python 3.12+ · SQLite with JSON1 (bundled on most platforms).

## Quick Start

```python
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

class User(SQLerModel):
    name: str
    age: int
    tags: list[str] = []

db = SQLerDB.in_memory()
User.set_db(db)

User(name="Alice", age=30, tags=["admin"]).save()
User(name="Bob", age=25, tags=["user"]).save()

admins = User.query().filter(F("tags").contains("admin")).all()
assert admins[0].name == "Alice"

young = User.query().filter(F("age") < 28).order_by("name").all()
assert young[0].name == "Bob"
```

---

## Interactive Tour

Learn sqler hands-on with [marimo](https://marimo.io) notebooks — run and modify code directly in your browser.

**[Launch Interactive Tour →](https://gabu-quest.github.io/sqler/)**

| Tour | Topics |
|------|--------|
| [01. Fundamentals](https://gabu-quest.github.io/sqler/tour_01_fundamentals/) | Models, CRUD, queries, aggregations |
| [02. Relationships](https://gabu-quest.github.io/sqler/tour_02_relationships/) | References, hydration, cross-model queries |
| [03. Safe Models](https://gabu-quest.github.io/sqler/tour_03_safe_models/) | Optimistic locking, conflict resolution |
| [04. Transactions](https://gabu-quest.github.io/sqler/tour_04_transactions/) | Atomic operations, rollback |
| [05. Mixins](https://gabu-quest.github.io/sqler/tour_05_mixins/) | Timestamps, soft delete, lifecycle hooks |
| [06. Advanced](https://gabu-quest.github.io/sqler/tour_06_advanced/) | Bulk ops, indexes, integrity, raw SQL |
| [07. Export/Import](https://gabu-quest.github.io/sqler/tour_07_export_import/) | CSV, JSON, JSONL |
| [08. Full-Text Search](https://gabu-quest.github.io/sqler/tour_08_fulltext_search/) | FTS5, boolean queries, ranking |
| [09. Change Tracking](https://gabu-quest.github.io/sqler/tour_09_change_tracking/) | Dirty checking, partial updates, diff |
| [10. Database Ops](https://gabu-quest.github.io/sqler/tour_10_db_operations/) | Health checks, stats, vacuum, logging |
| [11. Metrics & Caching](https://gabu-quest.github.io/sqler/tour_11_metrics_caching/) | Prometheus metrics, query caching, pools |

```bash
# Or run locally
git clone https://github.com/gabu-quest/sqler.git && cd sqler
uv sync --dev
uv run marimo edit examples/tour_01_fundamentals.py
```

---

## Why sqler?

SQLite is the most deployed database on earth, and JSON1 turns it into a document store. sqler bridges that gap: you get Pydantic validation with document-style flexibility, a fluent query builder that reaches into nested JSON, and real data integrity (optimistic locking, referential policies, transactions) — all in a single file, zero-config database.

---

## Performance

Real numbers from the [benchmark suite](benchmarks/results/REPORT.md) (22 scenarios, Python 3.12, SQLite 3.50):

| Operation | Result | Context |
|-----------|--------|---------|
| Bulk insert | **84K rows/sec** | `bulk_upsert()` at 10K rows |
| Index speedup | **470x** | 9.4ms → 0.02ms with `create_index()` |
| Cache hit | **7,000x** | 14ms → 0.002ms with `@cached_query` |
| FTS search | **0.28ms** | Sub-millisecond across all dataset sizes |
| Bulk vs single | **5.2x** | `bulk_upsert` vs `save()` loop at 10K |
| Lite models | **1.3x** | Dataclass variant vs Pydantic overhead |

### Honest Limitations

- **Single writer** — SQLite's architecture. Use `bulk_upsert` and transactions to batch writes.
- **JSON overhead** — Fields are extracted via `json_extract()`, not native columns. Indexes close the gap.
- **No JOINs** — Relationships use reference hydration, not SQL JOINs. Fine for typical document patterns.
- **Memory-bound** — Large result sets materialize in Python. Use `paginate()` and `count()` to limit.

Full report with 22 charts: [benchmarks/results/REPORT.md](benchmarks/results/REPORT.md)

---

## Features

**Core**
- JSON document models with Pydantic validation (+ lightweight dataclass variant)
- Fluent query builder: `filter`, `exclude`, `order_by`, `paginate`, `contains`, `isin`, `between`
- `F()` operator for nested fields: `F("x")["y"]`, `F(["items"]).any().where(...)`
- Sync and async APIs with matching semantics

**Safety & Integrity**
- `SQLerSafeModel` with optimistic locking (`_version` + `StaleVersionError`)
- Referential integrity: `cascade`, `restrict`, `set_null` delete policies
- Transaction-aware saves — `model.save()` inside `with db.transaction():` rolls back properly
- `bulk_upsert` for efficient batch writes

**Developer Experience**
- FTS5 full-text search with ranking and highlights
- Query caching with TTL and LRU eviction
- Connection pooling, schema migrations, metrics collection
- Export/import: CSV, JSON, JSONL (sync + async)
- 11 interactive marimo tour notebooks
- Query logging, `debug()`, `explain_query_plan()`

---

## When to Use sqler

**Good fit:**
- Fast prototyping with real persistence
- SQLite as embedded app state (Electron, CLI tools, mobile)
- JSON flexibility + data integrity in one package
- Pydantic validation on your data layer

**Consider alternatives:**
- Need multi-database support → [SQLAlchemy](https://www.sqlalchemy.org/)
- Purely relational SQLite workflows → [sqlite-utils](https://sqlite-utils.datasette.io/)
- Complex JOINs at scale → a full ORM with proper relational modeling
- Write-heavy under contention → PostgreSQL

---

## Showcase

### Safe Models — Optimistic Locking

```python
from sqler import SQLerDB, SQLerSafeModel, StaleVersionError

class Account(SQLerSafeModel):
    owner: str
    balance: int

db = SQLerDB.in_memory()
Account.set_db(db)

acc = Account(owner="Ada", balance=100).save()  # _version == 0
acc.balance = 120
acc.save()                                       # _version == 1

# Another process changes the row...
# Your next save detects the conflict:
try:
    acc.balance = 130
    acc.save()
except StaleVersionError:
    acc.refresh()  # reload from DB, then decide
```

[Full tour →](https://gabu-quest.github.io/sqler/tour_03_safe_models/) · [API reference →](docs/API.md#c06-optimistic-versioning)

### Relationships — Hydration & Cross-Filtering

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
assert loaded.address.city == "Kyoto"  # auto-hydrated

kyoto_users = User.query().filter(
    User.ref("address").field("city") == "Kyoto"
).all()
assert kyoto_users[0].name == "Alice"
```

[Full tour →](https://gabu-quest.github.io/sqler/tour_02_relationships/) · [API reference →](docs/API.md#c04-hydration--cross-filtering)

### Full-Text Search

```python
from sqler import SQLerDB, SQLerModel, FTSIndex

class Article(SQLerModel):
    title: str
    content: str

db = SQLerDB.in_memory()
Article.set_db(db)

Article(title="Python Tips", content="Learn Python fast").save()
Article(title="SQLite Guide", content="Master SQLite queries").save()

fts = FTSIndex(Article, fields=["title", "content"])
fts.create(db)
fts.rebuild()

results = fts.search("Python")        # instant — 0.28ms at any scale
ranked = fts.search_ranked("Python")   # with relevance scores
```

[Full tour →](https://gabu-quest.github.io/sqler/tour_08_fulltext_search/) · [API reference →](docs/API.md#c41-full-text-search-fts5)

---

## Documentation

| Resource | Description |
|----------|-------------|
| [API Reference](docs/API.md) | All 46 tested contracts (C01–C46) |
| [Examples Cookbook](docs/EXAMPLES.md) | End-to-end scripts for every feature |
| [Interactive Tours](https://gabu-quest.github.io/sqler/) | 11 marimo notebooks — run in browser |
| [Benchmark Report](benchmarks/results/REPORT.md) | 22 scenarios with charts |
| [Changelog](CHANGELOG.md) | Version history |

---

## Testing

```bash
uv run pytest -q                                       # unit tests
uv run pytest -q -m perf                               # performance benchmarks
uv run python -m benchmarks run --scale small           # full benchmark suite
uv run python -m benchmarks plot                        # generate charts
```

---

## Contributing

```bash
uv run ruff format .                                   # format
uv run ruff check .                                    # lint
uv run pytest -q --cov=src --cov-report=term-missing   # test with coverage
```

---

## License

MIT
