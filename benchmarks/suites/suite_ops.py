"""Operational benchmark suite — scenarios 18-22."""

from __future__ import annotations

import math
import os
import sqlite3
import statistics
import tempfile
import time

from sqler import F, SQLerDB, backup, export_csv, export_json, export_jsonl, restore

from benchmarks.core.base import BenchmarkResult, TimingStats
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.sqlite_baseline import create_table, insert_many
from benchmarks.core.timer import PrecisionTimer
from benchmarks.generators.documents import DocumentGenerator
from benchmarks.generators.models import BenchmarkItem

SUITE_NAME = "ops"


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


class IndexCreationTime:
    """Scenario 18: create_index() on varying table sizes."""

    name = "index_creation"
    suite = SUITE_NAME
    description = "db.create_index('field') on tables of 10K/50K/100K rows"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []

        for size in config.scale.table_sizes:
            docs = self.gen.generate("small", size)

            # sqler — pre-populate outside closure, only time create_index
            sqler_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
            for sdb in sqler_dbs:
                sdb.bulk_upsert("bench", docs)
            sqler_iter = iter(sqler_dbs)

            def do_create_index():
                db = next(sqler_iter)
                db.create_index("bench", "value")

            timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)
            stats = timer.measure(do_create_index)

            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="rows", value=f"sqler_{size}",
                timing=stats, rows=size,
                metadata={"storage_mode": "memory", "baseline": "sqler"},
            ))

            # SQLite baseline — pre-populate, only time CREATE INDEX
            sqlite_conns = [sqlite3.connect(":memory:") for _ in range(config.warmup + config.iterations)]
            for c in sqlite_conns:
                create_table(c, "bench")
                insert_many(c, "bench", docs)
            sqlite_iter = iter(sqlite_conns)

            def do_sqlite_index():
                conn = next(sqlite_iter)
                conn.execute(
                    "CREATE INDEX [idx_bench_value] ON [bench] "
                    "(json_extract(data, '$.value'))"
                )
                conn.commit()

            sqlite_stats = timer.measure(do_sqlite_index)

            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="rows", value=f"sqlite_{size}",
                timing=sqlite_stats, rows=size,
                metadata={"storage_mode": "memory", "baseline": "sqlite"},
            ))

            for c in sqlite_conns:
                c.close()

        return results

    def teardown(self) -> None:
        pass


class ColdVsWarm:
    """Scenario 19: First query after open vs 10th query."""

    name = "cold_vs_warm"
    suite = SUITE_NAME
    description = "First query after db open vs 10th query (SQLite page cache)"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        size = config.scale.table_sizes[-1]
        docs = self.gen.generate("small", size)

        cold_times: list[float] = []
        warm_times: list[float] = []
        sqlite_cold_times: list[float] = []
        sqlite_warm_times: list[float] = []

        for _ in range(config.iterations):
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = f.name

            # Populate
            db = SQLerDB.on_disk(db_path)
            db.bulk_upsert("bench", docs)
            db.close()

            # sqler cold: first query on fresh connection
            db2 = SQLerDB.on_disk(db_path)
            start = time.perf_counter()
            db2.query("bench").filter(F("value") > 5000).limit(10).all()
            cold_times.append((time.perf_counter() - start) * 1000)

            # sqler warm: 10th query
            for _ in range(9):
                db2.query("bench").filter(F("value") > 5000).limit(10).all()
            start = time.perf_counter()
            db2.query("bench").filter(F("value") > 5000).limit(10).all()
            warm_times.append((time.perf_counter() - start) * 1000)
            db2.close()

            # SQLite cold
            conn = sqlite3.connect(db_path)
            start = time.perf_counter()
            conn.execute(
                "SELECT data FROM [bench] WHERE json_extract(data, '$.value') > ? LIMIT 10",
                (5000,),
            ).fetchall()
            sqlite_cold_times.append((time.perf_counter() - start) * 1000)

            # SQLite warm
            for _ in range(9):
                conn.execute(
                    "SELECT data FROM [bench] WHERE json_extract(data, '$.value') > ? LIMIT 10",
                    (5000,),
                ).fetchall()
            start = time.perf_counter()
            conn.execute(
                "SELECT data FROM [bench] WHERE json_extract(data, '$.value') > ? LIMIT 10",
                (5000,),
            ).fetchall()
            sqlite_warm_times.append((time.perf_counter() - start) * 1000)
            conn.close()

            os.unlink(db_path)

        for label, times, baseline in [
            ("sqler_cold", cold_times, "sqler"),
            ("sqler_warm", warm_times, "sqler"),
            ("sqlite_cold", sqlite_cold_times, "sqlite"),
            ("sqlite_warm", sqlite_warm_times, "sqlite"),
        ]:
            sorted_t = sorted(times)
            stats_obj = _timing_stats_from_list(sorted_t)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="state", value=label,
                timing=stats_obj, rows=size,
                metadata={"storage_mode": "disk", "baseline": baseline},
            ))

        return results

    def teardown(self) -> None:
        pass


