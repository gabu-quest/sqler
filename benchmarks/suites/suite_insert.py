"""Insert benchmark suite — scenarios 1-4.

v1.2 fairness fixes:
  - create_conn() with matched PRAGMAs (H-1, M-2)
  - Deep-copy docs before each arm (H-8)
  - insert_many() for bulk baseline instead of bulk_upsert asymmetry (H-3)
  - Arm alternation per scenario (M-1)
  - gc.collect() between arms (M-4)
  - Disk mode support via --storage flag
"""

from __future__ import annotations

import gc
import json
import os
import statistics
import tempfile

from sqler import SQLerDB

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.sqlite_baseline import create_conn, create_table, insert_loop, insert_many
from benchmarks.core.timer import PrecisionTimer
from benchmarks.generators.documents import DocumentGenerator
from benchmarks.generators.models import BenchmarkItem, BenchmarkItemLite

SUITE_NAME = "insert"


def _storage_modes(config: BenchmarkConfig) -> list[str]:
    """Return list of storage modes to run based on config."""
    if config.storage == "both":
        return ["memory", "disk"]
    return [config.storage]


def _mode_prefix(mode: str) -> str:
    return "mem" if mode == "memory" else "disk"


def _alternate_arms(scenario_name: str, arms: list[tuple]) -> list[tuple]:
    """Deterministic arm order alternation based on scenario name hash.

    Eliminates systematic bias from always measuring one arm first (M-1).
    """
    if hash(scenario_name) % 2:
        return list(reversed(arms))
    return list(arms)


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

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)

            for size in config.scale.bulk_sizes:
                docs = self.gen.generate("small", size)

                with tempfile.TemporaryDirectory() as tmpdir:
                    arm_results = {}

                    def measure_sqler():
                        if mode == "memory":
                            sqler_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
                        else:
                            sqler_dbs = [
                                SQLerDB.on_disk(os.path.join(tmpdir, f"sqler_{i}.db"))
                                for i in range(config.warmup + config.iterations)
                            ]
                        sqler_iter = iter(sqler_dbs)

                        def do_insert(docs=docs):
                            db = next(sqler_iter)
                            db.bulk_upsert("bench", [d.copy() for d in docs])

                        arm_results["sqler"] = timer.measure(do_insert)

                        if mode == "disk":
                            for db in sqler_dbs:
                                db.close()

                    def measure_sqlite():
                        if mode == "memory":
                            sqlite_conns = [create_conn() for _ in range(config.warmup + config.iterations)]
                        else:
                            sqlite_conns = [
                                create_conn(os.path.join(tmpdir, f"sqlite_{i}.db"), "disk")
                                for i in range(config.warmup + config.iterations)
                            ]
                        for c in sqlite_conns:
                            create_table(c, "bench")
                        sqlite_iter = iter(sqlite_conns)

                        def do_sqlite(docs=docs):
                            conn = next(sqlite_iter)
                            insert_many(conn, "bench", [d.copy() for d in docs])

                        arm_results["sqlite"] = timer.measure(do_sqlite)

                        for c in sqlite_conns:
                            c.close()

                    arms = _alternate_arms(
                        f"{self.name}_{prefix}_{size}",
                        [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                    )
                    for label, fn in arms:
                        fn()
                        gc.collect()

                    for arm_label, baseline in [("sqler", "sqler"), ("sqlite", "sqlite")]:
                        stats = arm_results[arm_label]
                        tp = size / (stats.median_ms / 1000) if stats.median_ms > 0 else 0
                        results.append(BenchmarkResult(
                            scenario=self.name, suite=self.suite,
                            parameter="rows", value=f"{arm_label}_{prefix}_{size}",
                            timing=stats, rows=size,
                            throughput=round(tp, 1),
                            metadata={"storage_mode": mode, "baseline": baseline},
                        ))

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

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)

            for size in sizes:
                docs = self.gen.generate("small", size)

                with tempfile.TemporaryDirectory() as tmpdir:
                    arm_results = {}

                    def measure_sqler_arms():
                        # sqler bulk insert
                        if mode == "memory":
                            sqler_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
                        else:
                            sqler_dbs = [
                                SQLerDB.on_disk(os.path.join(tmpdir, f"bulk_{i}.db"))
                                for i in range(config.warmup + config.iterations)
                            ]
                        sqler_iter = iter(sqler_dbs)

                        def do_bulk(docs=docs):
                            db = next(sqler_iter)
                            db.bulk_upsert("bench", [d.copy() for d in docs])

                        arm_results["sqler_bulk"] = timer.measure(do_bulk)

                        gc.collect()

                        # sqler single save
                        if mode == "memory":
                            single_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
                        else:
                            single_dbs = [
                                SQLerDB.on_disk(os.path.join(tmpdir, f"single_{i}.db"))
                                for i in range(config.warmup + config.iterations)
                            ]
                        single_iter = iter(single_dbs)

                        def do_single(docs=docs):
                            db = next(single_iter)
                            BenchmarkItem.set_db(db, table="bench_model")
                            for doc in docs:
                                item = BenchmarkItem(**doc.copy())
                                item.save()

                        arm_results["sqler_single"] = timer.measure(do_single)

                    def measure_sqlite_arms():
                        # SQLite batch
                        if mode == "memory":
                            batch_conns = [create_conn() for _ in range(config.warmup + config.iterations)]
                        else:
                            batch_conns = [
                                create_conn(os.path.join(tmpdir, f"batch_{i}.db"), "disk")
                                for i in range(config.warmup + config.iterations)
                            ]
                        for c in batch_conns:
                            create_table(c, "bench")
                        batch_iter = iter(batch_conns)

                        def do_sqlite_batch(docs=docs):
                            conn = next(batch_iter)
                            insert_many(conn, "bench", [d.copy() for d in docs])

                        arm_results["sqlite_batch"] = timer.measure(do_sqlite_batch)

                        gc.collect()

                        # SQLite loop
                        if mode == "memory":
                            loop_conns = [create_conn() for _ in range(config.warmup + config.iterations)]
                        else:
                            loop_conns = [
                                create_conn(os.path.join(tmpdir, f"loop_{i}.db"), "disk")
                                for i in range(config.warmup + config.iterations)
                            ]
                        for c in loop_conns:
                            create_table(c, "bench")
                        loop_iter = iter(loop_conns)

                        def do_sqlite_loop(docs=docs):
                            conn = next(loop_iter)
                            insert_loop(conn, "bench", [d.copy() for d in docs])

                        arm_results["sqlite_loop"] = timer.measure(do_sqlite_loop)

                        for c in batch_conns:
                            c.close()
                        for c in loop_conns:
                            c.close()

                    arms = _alternate_arms(
                        f"{self.name}_{prefix}_{size}",
                        [("sqler", measure_sqler_arms), ("sqlite", measure_sqlite_arms)],
                    )
                    for label, fn in arms:
                        fn()
                        gc.collect()

                    for label, stats_key, baseline in [
                        (f"sqler_{prefix}_bulk_{size}", "sqler_bulk", "sqler"),
                        (f"sqler_{prefix}_single_{size}", "sqler_single", "sqler"),
                        (f"sqlite_{prefix}_batch_{size}", "sqlite_batch", "sqlite"),
                        (f"sqlite_{prefix}_loop_{size}", "sqlite_loop", "sqlite"),
                    ]:
                        stats = arm_results[stats_key]
                        tp = size / (stats.median_ms / 1000) if stats.median_ms > 0 else 0
                        results.append(BenchmarkResult(
                            scenario=self.name, suite=self.suite,
                            parameter="method", value=label,
                            timing=stats, rows=size,
                            throughput=round(tp, 1),
                            metadata={"storage_mode": mode, "baseline": baseline},
                        ))

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

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)

            for profile in ("tiny", "small", "medium", "large", "huge"):
                docs = self.gen.generate(profile, row_count)
                avg_doc_bytes = round(statistics.mean(len(json.dumps(d)) for d in docs))

                with tempfile.TemporaryDirectory() as tmpdir:
                    arm_results = {}

                    def measure_sqler():
                        if mode == "memory":
                            sqler_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
                        else:
                            sqler_dbs = [
                                SQLerDB.on_disk(os.path.join(tmpdir, f"sqler_{i}.db"))
                                for i in range(config.warmup + config.iterations)
                            ]
                        sqler_iter = iter(sqler_dbs)

                        def do_insert(docs=docs):
                            db = next(sqler_iter)
                            db.bulk_upsert("bench", [d.copy() for d in docs])

                        arm_results["sqler"] = timer.measure(do_insert)

                    def measure_sqlite():
                        if mode == "memory":
                            sqlite_conns = [create_conn() for _ in range(config.warmup + config.iterations)]
                        else:
                            sqlite_conns = [
                                create_conn(os.path.join(tmpdir, f"sqlite_{i}.db"), "disk")
                                for i in range(config.warmup + config.iterations)
                            ]
                        for c in sqlite_conns:
                            create_table(c, "bench")
                        sqlite_iter = iter(sqlite_conns)

                        def do_sqlite(docs=docs):
                            conn = next(sqlite_iter)
                            insert_many(conn, "bench", [d.copy() for d in docs])

                        arm_results["sqlite"] = timer.measure(do_sqlite)

                        for c in sqlite_conns:
                            c.close()

                    arms = _alternate_arms(
                        f"{self.name}_{prefix}_{profile}",
                        [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                    )
                    for label, fn in arms:
                        fn()
                        gc.collect()

                    for arm_label, baseline in [("sqler", "sqler"), ("sqlite", "sqlite")]:
                        stats = arm_results[arm_label]
                        tp = row_count / (stats.median_ms / 1000) if stats.median_ms > 0 else 0
                        results.append(BenchmarkResult(
                            scenario=self.name, suite=self.suite,
                            parameter="profile", value=f"{arm_label}_{prefix}_{profile}",
                            timing=stats, rows=row_count,
                            throughput=round(tp, 1),
                            metadata={"avg_doc_bytes": avg_doc_bytes, "storage_mode": mode, "baseline": baseline},
                        ))

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

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)
            docs = self.gen.generate("small", row_count)

            with tempfile.TemporaryDirectory() as tmpdir:
                arm_results = {}

                def measure_sqler_arms():
                    # Raw insert_document
                    if mode == "memory":
                        raw_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
                    else:
                        raw_dbs = [
                            SQLerDB.on_disk(os.path.join(tmpdir, f"raw_{i}.db"))
                            for i in range(config.warmup + config.iterations)
                        ]
                    raw_iter = iter(raw_dbs)

                    def do_raw(docs=docs):
                        db = next(raw_iter)
                        for doc in docs:
                            db.insert_document("bench", doc.copy())

                    arm_results["sqler_raw"] = timer.measure(do_raw)

                    gc.collect()

                    # SQLerModel.save()
                    if mode == "memory":
                        model_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
                    else:
                        model_dbs = [
                            SQLerDB.on_disk(os.path.join(tmpdir, f"model_{i}.db"))
                            for i in range(config.warmup + config.iterations)
                        ]
                    model_iter = iter(model_dbs)

                    def do_model(docs=docs):
                        db = next(model_iter)
                        BenchmarkItem.set_db(db, table="bench_model")
                        for doc in docs:
                            item = BenchmarkItem(**doc.copy())
                            item.save()

                    arm_results["sqler_pydantic"] = timer.measure(do_model)

                    gc.collect()

                    # SQLerLiteModel.save()
                    if mode == "memory":
                        lite_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
                    else:
                        lite_dbs = [
                            SQLerDB.on_disk(os.path.join(tmpdir, f"lite_{i}.db"))
                            for i in range(config.warmup + config.iterations)
                        ]
                    lite_iter = iter(lite_dbs)

                    def do_lite(docs=docs):
                        db = next(lite_iter)
                        BenchmarkItemLite.set_db(db, table="bench_lite")
                        for doc in docs:
                            item = BenchmarkItemLite(**doc.copy())
                            item.save()

                    arm_results["sqler_lite"] = timer.measure(do_lite)

                def measure_sqlite():
                    if mode == "memory":
                        sqlite_conns = [create_conn() for _ in range(config.warmup + config.iterations)]
                    else:
                        sqlite_conns = [
                            create_conn(os.path.join(tmpdir, f"sqlite_{i}.db"), "disk")
                            for i in range(config.warmup + config.iterations)
                        ]
                    for c in sqlite_conns:
                        create_table(c, "bench")
                    sqlite_iter = iter(sqlite_conns)

                    def do_sqlite(docs=docs):
                        conn = next(sqlite_iter)
                        insert_loop(conn, "bench", [d.copy() for d in docs])

                    arm_results["sqlite_loop"] = timer.measure(do_sqlite)

                    for c in sqlite_conns:
                        c.close()

                arms = _alternate_arms(
                    f"{self.name}_{prefix}",
                    [("sqler", measure_sqler_arms), ("sqlite", measure_sqlite)],
                )
                for label, fn in arms:
                    fn()
                    gc.collect()

                for label, stats_key, baseline in [
                    (f"sqler_{prefix}_raw", "sqler_raw", "sqler"),
                    (f"sqler_{prefix}_pydantic", "sqler_pydantic", "sqler"),
                    (f"sqler_{prefix}_lite", "sqler_lite", "sqler"),
                    (f"sqlite_{prefix}_loop", "sqlite_loop", "sqlite"),
                ]:
                    stats = arm_results[stats_key]
                    tp = row_count / (stats.median_ms / 1000) if stats.median_ms > 0 else 0
                    results.append(BenchmarkResult(
                        scenario=self.name, suite=self.suite,
                        parameter="method", value=label,
                        timing=stats, rows=row_count,
                        throughput=round(tp, 1),
                        metadata={"storage_mode": mode, "baseline": baseline},
                    ))

        return results

    def teardown(self) -> None:
        pass


SUITE = [BulkInsertScaling(), SingleVsBulk(), DocumentSizeImpact(), ModelOverhead()]
