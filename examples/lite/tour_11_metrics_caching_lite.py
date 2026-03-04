# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo"]
# ///
"""SQLer Lite Tour: Metrics, Caching & Pools - Works in Pyodide/WASM!"""

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
    # SQLer Lite Tour: Metrics, Caching & Pools

    This notebook covers SQLer's production-ready features for monitoring
    and performance optimization using **Lite models** (dataclasses).

    You'll learn:

    1. Metrics collection (Prometheus/StatsD compatible)
    2. Query result caching with TTL
    3. Manual cache invalidation (Lite alternative to CacheAwareModel)
    4. Connection pooling concepts

    Let's explore!
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    > **Lite vs Pydantic**: This tour uses `SQLerLiteModel` (dataclasses) so it runs
    > in your browser via WebAssembly. With `SQLerModel` (Pydantic), you also get:
    > - `CacheAwareModel` mixin for automatic cache invalidation on save/delete
    >
    > ```python
    > # Pydantic version — automatic invalidation
    > from sqler.cache import CacheAwareModel
    >
    > class Product(CacheAwareModel, SQLerModel):
    >     _table = "products"
    >     name: str
    >     class Meta:
    >         cache_table = "products"
    > # Cache auto-invalidated on save/delete!
    > ```
    >
    > Run locally: `uv run marimo edit examples/tour_11_metrics_caching.py`
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
    _sqler_cache = importlib.import_module("sqler.cache")
    _sqler_metrics = importlib.import_module("sqler.metrics")

    F = _sqler.F
    SQLerDB = _sqler.SQLerDB
    SQLerLiteModel = _sqler.SQLerLiteModel
    QueryCache = _sqler_cache.QueryCache
    cached_query = _sqler_cache.cached_query
    MetricsCollector = _sqler_metrics.MetricsCollector
    metrics = _sqler_metrics.metrics

    db = SQLerDB.in_memory()
    print("Database connected!")
    print("Imports ready: metrics, cache modules")
    return (
        F,
        MetricsCollector,
        QueryCache,
        SQLerDB,
        SQLerLiteModel,
        cached_query,
        dataclass,
        db,
        metrics,
    )


@app.cell
def _(SQLerLiteModel, dataclass, db):
    @dataclass
    class User(SQLerLiteModel):
        __tablename__ = "users"

        name: str
        email: str
        active: bool = True

    User.set_db(db)

    # Create sample users
    for _i in range(10):
        User(name=f"User{_i}", email=f"user{_i}@example.com", active=_i % 2 == 0).save()

    print(f"Created {User.query().count()} users")
    return (User,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. Metrics Collection

    SQLer's `MetricsCollector` automatically tracks query performance
    and exports in Prometheus or StatsD formats:
    """)
    return


@app.cell
def _(F, User, metrics):
    # Enable the global metrics collector
    metrics.reset()  # Clear any previous metrics
    metrics.enable(slow_threshold_ms=50)

    # Run some queries
    User.query().all()
    User.query().filter(F("active") == True).all()
    User.query().count()
    User.query().filter(F("name") == "User1").first()

    print("Metrics collector enabled!")
    print("  Slow query threshold: 50ms")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 3. Collected Metrics

    View aggregated query statistics:
    """)
    return


@app.cell
def _(metrics):
    # Get aggregated metrics
    _m = metrics.get_metrics()

    print("Collected Metrics:")
    print(f"  Total queries: {_m['queries']['total_queries']}")
    print(f"  Total errors: {_m['queries']['total_errors']}")
    print(f"  Avg duration: {_m['queries']['avg_duration_ms']:.2f}ms")
    print(f"  Max duration: {_m['queries']['max_duration_ms']:.2f}ms")

    # Per-table operations
    print("\nPer-table operations:")
    for _table, _ops in _m['tables'].items():
        print(f"  {_table}: {_ops}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 4. Prometheus Export

    Export metrics in Prometheus text format for monitoring:
    """)
    return


@app.cell
def _(metrics):
    # Export in Prometheus format
    _prom = metrics.prometheus_export()

    print("Prometheus export (partial):")
    for _line in _prom.split("\n")[:15]:
        if _line:
            print(f"  {_line}")
    print("  ...")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 5. Query Result Caching

    Cache expensive query results with TTL-based expiration:
    """)
    return


@app.cell
def _(QueryCache):
    # Create a cache
    cache = QueryCache(max_size=100, default_ttl_seconds=60)

    print("Cache created!")
    print("  Max size: 100 entries")
    print("  Default TTL: 60 seconds")
    return (cache,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. Manual Caching

    Check, get, and set cache entries manually:
    """)
    return


@app.cell
def _(F, User, cache):
    # Manual caching
    _key = "active_users"

    if not cache.has(_key):
        print("Cache MISS - fetching from database...")
        _users = User.query().filter(F("active") == True).all()
        cache.set(_key, _users, ttl_seconds=30, table="users")
    else:
        print("Cache HIT!")
        _users = cache.get(_key)

    print(f"Got {len(_users)} active users")

    # Check again (should be cached now)
    if cache.has(_key):
        print("\nSecond check: Cache HIT!")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. Cache Statistics

    Monitor cache performance:
    """)
    return


