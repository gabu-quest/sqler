"""Insert benchmark suite — scenarios 1-4."""

from __future__ import annotations

import json
import sqlite3
import statistics

from sqler import SQLerDB

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.sqlite_baseline import create_table, insert_loop, insert_many
from benchmarks.core.timer import PrecisionTimer
from benchmarks.generators.documents import DocumentGenerator
from benchmarks.generators.models import BenchmarkItem, BenchmarkItemLite

SUITE_NAME = "insert"


class BulkInsertScaling:
    """Scenario 1: Bulk insert at increasing row counts."""

    name = "bulk_insert_scaling"
    suite = SUITE_NAME
    description = "db.bulk_upsert() at 1K → max rows, measuring throughput scaling"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for size in config.scale.bulk_sizes:
            docs = self.gen.generate("small", size)

            # Pre-create DBs outside timed closure
            sqler_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
            sqler_iter = iter(sqler_dbs)

            def do_insert(docs=docs):
                db = next(sqler_iter)
                db.bulk_upsert("bench", docs)

            stats = timer.measure(do_insert)
            throughput = size / (stats.median_ms / 1000) if stats.median_ms > 0 else 0

            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="rows", value=f"sqler_{size}",
                timing=stats, rows=size,
                throughput=round(throughput, 1),
                metadata={"storage_mode": "memory", "baseline": "sqler"},
            ))

            # SQLite baseline: executemany
            sqlite_conns = [sqlite3.connect(":memory:") for _ in range(config.warmup + config.iterations)]
            for c in sqlite_conns:
                create_table(c, "bench")
            sqlite_iter = iter(sqlite_conns)

            def do_sqlite(docs=docs):
                conn = next(sqlite_iter)
                insert_many(conn, "bench", docs)

            sqlite_stats = timer.measure(do_sqlite)
            sqlite_tp = size / (sqlite_stats.median_ms / 1000) if sqlite_stats.median_ms > 0 else 0

            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="rows", value=f"sqlite_{size}",
                timing=sqlite_stats, rows=size,
                throughput=round(sqlite_tp, 1),
                metadata={"storage_mode": "memory", "baseline": "sqlite"},
            ))

            for c in sqlite_conns:
                c.close()

        return results

    def teardown(self) -> None:
        pass


class SingleVsBulk:
    """Scenario 2: model.save() in loop vs bulk_upsert for same data."""

    name = "single_vs_bulk"
    suite = SUITE_NAME
    description = "Compare model.save() loop vs db.bulk_upsert() at same row count"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        sizes = [s for s in config.scale.bulk_sizes if s <= 10_000]
        if not sizes:
            sizes = [config.scale.bulk_sizes[0]]

        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for size in sizes:
            docs = self.gen.generate("small", size)

            # sqler bulk insert
            sqler_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
            sqler_iter = iter(sqler_dbs)

            def do_bulk(docs=docs):
                db = next(sqler_iter)
                db.bulk_upsert("bench", docs)

            bulk_stats = timer.measure(do_bulk)

            # sqler single save
            single_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
            single_iter = iter(single_dbs)

            def do_single(docs=docs):
                db = next(single_iter)
                BenchmarkItem.set_db(db, table="bench_model")
                for doc in docs:
                    item = BenchmarkItem(**doc)
                    item.save()

            single_stats = timer.measure(do_single)

            # SQLite baselines
            batch_conns = [sqlite3.connect(":memory:") for _ in range(config.warmup + config.iterations)]
            for c in batch_conns:
                create_table(c, "bench")
            batch_iter = iter(batch_conns)

            def do_sqlite_batch(docs=docs):
                conn = next(batch_iter)
                insert_many(conn, "bench", docs)

            sqlite_batch_stats = timer.measure(do_sqlite_batch)

            loop_conns = [sqlite3.connect(":memory:") for _ in range(config.warmup + config.iterations)]
            for c in loop_conns:
                create_table(c, "bench")
            loop_iter = iter(loop_conns)

            def do_sqlite_loop(docs=docs):
                conn = next(loop_iter)
                insert_loop(conn, "bench", docs)

            sqlite_loop_stats = timer.measure(do_sqlite_loop)

            for label, stats, baseline in [
                (f"sqler_bulk_{size}", bulk_stats, "sqler"),
                (f"sqler_single_{size}", single_stats, "sqler"),
                (f"sqlite_batch_{size}", sqlite_batch_stats, "sqlite"),
                (f"sqlite_loop_{size}", sqlite_loop_stats, "sqlite"),
            ]:
                tp = size / (stats.median_ms / 1000) if stats.median_ms > 0 else 0
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="method", value=label,
                    timing=stats, rows=size,
                    throughput=round(tp, 1),
                    metadata={"storage_mode": "memory", "baseline": baseline},
                ))

            for c in batch_conns:
                c.close()
            for c in loop_conns:
                c.close()

        return results

    def teardown(self) -> None:
        pass


