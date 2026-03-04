# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo"]
# ///
"""SQLer Lite Tour: Database Operations - Works in Pyodide/WASM!"""

import marimo

__generated_with = "0.18.4"
app = marimo.App(width="medium")


@app.cell
def _():
    # --- marimo scaffolding (please ignore) ---
    import marimo as mo

    return (mo,)


@app.cell
async def _():
    # --- WASM scaffolding (please ignore) ---
    # Loads sqlite3 + sqler in Pyodide/browser environments.
    # Not needed when running locally with `marimo edit`.
    import sys

    pyodide_sqlite3_ready = True
    sqler_ready = True
    if sys.platform == "emscripten":
        pyodide_sqlite3_ready = False
        sqler_ready = False
        try:
            import pyodide

            await pyodide.loadPackage("sqlite3")
            pyodide_sqlite3_ready = True
        except Exception:
            try:
                import js

                await js.pyodide.loadPackage("sqlite3")
                pyodide_sqlite3_ready = True
            except Exception as exc:
                print("Failed to load sqlite3 in Pyodide:", exc)

        import importlib.util as importlib_util

        if importlib_util.find_spec("sqler") is not None:
            sqler_ready = True
        else:
            try:
                import micropip

                await micropip.install("sqler")
            except Exception as exc:
                print("Failed to install sqler in Pyodide:", exc)
            else:
                if importlib_util.find_spec("sqler") is not None:
                    sqler_ready = True

    return (pyodide_sqlite3_ready, sqler_ready)


@app.cell
def _(mo):
    mo.md(r"""
    # SQLer Lite Tour: Database Operations

    This notebook covers SQLer's database operations for production use
    including health checks, statistics, and maintenance.

    You'll learn:

    1. Health checks for monitoring
    2. Database statistics
    3. Vacuum (reclaim space)
    4. Registry and table management
    5. Pagination
    6. Query logging

    Let's explore!
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    > **Lite vs Pydantic**: This tour uses `SQLerLiteModel` (dataclasses) so it runs
    > in your browser via WebAssembly. The database operations shown here work
    > identically with both `SQLerModel` (Pydantic) and `SQLerLiteModel` (dataclasses)
    > — they operate at the database level, not the model level.
    >
    > Run locally: `uv run marimo edit examples/tour_10_db_operations.py`
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 1. Setting Up
    """)
    return


@app.cell
def _(pyodide_sqlite3_ready, sqler_ready):
    if not pyodide_sqlite3_ready:
        raise RuntimeError(
            "sqlite3 is required in Pyodide; failed to load package 'sqlite3'."
        )
    if not sqler_ready:
        raise RuntimeError(
            "sqler is required in Pyodide; failed to install sqler wheel."
        )

    import importlib
    from dataclasses import dataclass

    _sqler = importlib.import_module("sqler")
    F = _sqler.F
    SQLerDB = _sqler.SQLerDB
    SQLerLiteModel = _sqler.SQLerLiteModel
    resolve = _sqler.resolve
    tables = _sqler.tables

    # Import ops module
    _sqler_ops = importlib.import_module("sqler.ops")
    get_stats = _sqler_ops.get_stats
    health_check = _sqler_ops.health_check
    is_healthy = _sqler_ops.is_healthy
    vacuum = _sqler_ops.vacuum

    # Import logging
    _sqler_logging = importlib.import_module("sqler.logging")
    query_logger = _sqler_logging.query_logger

    db = SQLerDB.in_memory()
    print("Database connected!")
    print("\nOperations available:")
    print("  - health_check(): Detailed health status")
    print("  - is_healthy(): Quick boolean check")
    print("  - get_stats(): Database statistics")
    print("  - vacuum(): Reclaim disk space")
    return (
        F,
        SQLerDB,
        SQLerLiteModel,
        dataclass,
        db,
        get_stats,
        health_check,
        is_healthy,
        query_logger,
        resolve,
        tables,
        vacuum,
    )


@app.cell
def _(SQLerLiteModel, dataclass, db):
    @dataclass
    class User(SQLerLiteModel):
        __tablename__ = "users"

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
def _(resolve, tables):
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
def _(SQLerLiteModel, dataclass, db):
    @dataclass
    class Product(SQLerLiteModel):
        __tablename__ = "products"

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
def _(F, Product, query_logger):
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
