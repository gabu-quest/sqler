"""JSON-specific benchmark suite — scenarios 11-13."""

from __future__ import annotations

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.timer import PrecisionTimer
from benchmarks.generators.documents import DocumentGenerator

from sqler import F, SQLerDB

SUITE_NAME = "json"


class NestedFieldAccess:
    """Scenario 11: F('x')['y'] at depth 1, F('x')['y']['z'] at depth 2, etc."""

    name = "nested_field_access"
    suite = SUITE_NAME
    description = "Query nested JSON fields at increasing depths"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)
        size = config.scale.table_sizes[-1]

        for depth in (1, 2, 3):
            docs = self.gen.generate_nested(size, depth=depth)
            db = SQLerDB.in_memory()
            db.bulk_upsert("bench", docs)

            # Build nested field access path
            if depth == 1:
                def do_query(db=db):
                    return db.query("bench").filter(F("level_0") != None).all()
            elif depth == 2:
                def do_query(db=db):
                    return db.query("bench").filter(
                        F("level_0")["level_1"] != None
                    ).all()
            else:
                def do_query(db=db):
                    return db.query("bench").filter(
                        F("level_0")["level_1"]["target"] > 5000
                    ).all()

            stats = timer.measure(do_query)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="depth", value=depth,
                timing=stats, rows=size,
            ))

        return results

    def teardown(self) -> None:
        pass


class ArrayContainsIsin:
    """Scenario 12: F('tags').contains() and F('tags').isin() vs table size."""

    name = "array_contains_isin"
    suite = SUITE_NAME
    description = "F('tags').contains(x) and F('tags').isin([...]) at varying sizes"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for size in config.scale.table_sizes:
            docs = self.gen.generate_with_events(size)
            db = SQLerDB.in_memory()
            db.bulk_upsert("bench", docs)

            # .contains()
            def do_contains(db=db):
                return db.query("bench").filter(F("tags").contains("alpha")).all()

            contains_stats = timer.measure(do_contains)

            # .isin()
            def do_isin(db=db):
                return db.query("bench").filter(
                    F("tags").isin(["alpha", "bravo", "charlie"])
                ).all()

            isin_stats = timer.measure(do_isin)

            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="op", value=f"contains_{size}",
                timing=contains_stats, rows=size,
            ))
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="op", value=f"isin_{size}",
                timing=isin_stats, rows=size,
            ))

        return results

    def teardown(self) -> None:
        pass


class AnyWhere:
    """Scenario 13: F(['events']).any().where(F('type') == 'a') vs table size."""

    name = "any_where"
    suite = SUITE_NAME
    description = "F(['events']).any().where(condition) on arrays of objects"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for size in config.scale.table_sizes:
            docs = self.gen.generate_with_events(size, events_per_doc=5)
            db = SQLerDB.in_memory()
            db.bulk_upsert("bench", docs)

            # any().where() with equality
            def do_any_eq(db=db):
                return db.query("bench").filter(
                    F(["events"]).any().where(F("type") == "purchase")
                ).all()

            eq_stats = timer.measure(do_any_eq)

            # any().where() with comparison
            def do_any_gt(db=db):
                return db.query("bench").filter(
                    F(["events"]).any().where(F("amount") > 250)
                ).all()

            gt_stats = timer.measure(do_any_gt)

            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="table_size", value=f"eq_{size}",
                timing=eq_stats, rows=size,
            ))
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="table_size", value=f"gt_{size}",
                timing=gt_stats, rows=size,
            ))

        return results

    def teardown(self) -> None:
        pass


SUITE = [NestedFieldAccess(), ArrayContainsIsin(), AnyWhere()]
