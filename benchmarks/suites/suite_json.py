"""JSON-specific benchmark suite — scenarios 11-13."""

from __future__ import annotations

import sqlite3

from sqler import F, SQLerDB

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.sqlite_baseline import (
    any_where_eq,
    any_where_gt,
    array_contains,
    array_isin,
    create_table,
    insert_many,
    query_nested,
)
from benchmarks.core.timer import PrecisionTimer
from benchmarks.generators.documents import DocumentGenerator

SUITE_NAME = "json"


def _setup_sqlite_mirror(docs: list[dict]) -> sqlite3.Connection:
    """Create an in-memory sqlite3 conn with same data for baseline."""
    conn = sqlite3.connect(":memory:")
    create_table(conn, "bench")
    insert_many(conn, "bench", docs)
    return conn


class NestedFieldAccess:
    """Scenario 11: Nested JSON field access at depth 1/2/3."""

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

            conn = _setup_sqlite_mirror(docs)

            # Build consistent predicate: all depths query target > 5000
            if depth == 1:
                # depth=1: doc has level_0.target
                def do_query(db=db):
                    return db.query("bench").filter(
                        F("level_0")["target"] > 5000
                    ).all()

                json_path = "$.level_0.target"
            elif depth == 2:
                # depth=2: doc has level_0.level_1.target
                def do_query(db=db):
                    return db.query("bench").filter(
                        F("level_0")["level_1"]["target"] > 5000
                    ).all()

                json_path = "$.level_0.level_1.target"
            else:
                # depth=3: doc has level_0.level_1.level_2.target
                def do_query(db=db):
                    return db.query("bench").filter(
                        F("level_0")["level_1"]["level_2"]["target"] > 5000
                    ).all()

                json_path = "$.level_0.level_1.level_2.target"

            stats = timer.measure(do_query)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="depth", value=f"sqler_{depth}",
                timing=stats, rows=size,
                metadata={"storage_mode": "memory", "baseline": "sqler"},
            ))

            # SQLite baseline
            def do_sqlite(conn=conn, jp=json_path):
                return query_nested(conn, "bench", jp, ">", 5000)

            sqlite_stats = timer.measure(do_sqlite)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="depth", value=f"sqlite_{depth}",
                timing=sqlite_stats, rows=size,
                metadata={"storage_mode": "memory", "baseline": "sqlite"},
            ))

            conn.close()

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

            conn = _setup_sqlite_mirror(docs)

            # sqler .contains()
            def do_contains(db=db):
                return db.query("bench").filter(F("tags").contains("alpha")).all()

            contains_stats = timer.measure(do_contains)

            # sqler .isin()
            def do_isin(db=db):
                return db.query("bench").filter(
                    F("tags").isin(["alpha", "bravo", "charlie"])
                ).all()

            isin_stats = timer.measure(do_isin)

            # SQLite baselines
            def do_sqlite_contains(conn=conn):
                return array_contains(conn, "bench", "tags", "alpha")

            def do_sqlite_isin(conn=conn):
                return array_isin(conn, "bench", "tags", ["alpha", "bravo", "charlie"])

            sqlite_contains_stats = timer.measure(do_sqlite_contains)
            sqlite_isin_stats = timer.measure(do_sqlite_isin)

            for label, stats, baseline in [
                (f"sqler_contains_{size}", contains_stats, "sqler"),
                (f"sqler_isin_{size}", isin_stats, "sqler"),
                (f"sqlite_contains_{size}", sqlite_contains_stats, "sqlite"),
                (f"sqlite_isin_{size}", sqlite_isin_stats, "sqlite"),
            ]:
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="op", value=label,
                    timing=stats, rows=size,
                    metadata={"storage_mode": "memory", "baseline": baseline},
                ))

            conn.close()

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

            conn = _setup_sqlite_mirror(docs)

            # sqler any().where() with equality
            def do_any_eq(db=db):
                return db.query("bench").filter(
                    F(["events"]).any().where(F("type") == "purchase")
                ).all()

            eq_stats = timer.measure(do_any_eq)

            # sqler any().where() with comparison
            def do_any_gt(db=db):
                return db.query("bench").filter(
                    F(["events"]).any().where(F("amount") > 250)
                ).all()

            gt_stats = timer.measure(do_any_gt)

            # SQLite baselines
            def do_sqlite_eq(conn=conn):
                return any_where_eq(conn, "bench", "events", "type", "purchase")

            def do_sqlite_gt(conn=conn):
                return any_where_gt(conn, "bench", "events", "amount", 250)

            sqlite_eq_stats = timer.measure(do_sqlite_eq)
            sqlite_gt_stats = timer.measure(do_sqlite_gt)

            for label, stats, baseline in [
                (f"sqler_eq_{size}", eq_stats, "sqler"),
                (f"sqler_gt_{size}", gt_stats, "sqler"),
                (f"sqlite_eq_{size}", sqlite_eq_stats, "sqlite"),
                (f"sqlite_gt_{size}", sqlite_gt_stats, "sqlite"),
            ]:
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="table_size", value=label,
                    timing=stats, rows=size,
                    metadata={"storage_mode": "memory", "baseline": baseline},
                ))

            conn.close()

        return results

    def teardown(self) -> None:
        pass


SUITE = [NestedFieldAccess(), ArrayContainsIsin(), AnyWhere()]
