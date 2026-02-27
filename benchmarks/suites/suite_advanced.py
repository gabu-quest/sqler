"""Advanced benchmark suite — scenarios 14-17."""

from __future__ import annotations

import json
import sqlite3
import statistics
import threading
import time

from sqler import (
    F,
    FTSIndex,
    PooledSQLerDB,
    SQLerDB,
    StaleVersionError,
    cached_query,
)

from benchmarks.core.base import BenchmarkResult, TimingStats
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.sqlite_baseline import (
    SQLiteFTSBaseline,
    create_table,
    insert_loop,
    insert_many,
)
from benchmarks.core.timer import PrecisionTimer
from benchmarks.generators.documents import DocumentGenerator
from benchmarks.generators.models import BenchmarkArticle, BenchmarkCounter

SUITE_NAME = "advanced"


def _timing_stats_from_list(sorted_times: list[float]) -> TimingStats:
    """Build TimingStats from a sorted list of ms values."""
    n = len(sorted_times)
    reliable = n >= 20
    if reliable:
        p95 = PrecisionTimer._percentile(sorted_times, 0.95)
        p99 = PrecisionTimer._percentile(sorted_times, 0.99)
    else:
        p95 = float("nan")
        p99 = float("nan")

    import math

    return TimingStats(
        iterations=n,
        min_ms=round(sorted_times[0], 4),
        max_ms=round(sorted_times[-1], 4),
        mean_ms=round(statistics.mean(sorted_times), 4),
        median_ms=round(statistics.median(sorted_times), 4),
        p95_ms=round(p95, 4) if not math.isnan(p95) else p95,
        p99_ms=round(p99, 4) if not math.isnan(p99) else p99,
        stddev_ms=round(statistics.stdev(sorted_times), 4) if n > 1 else 0,
        total_ms=round(sum(sorted_times), 4),
        reliable_p95=reliable,
    )


class FullTextSearch:
    """Scenario 14: FTS index create/rebuild + search operations at scale."""

    name = "full_text_search"
    suite = SUITE_NAME
    description = "FTSIndex create, rebuild, search, search_ranked, search_with_highlights"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for size in config.scale.table_sizes:
            docs = self.gen.generate("medium", size)

            # ---- sqler FTS ----
            db = SQLerDB.in_memory()
            BenchmarkArticle.set_db(db, table="articles")
            for doc in docs:
                article = BenchmarkArticle(
                    title=doc["name"],
                    content=doc["description"],
                    author=doc.get("category", "unknown"),
                    tags=doc.get("tags", []),
                )
                article.save()

            fts = FTSIndex(BenchmarkArticle, fields=["title", "content"])
            fts.create()

            # Measure rebuild (use measure() not measure_once())
            def do_rebuild(fts=fts):
                fts.rebuild()

            rebuild_stats = timer.measure(do_rebuild)

            # Measure search
            def do_search(fts=fts):
                return fts.search("alpha", limit=20)

            search_stats = timer.measure(do_search)

            # Measure search_ranked
            def do_ranked(fts=fts):
                return fts.search_ranked("alpha bravo", limit=20)

            ranked_stats = timer.measure(do_ranked)

            # Measure search_with_highlights
            def do_highlights(fts=fts):
                return fts.search_with_highlights("alpha")

            highlight_stats = timer.measure(do_highlights)

            for label, stats in [
                (f"sqler_rebuild_{size}", rebuild_stats),
                (f"sqler_search_{size}", search_stats),
                (f"sqler_ranked_{size}", ranked_stats),
                (f"sqler_highlights_{size}", highlight_stats),
            ]:
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="operation", value=label,
                    timing=stats, rows=size,
                    metadata={"storage_mode": "memory", "baseline": "sqler"},
                ))

            # ---- SQLite FTS baseline ----
            conn = sqlite3.connect(":memory:")
            create_table(conn, "articles")
            article_docs = [
                {"title": d["name"], "content": d["description"]}
                for d in docs
            ]
            insert_many(conn, "articles", article_docs)

            fts_base = SQLiteFTSBaseline(conn, "articles", ["title", "content"])
            fts_base.create()

            def do_sqlite_rebuild(fb=fts_base):
                fb.rebuild()

            sqlite_rebuild_stats = timer.measure(do_sqlite_rebuild)

            def do_sqlite_search(fb=fts_base):
                return fb.search("alpha", limit=20)

            sqlite_search_stats = timer.measure(do_sqlite_search)

            def do_sqlite_ranked(fb=fts_base):
                return fb.search_ranked("alpha bravo", limit=20)

            sqlite_ranked_stats = timer.measure(do_sqlite_ranked)

            for label, stats in [
                (f"sqlite_rebuild_{size}", sqlite_rebuild_stats),
                (f"sqlite_search_{size}", sqlite_search_stats),
                (f"sqlite_ranked_{size}", sqlite_ranked_stats),
            ]:
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="operation", value=label,
                    timing=stats, rows=size,
                    metadata={"storage_mode": "memory", "baseline": "sqlite"},
                ))

            conn.close()

        return results

    def teardown(self) -> None:
        pass


