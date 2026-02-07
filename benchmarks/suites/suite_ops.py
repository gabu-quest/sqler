"""Operational benchmark suite — scenarios 18-22."""

from __future__ import annotations

import os
import tempfile

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.timer import PrecisionTimer
from benchmarks.generators.documents import DocumentGenerator
from benchmarks.generators.models import BenchmarkItem

from sqler import F, SQLerDB, backup, export_csv, export_json, export_jsonl, restore

SUITE_NAME = "ops"


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

            # Fresh db each time to measure from scratch
            def do_create_index(docs=docs, size=size):
                db = SQLerDB.in_memory()
                db.bulk_upsert("bench", docs)
                db.create_index("bench", "value")

            timer = PrecisionTimer(warmup=1, iterations=config.iterations)
            stats = timer.measure(do_create_index)

            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="rows", value=size,
                timing=stats, rows=size,
            ))

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
        import time

        results = []
        size = config.scale.table_sizes[-1]
        docs = self.gen.generate("small", size)

        cold_times = []
        warm_times = []

        for _ in range(config.iterations):
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                db_path = f.name

            # Populate
            db = SQLerDB.on_disk(db_path)
            db.bulk_upsert("bench", docs)
            db.close()

            # Cold: first query on fresh connection
            db2 = SQLerDB.on_disk(db_path)
            start = time.perf_counter()
            db2.query("bench").filter(F("value") > 5000).limit(10).all()
            cold_times.append((time.perf_counter() - start) * 1000)

            # Warm: 10th query
            for _ in range(9):
                db2.query("bench").filter(F("value") > 5000).limit(10).all()
            start = time.perf_counter()
            db2.query("bench").filter(F("value") > 5000).limit(10).all()
            warm_times.append((time.perf_counter() - start) * 1000)

            db2.close()
            os.unlink(db_path)

        import statistics

        for label, times in [("cold", cold_times), ("warm", warm_times)]:
            sorted_t = sorted(times)
            stats_obj = _timing_stats_from_list(sorted_t)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="state", value=label,
                timing=stats_obj, rows=size,
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
        timer = PrecisionTimer(warmup=1, iterations=config.iterations)

        export_sizes = [s for s in config.scale.table_sizes if s <= 50_000]
        if not export_sizes:
            export_sizes = [config.scale.table_sizes[0]]

        for size in export_sizes:
            docs = self.gen.generate("small", size)
            db = SQLerDB.in_memory()
            BenchmarkItem.set_db(db, table="bench")
            db.bulk_upsert("bench", docs)

            # Use queryset and model class for export APIs
            qs = BenchmarkItem.query()

            with tempfile.TemporaryDirectory() as tmpdir:
                # CSV (takes queryset or model class)
                def do_csv(qs=qs, tmpdir=tmpdir):
                    export_csv(qs, os.path.join(tmpdir, "out.csv"))

                csv_stats = timer.measure(do_csv)

                # JSON (takes model class)
                def do_json(tmpdir=tmpdir):
                    export_json(BenchmarkItem, os.path.join(tmpdir, "out.json"))

                json_stats = timer.measure(do_json)

                # JSONL (takes queryset or model class)
                def do_jsonl(qs=qs, tmpdir=tmpdir):
                    export_jsonl(qs, os.path.join(tmpdir, "out.jsonl"))

                jsonl_stats = timer.measure(do_jsonl)

            for label, stats in [
                (f"csv_{size}", csv_stats),
                (f"json_{size}", json_stats),
                (f"jsonl_{size}", jsonl_stats),
            ]:
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="format", value=label,
                    timing=stats, rows=size,
                ))

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

                timer = PrecisionTimer(warmup=1, iterations=config.iterations)

                # Backup: backup(db, destination)
                def do_backup(db=db, bak=bak_path):
                    if os.path.exists(bak):
                        os.unlink(bak)
                    backup(db, bak)

                backup_stats = timer.measure(do_backup)

                # Restore: restore(target_db, source_path)
                def do_restore(bak=bak_path, rst=rst_path):
                    if os.path.exists(rst):
                        os.unlink(rst)
                    target_db = SQLerDB.on_disk(rst)
                    restore(target_db, bak)

                restore_stats = timer.measure(do_restore)

                for label, stats in [
                    (f"backup_{size}", backup_stats),
                    (f"restore_{size}", restore_stats),
                ]:
                    results.append(BenchmarkResult(
                        scenario=self.name, suite=self.suite,
                        parameter="operation", value=label,
                        timing=stats, rows=size,
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

            for agg_name in ("sum", "avg", "min", "max"):
                def do_agg(db=db, agg=agg_name):
                    return getattr(db.query("bench"), agg)("value")

                stats = timer.measure(do_agg)
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="aggregate", value=f"{agg_name}_{size}",
                    timing=stats, rows=size,
                ))

        return results

    def teardown(self) -> None:
        pass


def _timing_stats_from_list(sorted_times: list[float]):
    """Helper to build TimingStats from a sorted list of ms values."""
    import statistics as st

    from benchmarks.core.base import TimingStats
    from benchmarks.core.timer import PrecisionTimer

    n = len(sorted_times)
    return TimingStats(
        iterations=n,
        min_ms=round(sorted_times[0], 4),
        max_ms=round(sorted_times[-1], 4),
        mean_ms=round(st.mean(sorted_times), 4),
        median_ms=round(st.median(sorted_times), 4),
        p95_ms=round(PrecisionTimer._percentile(sorted_times, 0.95), 4),
        p99_ms=round(PrecisionTimer._percentile(sorted_times, 0.99), 4),
        stddev_ms=round(st.stdev(sorted_times), 4) if n > 1 else 0,
        total_ms=round(sum(sorted_times), 4),
    )


SUITE = [
    IndexCreationTime(),
    ColdVsWarm(),
    ExportPerformance(),
    BackupRestore(),
    AggregatePerformance(),
]