@app.cell
def _(cache):
    # Cache statistics
    _stats = cache.stats

    print("Cache Statistics:")
    print(f"  Size: {_stats.size}/{_stats.max_size}")
    print(f"  Hits: {_stats.hits}")
    print(f"  Misses: {_stats.misses}")
    print(f"  Hit rate: {_stats.hit_rate:.1%}")
    print(f"  Evictions: {_stats.evictions}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 8. Cached Query Decorator

    Use `@cached_query` for automatic caching:
    """)
    return


@app.cell
def _(User, cache, cached_query):
    @cached_query(ttl_seconds=60, table="users", cache=cache)
    def get_user_count():
        print("  (executing query...)")
        return User.query().count()

    # First call - hits database
    print("First call:")
    _count1 = get_user_count()
    print(f"Count: {_count1}")

    # Second call - from cache
    print("\nSecond call:")
    _count2 = get_user_count()
    print(f"Count: {_count2}")

    # Invalidate and call again
    cache.invalidate_table("users")
    print("\nAfter invalidate_table('users'):")
    _count3 = get_user_count()
    print(f"Count: {_count3}")
    return (get_user_count,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 9. Pattern-Based Invalidation

    Invalidate multiple cache entries at once using wildcards:
    """)
    return


@app.cell
def _(cache):
    # Add multiple cache entries
    cache.set("users:page:1", ["user1", "user2"], ttl_seconds=60)
    cache.set("users:page:2", ["user3", "user4"], ttl_seconds=60)
    cache.set("users:page:3", ["user5", "user6"], ttl_seconds=60)
    cache.set("products:page:1", ["prod1", "prod2"], ttl_seconds=60)

    print(f"Cache size: {len(cache)}")

    # Invalidate all user pages
    _count = cache.invalidate_pattern("users:page:*")
    print(f"\nInvalidated {_count} entries matching 'users:page:*'")
    print(f"Cache size after: {len(cache)}")

    # products:page:1 should still exist
    print(f"products:page:1 exists: {cache.has('products:page:1')}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 10. Manual Cache Invalidation (Lite Pattern)

    With Lite models, manually invalidate cache after save/delete operations.

    **Pydantic alternative:** Use `CacheAwareModel` mixin for automatic invalidation.
    """)
    return


@app.cell
def _(SQLerLiteModel, cache, dataclass, db):
    @dataclass
    class Product(SQLerLiteModel):
        __tablename__ = "products"

        name: str
        price: float

    Product.set_db(db)

    # Cache some product data
    cache.set("all_products", "cached data", table="products")
    print(f"Cached 'all_products': {cache.has('all_products')}")

    # With Lite models, manually invalidate cache after save/delete
    prod = Product(name="Widget", price=29.99)
    prod.save()
    cache.invalidate_table("products")  # manual invalidation

    print(f"After save + manual invalidation - 'all_products' cached: {cache.has('all_products')}")
    return (Product, prod)


@app.cell
def _(mo):
    mo.md(r"""
    ## 11. Connection Pooling

    For production with concurrent access, use connection pools.

    **Note:** Pools require disk-based databases, so we'll show the API:
    """)
    return


@app.cell
def _():
    print("Connection Pool Features:")
    print("")
    print("  # Create pooled database")
    print("  from sqler import PooledSQLerDB")
    print("  db = PooledSQLerDB.on_disk('app.db', max_connections=10)")
    print("")
    print("  # Use exactly like SQLerDB")
    print("  User.set_db(db)")
    print("  users = User.query().all()")
    print("")
    print("  # Check pool stats")
    print("  stats = db.pool_stats()")
    print("  print(f'In use: {stats.in_use_connections}')")
    print("  print(f'Available: {stats.available_connections}')")
    print("")
    print("WAL mode benefits:")
    print("  - Multiple concurrent readers")
    print("  - Writers don't block readers")
    print("  - Readers don't block writers")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 12. Metrics with Labels

    Add custom labels for multi-tenant or multi-instance monitoring:
    """)
    return


@app.cell
def _(MetricsCollector, User):
    # Create collector with custom labels
    _collector = MetricsCollector()
    _collector.enable(
        slow_threshold_ms=100,
        labels={"service": "api", "environment": "production"}
    )

    # Run a query
    User.query().count()

    # Export shows labels
    _prom = _collector.prometheus_export()
    print("Prometheus with labels:")
    for _line in _prom.split("\n")[:5]:
        if _line and not _line.startswith("#"):
            print(f"  {_line}")

    _collector.disable()
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    SQLer production features (all work with Lite models!):

    | Feature | Description |
    |---------|-------------|
    | `MetricsCollector` | Collect query performance metrics |
    | `.prometheus_export()` | Export in Prometheus format |
    | `.statsd_export()` | Export in StatsD format |
    | `QueryCache` | TTL-based query result cache |
    | `@cached_query` | Decorator for automatic caching |
    | `.invalidate_pattern()` | Wildcard cache invalidation |
    | `.invalidate_table()` | Manual cache invalidation (Lite pattern) |
    | `ConnectionPool` | Thread-safe connection pooling |
    | `PooledSQLerDB` | SQLerDB with built-in pooling |

    **Best Practices:**
    - Enable metrics for production monitoring
    - Cache expensive queries with appropriate TTL
    - Use `invalidate_table()` after bulk updates (Lite pattern)
    - With Pydantic: use `CacheAwareModel` for automatic invalidation
    - Use connection pooling for concurrent access
    - Set `slow_threshold_ms` to track slow queries

    **That's the end of the SQLer Lite tour!**
    """)
    return


@app.cell
def _(db, metrics):
    metrics.disable()
    db.close()
    print("Database closed!")
    return


if __name__ == "__main__":
    app.run()
