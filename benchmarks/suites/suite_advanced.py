"""Advanced benchmark suite — scenarios 14-17.

v1.2 fairness fixes:
  - create_conn() with matched PRAGMAs (H-1, M-2)
  - FTS highlights baseline via search_with_highlights (M-9)
  - WAL mode for locking baseline (M-5)
  - Pre-open pool baseline connections outside timed window (M-6)
  - timing_stats_from_list extracted to core/timer.py (DRY)
  - Arm alternation per scenario (M-1)
  - gc.collect() between arms (M-4)
"""

from __future__ import annotations

import gc
import json
import os
import sqlite3
import tempfile
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

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.sqlite_baseline import (
    SQLiteFTSBaseline,
    create_conn,
    create_table,
    insert_loop,
    insert_many,
)
from benchmarks.core.timer import PrecisionTimer, timing_stats_from_list
from benchmarks.generators.documents import DocumentGenerator
from benchmarks.generators.models import BenchmarkArticle, BenchmarkCounter

SUITE_NAME = "advanced"


def _storage_modes(config: BenchmarkConfig) -> list[str]:
    if config.storage == "both":
        return ["memory", "disk"]
    return [config.storage]


def _mode_prefix(mode: str) -> str:
    return "mem" if mode == "memory" else "disk"


def _alternate_arms(scenario_name: str, arms: list[tuple]) -> list[tuple]:
    if hash(scenario_name) % 2:
        return list(reversed(arms))
    return list(arms)