class OptimisticLockingContention:
    """Scenario 15: SQLerSafeModel with N threads incrementing same counter."""

    name = "optimistic_locking"
    suite = SUITE_NAME
    description = "Concurrent threads incrementing SQLerSafeModel counter"

    def setup(self, config: BenchmarkConfig) -> None:
        pass

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        increments_per_thread = 50
        import os
        import tempfile

        for n_threads in config.scale.thread_counts:
            # ---- sqler optimistic locking ----
            wall_times: list[float] = []

            for _ in range(config.iterations):
                with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                    db_path = f.name

                db = SQLerDB.on_disk(db_path)
                BenchmarkCounter.set_db(db, table="counters")
                counter = BenchmarkCounter(name="bench", tally=0)
                counter.save()
                counter_id = counter._id

                conflicts = [0]
                lock = threading.Lock()

                def worker():
                    local_db = SQLerDB.on_disk(db_path)
                    BenchmarkCounter.set_db(local_db, table="counters")
                    for _ in range(increments_per_thread):
                        while True:
                            try:
                                c = BenchmarkCounter.from_id(counter_id)
                                c.tally += 1
                                c.save()
                                break
                            except StaleVersionError:
                                with lock:
                                    conflicts[0] += 1

                start = time.perf_counter()
                threads = [threading.Thread(target=worker) for _ in range(n_threads)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                elapsed_ms = (time.perf_counter() - start) * 1000
                wall_times.append(elapsed_ms)

                os.unlink(db_path)

            sorted_times = sorted(wall_times)
            stats = _timing_stats_from_list(sorted_times)
            total_ops = n_threads * increments_per_thread
            throughput = total_ops / (stats.median_ms / 1000) if stats.median_ms > 0 else 0

            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="threads", value=f"sqler_{n_threads}",
                timing=stats, rows=total_ops,
                throughput=round(throughput, 1),
                metadata={"total_ops": total_ops, "storage_mode": "disk", "baseline": "sqler"},
            ))

            # ---- SQLite baseline: raw SELECT + UPDATE with version check ----
            sqlite_wall_times: list[float] = []

            for _ in range(config.iterations):
                with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                    db_path = f.name

                conn = sqlite3.connect(db_path)
                conn.execute(
                    "CREATE TABLE counters ("
                    "_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "tally INTEGER NOT NULL, "
                    "version INTEGER NOT NULL)"
                )
                conn.execute("INSERT INTO counters (tally, version) VALUES (0, 1)")
                conn.commit()
                conn.close()

                def sqlite_worker():
                    local_conn = sqlite3.connect(db_path, timeout=10)
                    for _ in range(increments_per_thread):
                        while True:
                            row = local_conn.execute(
                                "SELECT tally, version FROM counters WHERE _id = 1"
                            ).fetchone()
                            tally, version = row
                            cur = local_conn.execute(
                                "UPDATE counters SET tally = ?, version = ? "
                                "WHERE _id = 1 AND version = ?",
                                (tally + 1, version + 1, version),
                            )
                            local_conn.commit()
                            if cur.rowcount > 0:
                                break
                    local_conn.close()

                start = time.perf_counter()
                threads = [threading.Thread(target=sqlite_worker) for _ in range(n_threads)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                sqlite_wall_times.append((time.perf_counter() - start) * 1000)

                os.unlink(db_path)

            sorted_sqlite = sorted(sqlite_wall_times)
            sqlite_stats = _timing_stats_from_list(sorted_sqlite)
            sqlite_tp = total_ops / (sqlite_stats.median_ms / 1000) if sqlite_stats.median_ms > 0 else 0

            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="threads", value=f"sqlite_{n_threads}",
                timing=sqlite_stats, rows=total_ops,
                throughput=round(sqlite_tp, 1),
                metadata={"total_ops": total_ops, "storage_mode": "disk", "baseline": "sqlite"},
            ))

        return results

    def teardown(self) -> None:
        pass


