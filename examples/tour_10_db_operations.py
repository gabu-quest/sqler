# /// script
# requires-python = ">=3.12"
# dependencies = ["sqler", "marimo"]
# ///

import marimo

__generated_with = "0.18.4"
app = marimo.App(width="medium")


@app.cell
def _():
    # --- marimo scaffolding (please ignore) ---
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(r"""
    # SQLer Tour: Database Operations

    This notebook covers SQLer's database operations for production use
    including health checks, statistics, and maintenance.

    You'll learn:

    1. Health checks for monitoring
    2. Database statistics
    3. Vacuum (reclaim space)
    4. Checkpoint (WAL mode)
    5. Registry and table management

    Let's explore!
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 1. Setting Up
    """)
    return


@app.cell
def _():
    from sqler import SQLerDB, SQLerModel
    from sqler.ops import (
        get_stats,
        health_check,
        is_healthy,
        vacuum,
    )

    db = SQLerDB.in_memory()
    print("Database connected!")
    print("\nOperations available:")
    print("  - health_check(): Detailed health status")
    print("  - is_healthy(): Quick boolean check")
    print("  - get_stats(): Database statistics")
    print("  - vacuum(): Reclaim disk space")
    print("  - checkpoint(): WAL checkpoint")
    return SQLerModel, db, get_stats, health_check, is_healthy, vacuum


@app.cell
def _(SQLerModel, db):
    class User(SQLerModel):
        _table = "users"
        name: str
        email: str

    User.set_db(db)

    # Create some data
    for _i in range(10):
        User(name=f"User{_i}", email=f"user{_i}@example.com").save()

    print(f"Created {User.query().count()} users")
    return (User,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. Health Checks

    Use health checks for monitoring and liveness probes:
    """)
    return


@app.cell
def _(db, health_check):
    # Detailed health check
    _status = health_check(db)

    print("Health Check Result:")
    print(f"  Healthy: {_status.healthy}")
    print(f"  Latency: {_status.latency_ms:.2f}ms")
    print(f"  Message: {_status.message}")
    print(f"  Timestamp: {_status.timestamp}")
    return


@app.cell
def _(db, is_healthy):
    # Quick boolean check for liveness probes
    _healthy = is_healthy(db)
    print(f"is_healthy(db) = {_healthy}")

    # Use in health endpoints:
    # if not is_healthy(db):
    #     return {"status": "unhealthy"}, 503
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 3. Database Statistics

    Get detailed statistics about your database:
    """)
    return


@app.cell
def _(db, get_stats):
    _stats = get_stats(db)

    print("Database Statistics:")
    print(f"  Page count: {_stats.page_count}")
    print(f"  Page size: {_stats.page_size} bytes")
    print(f"  Size: {_stats.size_bytes} bytes")
    print(f"  WAL size: {_stats.wal_size_bytes} bytes")
    print(f"  Freelist: {_stats.freelist_count} pages")
    print(f"  Tables: {_stats.table_count}")
    print(f"  Indexes: {_stats.index_count}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 4. Registry

    SQLer maintains a registry of all models bound to tables:
    """)
    return


@app.cell
def _():
    from sqler import resolve, tables

    # List all registered tables (returns dict[str, type])
    _registry = tables()
    print(f"Registered tables: {list(_registry.keys())}")

    # Resolve a model by table name
    for _table, _cls in _registry.items():
        print(f"  {_table} -> {_cls.__name__}")

    # You can also resolve by name
    _user_cls = resolve("users")
    print(f"\nresolve('users') = {_user_cls}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 5. Vacuum (Reclaim Space)

    After deleting records, use vacuum to reclaim disk space:
    """)
    return


@app.cell
def _(User, db, get_stats, vacuum):
    # Check initial size
    _before = get_stats(db)
    print(f"Before: {_before.size_bytes} bytes, {_before.freelist_count} free pages")

    # Delete some records
    for _i in range(5):
        _user = User.from_id(_i + 1)
        if _user:
            _user.delete()

    print(f"Deleted 5 users, now have {User.query().count()} users")

    # Vacuum to reclaim space
    _duration = vacuum(db)
    print(f"Vacuumed database in {_duration:.2f}ms")

    _after = get_stats(db)
    print(f"After: {_after.size_bytes} bytes, {_after.freelist_count} free pages")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. Pagination

    For large datasets, use pagination:
    """)
    return


@app.cell
def _(SQLerModel, db):
    class Product(SQLerModel):
        _table = "products"
        name: str
        price: float

    Product.set_db(db)

    # Create many products
    for _i in range(25):
        Product(name=f"Product {_i}", price=_i * 10.0).save()

    print(f"Created {Product.query().count()} products")
    return (Product,)


@app.cell
def _(Product):
    # Manual pagination with limit/offset
    _page_size = 5

    print("Manual pagination (limit/offset):")
    for _page in range(3):
        _offset = _page * _page_size
        _results = Product.query().limit(_page_size).offset(_offset).all()
        print(f"  Page {_page + 1}: {[p.name for p in _results]}")
    return


@app.cell
def _(Product):
    # Using paginate() for convenience (returns dicts)
    _page = Product.query().paginate(page=1, per_page=5)

    print("\nUsing paginate():")
    print(f"  Total items: {_page.total}")
    print(f"  Total pages: {_page.total_pages}")
    print(f"  Current page: {_page.page}")
    print(f"  Has next: {_page.has_next}")
    print(f"  Items: {[p['name'] for p in _page.items]}")

    # Get next page
    if _page.has_next:
        _page2 = Product.query().paginate(page=2, per_page=5)
        print(f"\nPage 2: {[p['name'] for p in _page2.items]}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. Logging Queries

    Enable query logging for debugging:
    """)
    return


@app.cell
def _(Product):
    from sqler.logging import query_logger
    from sqler.query import SQLerField as F

    # Enable the global query logger (used by adapters)
    query_logger.enable()
    query_logger.clear()  # Clear any previous logs

    # Run some queries - these get logged automatically
    Product.query().filter(F("price") > 100).all()
    Product.query().count()

    # Get logged queries
    print("Recent queries:")
    for _log in query_logger.logs[-5:]:
        _sql_preview = _log.sql[:60] if len(_log.sql) > 60 else _log.sql
        print(f"  {_sql_preview}... ({_log.duration_ms:.2f}ms)")

    # Get statistics
    _stats = query_logger.get_stats()
    print(f"\nStats: {_stats['count']} queries, avg {_stats['avg_time_ms']:.2f}ms")

    query_logger.disable()
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    SQLer database operations:

    | Feature | Description |
    |---------|-------------|
    | `health_check(db)` | Detailed health status |
    | `is_healthy(db)` | Quick boolean check |
    | `get_stats(db)` | Database statistics |
    | `vacuum(db)` | Reclaim disk space |
    | `checkpoint(db)` | WAL checkpoint |
    | `backup(db, path)` | Online backup |
    | `restore(path)` | Restore from backup |
    | `tables()` | List registered tables |
    | `resolve(table)` | Get model for table |
    | `.paginate(page, size)` | Pagination helper |
    | `QueryLogger` | Query debugging |

    **For production:**
    - Use `is_healthy()` for Kubernetes liveness probes
    - Schedule regular `vacuum()` for disk cleanup
    - Monitor with `get_stats()` for growth tracking
    - Enable `QueryLogger` for debugging slow queries

    **Next up:** Tour 11 covers Metrics & Caching!
    """)
    return


@app.cell
def _(db):
    db.close()
    print("Database closed!")
    return


if __name__ == "__main__":
    app.run()
