"""Advanced benchmark suite — scenarios 14-17."""

from __future__ import annotations

import threading

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.timer import PrecisionTimer
from benchmarks.generators.documents import DocumentGenerator
from benchmarks.generators.models import BenchmarkArticle, BenchmarkCounter

from sqler import (
    F,
    FTSIndex,
    PooledSQLerDB,
    QueryCache,
    SQLerDB,
    StaleVersionError,
    cached_query,
)

SUITE_NAME = "advanced"


class FullTextSearch:
    """Scenario 14: FTS index create/rebuild + search operations at scale."""

    name = "full_text_search"
    suite = SUITE_NAME
    description = "FTSIndex create, rebuild, search, search_ranked, search_with_highlights"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=1, iterations=config.iterations)

        for size in config.scale.table_sizes:
            docs = self.gen.generate("medium", size)

            # Prepare articles
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

            # Measure rebuild
            def do_rebuild(fts=fts):
                fts.rebuild()

            rebuild_stats = timer.measure_once(do_rebuild)

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
                (f"rebuild_{size}", rebuild_stats),
                (f"search_{size}", search_stats),
                (f"ranked_{size}", ranked_stats),
                (f"highlights_{size}", highlight_stats),
            ]:
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="operation", value=label,
                    timing=stats, rows=size,
                ))

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

        for n_threads in config.scale.thread_counts:
            import tempfile
            import time

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

            total_ops = n_threads * increments_per_thread
            throughput = total_ops / (elapsed_ms / 1000) if elapsed_ms > 0 else 0

            from benchmarks.core.base import TimingStats

            stats = TimingStats(
                iterations=1, min_ms=elapsed_ms, max_ms=elapsed_ms,
                mean_ms=elapsed_ms, median_ms=elapsed_ms,
                p95_ms=elapsed_ms, p99_ms=elapsed_ms,
                stddev_ms=0, total_ms=elapsed_ms,
            )

            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="threads", value=n_threads,
                timing=stats, rows=total_ops,
                throughput=round(throughput, 1),
                metadata={"conflicts": conflicts[0], "total_ops": total_ops},
            ))

            import os
            os.unlink(db_path)

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
        timer = PrecisionTimer(warmup=0, iterations=config.iterations)
        size = config.scale.table_sizes[-1]
        docs = self.gen.generate("small", size)

        db = SQLerDB.in_memory()
        db.bulk_upsert("bench", docs)

        cache = QueryCache(max_size=100, default_ttl_seconds=60)

        # Uncached query
        def do_uncached(db=db):
            return db.query("bench").filter(F("value") > 5000).all()

        uncached_stats = timer.measure(do_uncached)

        # Cached query — first call (cold)
        @cached_query(ttl_seconds=60)
        def cached_fn():
            return db.query("bench").filter(F("value") > 5000).all()

        # Cold call
        import time
        cold_start = time.perf_counter()
        cached_fn()
        cold_ms = (time.perf_counter() - cold_start) * 1000

        # Warm/hit calls
        hit_times = []
        for _ in range(config.iterations):
            start = time.perf_counter()
            cached_fn()
            hit_times.append((time.perf_counter() - start) * 1000)

        from benchmarks.core.base import TimingStats
        import statistics

        hit_sorted = sorted(hit_times)
        hit_stats = TimingStats(
            iterations=len(hit_times),
            min_ms=round(hit_sorted[0], 4),
            max_ms=round(hit_sorted[-1], 4),
            mean_ms=round(statistics.mean(hit_sorted), 4),
            median_ms=round(statistics.median(hit_sorted), 4),
            p95_ms=round(PrecisionTimer._percentile(hit_sorted, 0.95), 4),
            p99_ms=round(PrecisionTimer._percentile(hit_sorted, 0.99), 4),
            stddev_ms=round(statistics.stdev(hit_sorted), 4) if len(hit_sorted) > 1 else 0,
            total_ms=round(sum(hit_sorted), 4),
        )

        cold_stats = TimingStats(
            iterations=1, min_ms=round(cold_ms, 4), max_ms=round(cold_ms, 4),
            mean_ms=round(cold_ms, 4), median_ms=round(cold_ms, 4),
            p95_ms=round(cold_ms, 4), p99_ms=round(cold_ms, 4),
            stddev_ms=0, total_ms=round(cold_ms, 4),
        )

        for label, stats in [
            ("uncached", uncached_stats),
            ("cold", cold_stats),
            ("hit", hit_stats),
        ]:
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="cache_state", value=label,
                timing=stats, rows=size,
            ))

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
        import tempfile
        import time

        size = config.scale.table_sizes[-1]
        docs = self.gen.generate("small", size)

        for n_threads in config.scale.thread_counts:
            queries_per_thread = 20

            # --- Plain SQLerDB ---
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                plain_path = f.name

            plain_db = SQLerDB.on_disk(plain_path)
            plain_db.bulk_upsert("bench", docs)

            def plain_worker():
                local_db = SQLerDB.on_disk(plain_path)
                for _ in range(queries_per_thread):
                    local_db.query("bench").filter(F("value") > 5000).limit(10).all()

            start = time.perf_counter()
            threads = [threading.Thread(target=plain_worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            plain_ms = (time.perf_counter() - start) * 1000

            # --- PooledSQLerDB ---
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                pool_path = f.name

            # Populate via plain db first — PooledSQLerDB doesn't have bulk_upsert
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
            pool_ms = (time.perf_counter() - start) * 1000

            total_queries = n_threads * queries_per_thread

            from benchmarks.core.base import TimingStats

            for label, ms in [("plain", plain_ms), ("pooled", pool_ms)]:
                throughput = total_queries / (ms / 1000) if ms > 0 else 0
                stats = TimingStats(
                    iterations=1, min_ms=ms, max_ms=ms,
                    mean_ms=ms, median_ms=ms, p95_ms=ms, p99_ms=ms,
                    stddev_ms=0, total_ms=ms,
                )
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="backend", value=f"{label}_{n_threads}t",
                    timing=stats, rows=total_queries,
                    throughput=round(throughput, 1),
                ))

            import os
            os.unlink(plain_path)
            os.unlink(pool_path)

        return results

    def teardown(self) -> None:
        pass


SUITE = [
    FullTextSearch(),
    OptimisticLockingContention(),
    QueryCacheImpact(),
    ConnectionPoolThroughput(),
]