class FullTextSearch:
    """Scenario 14: FTS index create/rebuild + search operations at scale.

    v1.2: Added sqlite highlights baseline (M-9).
    """

    name = "full_text_search"
    suite = SUITE_NAME
    description = "FTSIndex create, rebuild, search, search_ranked, search_with_highlights"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)

            for size in config.scale.table_sizes:
                docs = self.gen.generate("medium", size)

                with tempfile.TemporaryDirectory() as tmpdir:
                    arm_results = {}

                    def measure_sqler():
                        if mode == "memory":
                            db = SQLerDB.in_memory()
                        else:
                            db = SQLerDB.on_disk(os.path.join(tmpdir, "sqler_fts.db"))
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

                        def do_rebuild(fts=fts):
                            fts.rebuild()

                        arm_results["sqler_rebuild"] = timer.measure(do_rebuild)

                        gc.collect()

                        def do_search(fts=fts):
                            return fts.search("alpha", limit=20)

                        arm_results["sqler_search"] = timer.measure(do_search)

                        gc.collect()

                        def do_ranked(fts=fts):
                            return fts.search_ranked("alpha bravo", limit=20)

                        arm_results["sqler_ranked"] = timer.measure(do_ranked)

                        gc.collect()

                        def do_highlights(fts=fts):
                            return fts.search_with_highlights("alpha")

                        arm_results["sqler_highlights"] = timer.measure(do_highlights)

                    def measure_sqlite():
                        if mode == "memory":
                            conn = create_conn()
                        else:
                            conn = create_conn(os.path.join(tmpdir, "sqlite_fts.db"), "disk")
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

                        arm_results["sqlite_rebuild"] = timer.measure(do_sqlite_rebuild)

                        gc.collect()

                        def do_sqlite_search(fb=fts_base):
                            return fb.search("alpha", limit=20)

                        arm_results["sqlite_search"] = timer.measure(do_sqlite_search)

                        gc.collect()

                        def do_sqlite_ranked(fb=fts_base):
                            return fb.search_ranked("alpha bravo", limit=20)

                        arm_results["sqlite_ranked"] = timer.measure(do_sqlite_ranked)

                        gc.collect()

                        # v1.2: FTS highlights baseline (M-9)
                        def do_sqlite_highlights(fb=fts_base):
                            return fb.search_with_highlights("alpha")

                        arm_results["sqlite_highlights"] = timer.measure(do_sqlite_highlights)

                        conn.close()

                    arms = _alternate_arms(
                        f"{self.name}_{prefix}_{size}",
                        [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                    )
                    for label, fn in arms:
                        fn()
                        gc.collect()

                    for op in ["rebuild", "search", "ranked", "highlights"]:
                        for arm_label, baseline in [("sqler", "sqler"), ("sqlite", "sqlite")]:
                            stats_key = f"{arm_label}_{op}"
                            results.append(BenchmarkResult(
                                scenario=self.name, suite=self.suite,
                                parameter="operation",
                                value=f"{arm_label}_{prefix}_{op}_{size}",
                                timing=arm_results[stats_key], rows=size,
                                metadata={"storage_mode": mode, "baseline": baseline},
                            ))

        return results

    def teardown(self) -> None:
        pass


class OptimisticLockingContention:
    """Scenario 15: SQLerSafeModel with N threads incrementing same counter.

    v1.2: WAL mode for sqlite baseline (M-5), matched PRAGMAs.
    Always disk-based (locking requires it).
    """

    name = "optimistic_locking"
    suite = SUITE_NAME
    description = "Concurrent threads incrementing SQLerSafeModel counter"

    def setup(self, config: BenchmarkConfig) -> None:
        pass

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        increments_per_thread = 50

        for n_threads in config.scale.thread_counts:
            arm_results = {}

            def measure_sqler():
                wall_times: list[float] = []

                for _ in range(config.iterations):
                    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                        db_path = f.name

                    db = SQLerDB.on_disk(db_path)
                    BenchmarkCounter.set_db(db, table="counters")
                    counter = BenchmarkCounter(name="bench", tally=0)
                    counter.save()
                    counter_id = counter._id

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
                                    pass

                    start = time.perf_counter()
                    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join()
                    wall_times.append((time.perf_counter() - start) * 1000)

                    os.unlink(db_path)
                    # Clean up WAL/SHM files
                    for suffix in ("-wal", "-shm"):
                        p = db_path + suffix
                        if os.path.exists(p):
                            os.unlink(p)

                arm_results["sqler"] = timing_stats_from_list(sorted(wall_times))

            def measure_sqlite():
                sqlite_wall_times: list[float] = []

                for _ in range(config.iterations):
                    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                        db_path = f.name

                    # v1.2: matched PRAGMAs — WAL mode for fair locking comparison (M-5)
                    conn = create_conn(db_path, "disk")
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
                        local_conn = create_conn(db_path, "disk")
                        for _ in range(increments_per_thread):
                            while True:
                                row = local_conn.execute(
                                    "SELECT tally, version FROM counters WHERE _id = 1"
                                ).fetchone()
                                tally, version = row[0], row[1]
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
                    for suffix in ("-wal", "-shm"):
                        p = db_path + suffix
                        if os.path.exists(p):
                            os.unlink(p)

                arm_results["sqlite"] = timing_stats_from_list(sorted(sqlite_wall_times))

            arms = _alternate_arms(
                f"{self.name}_{n_threads}",
                [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
            )
            for label, fn in arms:
                fn()
                gc.collect()

            total_ops = n_threads * increments_per_thread
            for arm_label, baseline in [("sqler", "sqler"), ("sqlite", "sqlite")]:
                stats = arm_results[arm_label]
                tp = total_ops / (stats.median_ms / 1000) if stats.median_ms > 0 else 0
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="threads", value=f"{arm_label}_disk_{n_threads}",
                    timing=stats, rows=total_ops,
                    throughput=round(tp, 1),
                    metadata={"total_ops": total_ops, "storage_mode": "disk", "baseline": baseline},
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

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)
            size = config.scale.table_sizes[-1]
            docs = self.gen.generate("small", size)

            with tempfile.TemporaryDirectory() as tmpdir:
                if mode == "memory":
                    db = SQLerDB.in_memory()
                else:
                    db = SQLerDB.on_disk(os.path.join(tmpdir, "sqler_cache.db"))
                db.bulk_upsert("bench", docs)

                if mode == "memory":
                    conn = create_conn()
                else:
                    conn = create_conn(os.path.join(tmpdir, "sqlite_cache.db"), "disk")
                create_table(conn, "bench")
                insert_many(conn, "bench", docs)

                arm_results = {}

                def measure_sqler():
                    # Uncached query
                    def do_uncached(db=db):
                        return db.query("bench").filter(F("value") > 5000).all()
                    arm_results["sqler_uncached"] = timer.measure(do_uncached)

                    gc.collect()

                    # Cached cold (TTL=0 forces cold on every call)
                    cold_times: list[float] = []
                    for _ in range(config.iterations):
                        @cached_query(ttl_seconds=0)
                        def cached_cold():
                            return db.query("bench").filter(F("value") > 5000).all()
                        start = time.perf_counter()
                        cached_cold()
                        cold_times.append((time.perf_counter() - start) * 1000)
                    arm_results["sqler_cold"] = timing_stats_from_list(sorted(cold_times))

                    gc.collect()

                    # Cached hit
                    @cached_query(ttl_seconds=60)
                    def cached_hit():
                        return db.query("bench").filter(F("value") > 5000).all()
                    cached_hit()  # Prime

                    hit_times: list[float] = []
                    for _ in range(config.iterations):
                        start = time.perf_counter()
                        cached_hit()
                        hit_times.append((time.perf_counter() - start) * 1000)
                    arm_results["sqler_hit"] = timing_stats_from_list(sorted(hit_times))

                def measure_sqlite():
                    # Uncached
                    def do_sqlite_uncached(conn=conn):
                        rows = conn.execute(
                            "SELECT data FROM [bench] WHERE json_extract(data, '$.value') > ?",
                            (5000,),
                        ).fetchall()
                        return [json.loads(r["data"]) for r in rows]
                    arm_results["sqlite_uncached"] = timer.measure(do_sqlite_uncached)

                    gc.collect()

                    # Manual cache hit
                    cached_result = do_sqlite_uncached()
                    cache_store = {"key": cached_result}

                    def do_sqlite_cache_hit():
                        return cache_store["key"]
                    arm_results["sqlite_cache_hit"] = timer.measure(do_sqlite_cache_hit)

                arms = _alternate_arms(
                    f"{self.name}_{prefix}",
                    [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                )
                for label, fn in arms:
                    fn()
                    gc.collect()

                for label, stats_key, baseline in [
                    (f"sqler_{prefix}_uncached", "sqler_uncached", "sqler"),
                    (f"sqler_{prefix}_cold", "sqler_cold", "sqler"),
                    (f"sqler_{prefix}_hit", "sqler_hit", "sqler"),
                    (f"sqlite_{prefix}_uncached", "sqlite_uncached", "sqlite"),
                    (f"sqlite_{prefix}_cache_hit", "sqlite_cache_hit", "sqlite"),
                ]:
                    results.append(BenchmarkResult(
                        scenario=self.name, suite=self.suite,
                        parameter="cache_state", value=label,
                        timing=arm_results[stats_key], rows=size,
                        metadata={"storage_mode": mode, "baseline": baseline},
                    ))

                conn.close()

        return results

    def teardown(self) -> None:
        pass


class ConnectionPoolThroughput:
    """Scenario 17: PooledSQLerDB concurrent readers vs plain SQLerDB.

    v1.2: Pre-open baseline connections outside timed window (M-6).
    Always disk-based (connection pool requires it).
    """

    name = "connection_pool"
    suite = SUITE_NAME
    description = "PooledSQLerDB concurrent query throughput vs plain SQLerDB"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []

        size = config.scale.table_sizes[-1]
        docs = self.gen.generate("small", size)

        for n_threads in config.scale.thread_counts:
            queries_per_thread = 20
            arm_results = {}

            def measure_plain():
                plain_wall_times: list[float] = []

                for _ in range(config.iterations):
                    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                        plain_path = f.name

                    plain_db = SQLerDB.on_disk(plain_path)
                    plain_db.bulk_upsert("bench", docs)

                    # Pre-open connections outside timed window (M-6)
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

                    for ldb in local_dbs:
                        ldb.close()
                    os.unlink(plain_path)
                    for suffix in ("-wal", "-shm"):
                        p = plain_path + suffix
                        if os.path.exists(p):
                            os.unlink(p)

                arm_results["sqler_plain"] = timing_stats_from_list(sorted(plain_wall_times))

            def measure_pooled():
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
                    for suffix in ("-wal", "-shm"):
                        p = pool_path + suffix
                        if os.path.exists(p):
                            os.unlink(p)

                arm_results["sqler_pooled"] = timing_stats_from_list(sorted(pool_wall_times))

            def measure_sqlite():
                sqlite_wall_times: list[float] = []

                for _ in range(config.iterations):
                    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                        sq_path = f.name

                    sq_conn = create_conn(sq_path, "disk")
                    create_table(sq_conn, "bench")
                    insert_many(sq_conn, "bench", docs)
                    sq_conn.close()

                    # Pre-open connections outside timed window (M-6)
                    pre_conns = [create_conn(sq_path, "disk") for _ in range(n_threads)]

                    def sqlite_worker(c):
                        for _ in range(queries_per_thread):
                            c.execute(
                                "SELECT data FROM [bench] "
                                "WHERE json_extract(data, '$.value') > ? LIMIT 10",
                                (5000,),
                            ).fetchall()

                    start = time.perf_counter()
                    threads = [
                        threading.Thread(target=sqlite_worker, args=(pre_conns[i],))
                        for i in range(n_threads)
                    ]
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join()
                    sqlite_wall_times.append((time.perf_counter() - start) * 1000)

                    for c in pre_conns:
                        c.close()
                    os.unlink(sq_path)
                    for suffix in ("-wal", "-shm"):
                        p = sq_path + suffix
                        if os.path.exists(p):
                            os.unlink(p)

                arm_results["sqlite"] = timing_stats_from_list(sorted(sqlite_wall_times))

            # Alternate sqler group vs sqlite (M-1)
            arms = _alternate_arms(
                f"{self.name}_{n_threads}",
                [("sqler_group", lambda: (measure_plain(), gc.collect(), measure_pooled())),
                 ("sqlite", measure_sqlite)],
            )
            for label, fn in arms:
                fn()
                gc.collect()

            total_queries = n_threads * queries_per_thread
            for label, stats_key, baseline in [
                (f"sqler_disk_plain_{n_threads}t", "sqler_plain", "sqler"),
                (f"sqler_disk_pooled_{n_threads}t", "sqler_pooled", "sqler"),
                (f"sqlite_disk_{n_threads}t", "sqlite", "sqlite"),
            ]:
                stats = arm_results[stats_key]
                tp = total_queries / (stats.median_ms / 1000) if stats.median_ms > 0 else 0
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