class ExportPerformance:
    """Scenario 20: export_csv vs export_json vs export_jsonl at various sizes."""

    name = "export_performance"
    suite = SUITE_NAME
    description = "export_csv vs export_json vs export_jsonl at 10K/50K rows"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        export_sizes = [s for s in config.scale.table_sizes if s <= 50_000]
        if not export_sizes:
            export_sizes = [config.scale.table_sizes[0]]

        for size in export_sizes:
            docs = self.gen.generate("small", size)
            db = SQLerDB.in_memory()
            BenchmarkItem.set_db(db, table="bench")
            db.bulk_upsert("bench", docs)

            qs = BenchmarkItem.query()

            # Also prepare sqlite mirror
            conn = sqlite3.connect(":memory:")
            create_table(conn, "bench")
            insert_many(conn, "bench", docs)

            with tempfile.TemporaryDirectory() as tmpdir:
                # sqler CSV
                def do_csv(qs=qs, tmpdir=tmpdir):
                    export_csv(qs, os.path.join(tmpdir, "out.csv"))

                csv_stats = timer.measure(do_csv)

                # sqler JSON
                def do_json(tmpdir=tmpdir):
                    export_json(BenchmarkItem, os.path.join(tmpdir, "out.json"))

                json_stats = timer.measure(do_json)

                # sqler JSONL
                def do_jsonl(qs=qs, tmpdir=tmpdir):
                    export_jsonl(qs, os.path.join(tmpdir, "out.jsonl"))

                jsonl_stats = timer.measure(do_jsonl)

                # SQLite baseline: fetchall + csv.writer
                import csv
                import json as json_mod

                def do_sqlite_csv(conn=conn, tmpdir=tmpdir):
                    rows = conn.execute("SELECT data FROM [bench]").fetchall()
                    parsed = [json_mod.loads(r[0]) for r in rows]
                    if parsed:
                        with open(os.path.join(tmpdir, "sq_out.csv"), "w", newline="") as f:
                            w = csv.DictWriter(f, fieldnames=parsed[0].keys())
                            w.writeheader()
                            w.writerows(parsed)

                sqlite_csv_stats = timer.measure(do_sqlite_csv)

                # SQLite baseline: fetchall + json.dumps
                def do_sqlite_jsonl(conn=conn, tmpdir=tmpdir):
                    rows = conn.execute("SELECT data FROM [bench]").fetchall()
                    with open(os.path.join(tmpdir, "sq_out.jsonl"), "w") as f:
                        for r in rows:
                            f.write(r[0] + "\n")

                sqlite_jsonl_stats = timer.measure(do_sqlite_jsonl)

            for label, stats, baseline in [
                (f"sqler_csv_{size}", csv_stats, "sqler"),
                (f"sqler_json_{size}", json_stats, "sqler"),
                (f"sqler_jsonl_{size}", jsonl_stats, "sqler"),
                (f"sqlite_csv_{size}", sqlite_csv_stats, "sqlite"),
                (f"sqlite_jsonl_{size}", sqlite_jsonl_stats, "sqlite"),
            ]:
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="format", value=label,
                    timing=stats, rows=size,
                    metadata={"storage_mode": "memory", "baseline": baseline},
                ))

            conn.close()

        return results

    def teardown(self) -> None:
        pass