class QueryCacheImpact:
    """Scenario 16: Same query with/without @cached_query, cold/warm/hit."""

    name = "query_cache"
    suite = SUITE_NAME
    description = "Query performance with/without cache: cold, warm, and hit paths"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)
        size = config.scale.table_sizes[-1]
        docs = self.gen.generate("small", size)

        db = SQLerDB.in_memory()
        db.bulk_upsert("bench", docs)

        conn = sqlite3.connect(":memory:")
        create_table(conn, "bench")
        insert_many(conn, "bench", docs)

        # sqler uncached query
        def do_uncached(db=db):
            return db.query("bench").filter(F("value") > 5000).all()

        uncached_stats = timer.measure(do_uncached)

        # sqler cached query — measure cold calls across iterations
        cold_times: list[float] = []
        for _ in range(config.iterations):
            @cached_query(ttl_seconds=0)  # TTL=0 forces cold on every call
            def cached_cold():
                return db.query("bench").filter(F("value") > 5000).all()

            start = time.perf_counter()
            cached_cold()
            cold_times.append((time.perf_counter() - start) * 1000)

        cold_stats = _timing_stats_from_list(sorted(cold_times))

        # sqler cached query — hit path
        @cached_query(ttl_seconds=60)
        def cached_hit():
            return db.query("bench").filter(F("value") > 5000).all()

        # Prime the cache
        cached_hit()

        hit_times: list[float] = []
        for _ in range(config.iterations):
            start = time.perf_counter()
            cached_hit()
            hit_times.append((time.perf_counter() - start) * 1000)

        hit_stats = _timing_stats_from_list(sorted(hit_times))

        # SQLite baseline — manual dict cache
        def do_sqlite_uncached(conn=conn):
            rows = conn.execute(
                "SELECT data FROM [bench] WHERE json_extract(data, '$.value') > ?",
                (5000,),
            ).fetchall()
            return [json.loads(r[0]) for r in rows]

        sqlite_uncached_stats = timer.measure(do_sqlite_uncached)

        # SQLite manual cache hit
        cached_result = do_sqlite_uncached()
        cache_store = {"key": cached_result}

        def do_sqlite_cache_hit():
            return cache_store["key"]

        sqlite_hit_stats = timer.measure(do_sqlite_cache_hit)

        for label, stats, baseline in [
            ("sqler_uncached", uncached_stats, "sqler"),
            ("sqler_cold", cold_stats, "sqler"),
            ("sqler_hit", hit_stats, "sqler"),
            ("sqlite_uncached", sqlite_uncached_stats, "sqlite"),
            ("sqlite_cache_hit", sqlite_hit_stats, "sqlite"),
        ]:
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="cache_state", value=label,
                timing=stats, rows=size,
                metadata={"storage_mode": "memory", "baseline": baseline},
            ))

        conn.close()
        return results

    def teardown(self) -> None:
        pass


