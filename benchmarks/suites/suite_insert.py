"""Insert benchmark suite — scenarios 1-4."""

from __future__ import annotations

from sqler import SQLerDB

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
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
        timer = PrecisionTimer(warmup=1, iterations=config.iterations)

        for size in config.scale.bulk_sizes:
            docs = self.gen.generate("small", size)

            def do_insert(docs=docs, size=size):
                db = SQLerDB.in_memory()
                db.bulk_upsert("bench", docs)

            stats = timer.measure(do_insert)
            throughput = size / (stats.median_ms / 1000) if stats.median_ms > 0 else 0

            results.append(BenchmarkResult(
                scenario=self.name,
                suite=self.suite,
                parameter="rows",
                value=size,
                timing=stats,
                rows=size,
                throughput=round(throughput, 1),
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
        # Use medium table sizes for comparison
        sizes = [s for s in config.scale.bulk_sizes if s <= 10_000]
        if not sizes:
            sizes = [config.scale.bulk_sizes[0]]

        timer = PrecisionTimer(warmup=1, iterations=max(1, config.iterations // 2))

        for size in sizes:
            docs = self.gen.generate("small", size)

            # Bulk insert path
            def do_bulk(docs=docs):
                db = SQLerDB.in_memory()
                db.bulk_upsert("bench", docs)

            bulk_stats = timer.measure(do_bulk)

            # Single save path
            def do_single(docs=docs):
                db = SQLerDB.in_memory()
                BenchmarkItem.set_db(db, table="bench_model")
                for doc in docs:
                    item = BenchmarkItem(**doc)
                    item.save()

            single_stats = timer.measure(do_single)

            results.append(BenchmarkResult(
                scenario=self.name,
                suite=self.suite,
                parameter="method",
                value=f"bulk_{size}",
                timing=bulk_stats,
                rows=size,
                throughput=round(size / (bulk_stats.median_ms / 1000), 1) if bulk_stats.median_ms > 0 else 0,
            ))
            results.append(BenchmarkResult(
                scenario=self.name,
                suite=self.suite,
                parameter="method",
                value=f"single_{size}",
                timing=single_stats,
                rows=size,
                throughput=round(size / (single_stats.median_ms / 1000), 1) if single_stats.median_ms > 0 else 0,
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
        timer = PrecisionTimer(warmup=1, iterations=config.iterations)

        for profile in ("tiny", "small", "medium", "large", "huge"):
            docs = self.gen.generate(profile, row_count)

            def do_insert(docs=docs):
                db = SQLerDB.in_memory()
                db.bulk_upsert("bench", docs)

            stats = timer.measure(do_insert)
            throughput = row_count / (stats.median_ms / 1000) if stats.median_ms > 0 else 0

            results.append(BenchmarkResult(
                scenario=self.name,
                suite=self.suite,
                parameter="profile",
                value=profile,
                timing=stats,
                rows=row_count,
                throughput=round(throughput, 1),
                metadata={"avg_doc_bytes": len(str(docs[0]))},
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
        timer = PrecisionTimer(warmup=1, iterations=max(1, config.iterations // 2))

        docs = self.gen.generate("small", row_count)

        # Raw insert_document
        def do_raw(docs=docs):
            db = SQLerDB.in_memory()
            for doc in docs:
                db.insert_document("bench", doc)

        raw_stats = timer.measure(do_raw)

        # SQLerModel.save()
        def do_model(docs=docs):
            db = SQLerDB.in_memory()
            BenchmarkItem.set_db(db, table="bench_model")
            for doc in docs:
                item = BenchmarkItem(**doc)
                item.save()

        model_stats = timer.measure(do_model)

        # SQLerLiteModel.save()
        def do_lite(docs=docs):
            db = SQLerDB.in_memory()
            BenchmarkItemLite.set_db(db, table="bench_lite")
            for doc in docs:
                item = BenchmarkItemLite(**doc)
                item.save()

        lite_stats = timer.measure(do_lite)

        for label, stats in [("raw", raw_stats), ("pydantic", model_stats), ("lite", lite_stats)]:
            throughput = row_count / (stats.median_ms / 1000) if stats.median_ms > 0 else 0
            results.append(BenchmarkResult(
                scenario=self.name,
                suite=self.suite,
                parameter="method",
                value=label,
                timing=stats,
                rows=row_count,
                throughput=round(throughput, 1),
            ))

        return results

    def teardown(self) -> None:
        pass


SUITE = [BulkInsertScaling(), SingleVsBulk(), DocumentSizeImpact(), ModelOverhead()]