class DocumentSizeImpact:
    """Scenario 3: bulk_upsert with different doc size profiles, fixed row count."""

    name = "doc_size_impact"
    suite = SUITE_NAME
    description = "bulk_upsert with tiny/small/medium/large/huge docs at fixed 10K rows"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        row_count = min(10_000, config.scale.max_rows)
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for profile in ("tiny", "small", "medium", "large", "huge"):
            docs = self.gen.generate(profile, row_count)
            avg_doc_bytes = round(statistics.mean(len(json.dumps(d)) for d in docs))

            # sqler
            sqler_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
            sqler_iter = iter(sqler_dbs)

            def do_insert(docs=docs):
                db = next(sqler_iter)
                db.bulk_upsert("bench", docs)

            stats = timer.measure(do_insert)
            throughput = row_count / (stats.median_ms / 1000) if stats.median_ms > 0 else 0

            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="profile", value=f"sqler_{profile}",
                timing=stats, rows=row_count,
                throughput=round(throughput, 1),
                metadata={"avg_doc_bytes": avg_doc_bytes, "storage_mode": "memory", "baseline": "sqler"},
            ))

            # SQLite baseline
            sqlite_conns = [sqlite3.connect(":memory:") for _ in range(config.warmup + config.iterations)]
            for c in sqlite_conns:
                create_table(c, "bench")
            sqlite_iter = iter(sqlite_conns)

            def do_sqlite(docs=docs):
                conn = next(sqlite_iter)
                insert_many(conn, "bench", docs)

            sqlite_stats = timer.measure(do_sqlite)
            sqlite_tp = row_count / (sqlite_stats.median_ms / 1000) if sqlite_stats.median_ms > 0 else 0

            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="profile", value=f"sqlite_{profile}",
                timing=sqlite_stats, rows=row_count,
                throughput=round(sqlite_tp, 1),
                metadata={"avg_doc_bytes": avg_doc_bytes, "storage_mode": "memory", "baseline": "sqlite"},
            ))

            for c in sqlite_conns:
                c.close()

        return results

    def teardown(self) -> None:
        pass


class ModelOverhead:
    """Scenario 4: Same data through SQLerModel vs SQLerLiteModel vs db.insert_document."""

    name = "model_overhead"
    suite = SUITE_NAME
    description = "Compare SQLerModel.save() vs SQLerLiteModel.save() vs db.insert_document()"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        row_count = min(5_000, config.scale.max_rows)
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        docs = self.gen.generate("small", row_count)

        # Raw insert_document
        raw_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
        raw_iter = iter(raw_dbs)

        def do_raw(docs=docs):
            db = next(raw_iter)
            for doc in docs:
                db.insert_document("bench", doc)

        raw_stats = timer.measure(do_raw)

        # SQLerModel.save()
        model_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
        model_iter = iter(model_dbs)

        def do_model(docs=docs):
            db = next(model_iter)
            BenchmarkItem.set_db(db, table="bench_model")
            for doc in docs:
                item = BenchmarkItem(**doc)
                item.save()

        model_stats = timer.measure(do_model)

        # SQLerLiteModel.save()
        lite_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
        lite_iter = iter(lite_dbs)

        def do_lite(docs=docs):
            db = next(lite_iter)
            BenchmarkItemLite.set_db(db, table="bench_lite")
            for doc in docs:
                item = BenchmarkItemLite(**doc)
                item.save()

        lite_stats = timer.measure(do_lite)

        # SQLite baseline — raw INSERT loop
        sqlite_conns = [sqlite3.connect(":memory:") for _ in range(config.warmup + config.iterations)]
        for c in sqlite_conns:
            create_table(c, "bench")
        sqlite_iter = iter(sqlite_conns)

        def do_sqlite(docs=docs):
            conn = next(sqlite_iter)
            insert_loop(conn, "bench", docs)

        sqlite_stats = timer.measure(do_sqlite)

        for label, stats, baseline in [
            ("sqler_raw", raw_stats, "sqler"),
            ("sqler_pydantic", model_stats, "sqler"),
            ("sqler_lite", lite_stats, "sqler"),
            ("sqlite_loop", sqlite_stats, "sqlite"),
        ]:
            throughput = row_count / (stats.median_ms / 1000) if stats.median_ms > 0 else 0
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="method", value=label,
                timing=stats, rows=row_count,
                throughput=round(throughput, 1),
                metadata={"storage_mode": "memory", "baseline": baseline},
            ))

        for c in sqlite_conns:
            c.close()

        return results

    def teardown(self) -> None:
        pass


SUITE = [BulkInsertScaling(), SingleVsBulk(), DocumentSizeImpact(), ModelOverhead()]