class ConnectionPoolThroughput:
    """Scenario 17: PooledSQLerDB concurrent readers vs plain SQLerDB."""

    name = "connection_pool"
    suite = SUITE_NAME
    description = "PooledSQLerDB concurrent query throughput vs plain SQLerDB"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        import os
        import tempfile

        size = config.scale.table_sizes[-1]
        docs = self.gen.generate("small", size)

        for n_threads in config.scale.thread_counts:
            queries_per_thread = 20

            # ---- Plain SQLerDB ----
            plain_wall_times: list[float] = []

            for _ in range(config.iterations):
                with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                    plain_path = f.name

                plain_db = SQLerDB.on_disk(plain_path)
                plain_db.bulk_upsert("bench", docs)

                # Pre-open connections for symmetry
                local_dbs = [SQLerDB.on_disk(plain_path) for _ in range(n_threads)]

                def plain_worker(ldb):
                    for _ in range(queries_per_thread):
                        ldb.query("bench").filter(F("value") > 5000).limit(10).all()

                start = time.perf_counter()
                threads = [
                    threading.Thread(target=plain_worker, args=(local_dbs[i],))
                    for i in range(n_threads)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                plain_wall_times.append((time.perf_counter() - start) * 1000)

                os.unlink(plain_path)

            plain_sorted = sorted(plain_wall_times)
            plain_stats = _timing_stats_from_list(plain_sorted)
            total_queries = n_threads * queries_per_thread
            plain_tp = total_queries / (plain_stats.median_ms / 1000) if plain_stats.median_ms > 0 else 0

            # ---- PooledSQLerDB ----
            pool_wall_times: list[float] = []

            for _ in range(config.iterations):
                with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                    pool_path = f.name

                setup_db = SQLerDB.on_disk(pool_path)
                setup_db.bulk_upsert("bench", docs)
                setup_db.close()
                pool_db = PooledSQLerDB.on_disk(pool_path, max_connections=n_threads)

                def pool_worker():
                    for _ in range(queries_per_thread):
                        pool_db.query("bench").filter(F("value") > 5000).limit(10).all()

                start = time.perf_counter()
                threads = [threading.Thread(target=pool_worker) for _ in range(n_threads)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                pool_wall_times.append((time.perf_counter() - start) * 1000)

                os.unlink(pool_path)

            pool_sorted = sorted(pool_wall_times)
            pool_stats = _timing_stats_from_list(pool_sorted)
            pool_tp = total_queries / (pool_stats.median_ms / 1000) if pool_stats.median_ms > 0 else 0

            # ---- SQLite baseline ----
            sqlite_wall_times: list[float] = []

            for _ in range(config.iterations):
                with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                    sq_path = f.name

                sq_conn = sqlite3.connect(sq_path)
                create_table(sq_conn, "bench")
                insert_many(sq_conn, "bench", docs)
                sq_conn.close()

                def sqlite_worker(path):
                    c = sqlite3.connect(path)
                    for _ in range(queries_per_thread):
                        c.execute(
                            "SELECT data FROM [bench] "
                            "WHERE json_extract(data, '$.value') > ? LIMIT 10",
                            (5000,),
                        ).fetchall()
                    c.close()

                start = time.perf_counter()
                threads = [
                    threading.Thread(target=sqlite_worker, args=(sq_path,))
                    for _ in range(n_threads)
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                sqlite_wall_times.append((time.perf_counter() - start) * 1000)
                os.unlink(sq_path)

            sqlite_sorted = sorted(sqlite_wall_times)
            sqlite_stats = _timing_stats_from_list(sqlite_sorted)
            sqlite_tp = total_queries / (sqlite_stats.median_ms / 1000) if sqlite_stats.median_ms > 0 else 0

            for label, stats, tp, baseline in [
                (f"sqler_plain_{n_threads}t", plain_stats, plain_tp, "sqler"),
                (f"sqler_pooled_{n_threads}t", pool_stats, pool_tp, "sqler"),
                (f"sqlite_{n_threads}t", sqlite_stats, sqlite_tp, "sqlite"),
            ]:
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="backend", value=label,
                    timing=stats, rows=total_queries,
                    throughput=round(tp, 1),
                    metadata={"storage_mode": "disk", "baseline": baseline},
                ))

        return results

    def teardown(self) -> None:
        pass


SUITE = [
    FullTextSearch(),
    OptimisticLockingContention(),
    QueryCacheImpact(),
    ConnectionPoolThroughput(),
]