class BackupRestore:
    """Scenario 21: backup() and restore() at various database sizes."""

    name = "backup_restore"
    suite = SUITE_NAME
    description = "backup(db, path) and restore(path, db) at varying sizes"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []

        for size in config.scale.table_sizes:
            docs = self.gen.generate("small", size)

            with tempfile.TemporaryDirectory() as tmpdir:
                src_path = os.path.join(tmpdir, "src.db")
                bak_path = os.path.join(tmpdir, "bak.db")
                rst_path = os.path.join(tmpdir, "rst.db")

                db = SQLerDB.on_disk(src_path)
                db.bulk_upsert("bench", docs)

                timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

                # sqler backup
                def do_backup(db=db, bak=bak_path):
                    if os.path.exists(bak):
                        os.unlink(bak)
                    backup(db, bak)

                backup_stats = timer.measure(do_backup)

                # sqler restore
                def do_restore(bak=bak_path, rst=rst_path):
                    if os.path.exists(rst):
                        os.unlink(rst)
                    target_db = SQLerDB.on_disk(rst)
                    restore(target_db, bak)

                restore_stats = timer.measure(do_restore)

                # SQLite baseline: conn.backup()
                sqlite_src = sqlite3.connect(src_path)
                sq_bak_path = os.path.join(tmpdir, "sq_bak.db")
                sq_rst_path = os.path.join(tmpdir, "sq_rst.db")

                def do_sqlite_backup(src=sqlite_src, bak=sq_bak_path):
                    if os.path.exists(bak):
                        os.unlink(bak)
                    dest = sqlite3.connect(bak)
                    src.backup(dest)
                    dest.close()

                sqlite_backup_stats = timer.measure(do_sqlite_backup)

                def do_sqlite_restore(bak=sq_bak_path, rst=sq_rst_path):
                    if os.path.exists(rst):
                        os.unlink(rst)
                    src_conn = sqlite3.connect(bak)
                    dest_conn = sqlite3.connect(rst)
                    src_conn.backup(dest_conn)
                    dest_conn.close()
                    src_conn.close()

                sqlite_restore_stats = timer.measure(do_sqlite_restore)

                sqlite_src.close()

                for label, stats, baseline in [
                    (f"sqler_backup_{size}", backup_stats, "sqler"),
                    (f"sqler_restore_{size}", restore_stats, "sqler"),
                    (f"sqlite_backup_{size}", sqlite_backup_stats, "sqlite"),
                    (f"sqlite_restore_{size}", sqlite_restore_stats, "sqlite"),
                ]:
                    results.append(BenchmarkResult(
                        scenario=self.name, suite=self.suite,
                        parameter="operation", value=label,
                        timing=stats, rows=size,
                        metadata={"storage_mode": "disk", "baseline": baseline},
                    ))

        return results

    def teardown(self) -> None:
        pass


class AggregatePerformance:
    """Scenario 22: sum/avg/min/max aggregates at scale."""

    name = "aggregates"
    suite = SUITE_NAME
    description = "query.sum/avg/min/max('value') at varying table sizes"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for size in config.scale.table_sizes:
            docs = self.gen.generate("small", size)
            db = SQLerDB.in_memory()
            db.bulk_upsert("bench", docs)

            conn = sqlite3.connect(":memory:")
            create_table(conn, "bench")
            insert_many(conn, "bench", docs)

            for agg_name in ("sum", "avg", "min", "max"):
                # sqler
                def do_agg(db=db, agg=agg_name):
                    return getattr(db.query("bench"), agg)("value")

                stats = timer.measure(do_agg)
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="aggregate", value=f"sqler_{agg_name}_{size}",
                    timing=stats, rows=size,
                    metadata={"storage_mode": "memory", "baseline": "sqler"},
                ))

                # SQLite baseline
                def do_sqlite_agg(conn=conn, agg=agg_name.upper()):
                    return conn.execute(
                        f"SELECT {agg}(json_extract(data, '$.value')) FROM [bench]"
                    ).fetchone()[0]

                sqlite_stats = timer.measure(do_sqlite_agg)
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="aggregate", value=f"sqlite_{agg_name}_{size}",
                    timing=sqlite_stats, rows=size,
                    metadata={"storage_mode": "memory", "baseline": "sqlite"},
                ))

            conn.close()

        return results

    def teardown(self) -> None:
        pass


SUITE = [
    IndexCreationTime(),
    ColdVsWarm(),
    ExportPerformance(),
    BackupRestore(),
    AggregatePerformance(),
]
