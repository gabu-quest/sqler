"""JSON-specific benchmark suite — scenarios 11-13.

v1.2 fairness fixes:
  - create_conn() with matched PRAGMAs (H-1, M-2)
  - Fixed array SQL: json_each(data, '$.path') not json_each(json_extract(...)) (H-2)
  - All baselines return raw JSON strings (fetch_as_strings), matching db.query().all()
  - Arm alternation per scenario (M-1)
  - gc.collect() between arms (M-4)
  - Disk mode support via --storage flag
"""

from __future__ import annotations

import gc
import os
import tempfile

from sqler import F, SQLerDB

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.sqlite_baseline import (
    any_where_eq,
    any_where_gt,
    array_contains,
    array_isin,
    create_conn,
    create_table,
    insert_many,
    query_nested,
)
from benchmarks.core.timer import PrecisionTimer
from benchmarks.generators.documents import DocumentGenerator

SUITE_NAME = "json"


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


def _setup_sqler_db(docs: list[dict], mode: str, tmpdir: str | None = None) -> SQLerDB:
    if mode == "memory":
        db = SQLerDB.in_memory()
    else:
        path = os.path.join(tmpdir, "sqler.db") if tmpdir else "sqler.db"
        db = SQLerDB.on_disk(path)
    db.bulk_upsert("bench", docs)
    return db


def _setup_sqlite_mirror(docs: list[dict], mode: str = "memory",
                          tmpdir: str | None = None):
    if mode == "memory":
        conn = create_conn()
    else:
        path = os.path.join(tmpdir, "sqlite.db") if tmpdir else "sqlite.db"
        conn = create_conn(path, "disk")
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

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)
            size = config.scale.table_sizes[-1]

            for depth in (1, 2, 3):
                docs = self.gen.generate_nested(size, depth=depth)

                with tempfile.TemporaryDirectory() as tmpdir:
                    db = _setup_sqler_db(docs, mode, tmpdir if mode == "disk" else None)
                    conn = _setup_sqlite_mirror(docs, mode, tmpdir if mode == "disk" else None)

                    # Build consistent predicate: all depths query target > 5000
                    if depth == 1:
                        def do_query(db=db):
                            return db.query("bench").filter(
                                F("level_0")["target"] > 5000
                            ).all()
                        json_path = "$.level_0.target"
                    elif depth == 2:
                        def do_query(db=db):
                            return db.query("bench").filter(
                                F("level_0")["level_1"]["target"] > 5000
                            ).all()
                        json_path = "$.level_0.level_1.target"
                    else:
                        def do_query(db=db):
                            return db.query("bench").filter(
                                F("level_0")["level_1"]["level_2"]["target"] > 5000
                            ).all()
                        json_path = "$.level_0.level_1.level_2.target"

                    def do_sqlite(conn=conn, jp=json_path):
                        return query_nested(conn, "bench", jp, ">", 5000)

                    # Result count verification (LOW-3)
                    sqler_verify = do_query()
                    sqlite_verify = do_sqlite()
                    if len(sqler_verify) != len(sqlite_verify):
                        raise AssertionError(
                            f"Result count mismatch in nested_field_access_{prefix}_d{depth}: "
                            f"sqler={len(sqler_verify)}, sqlite={len(sqlite_verify)}"
                        )

                    arm_results = {}

                    def measure_sqler(fn=do_query):
                        arm_results["sqler"] = timer.measure(fn)

                    def measure_sqlite(fn=do_sqlite):
                        arm_results["sqlite"] = timer.measure(fn)

                    arms = _alternate_arms(
                        f"{self.name}_{prefix}_{depth}",
                        [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                    )
                    for label, fn in arms:
                        fn()
                        gc.collect()

                    for arm_label, baseline in [("sqler", "sqler"), ("sqlite", "sqlite")]:
                        results.append(BenchmarkResult(
                            scenario=self.name, suite=self.suite,
                            parameter="depth", value=f"{arm_label}_{prefix}_{depth}",
                            timing=arm_results[arm_label], rows=size,
                            metadata={"storage_mode": mode, "baseline": baseline},
                        ))

                    conn.close()

        return results

    def teardown(self) -> None:
        pass


class ArrayContainsIsin:
    """Scenario 12: F('tags').contains() and F('tags').isin() vs table size.

    v1.2: sqlite baseline now uses json_each(data, '$.tags') — same SQL as sqler (H-2).
    """

    name = "array_contains_isin"
    suite = SUITE_NAME
    description = "F('tags').contains(x) and F('tags').isin([...]) at varying sizes"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)

            for size in config.scale.table_sizes:
                docs = self.gen.generate_with_events(size)

                with tempfile.TemporaryDirectory() as tmpdir:
                    db = _setup_sqler_db(docs, mode, tmpdir if mode == "disk" else None)
                    conn = _setup_sqlite_mirror(docs, mode, tmpdir if mode == "disk" else None)

                    arm_results = {}

                    def measure_sqler(db=db):
                        def do_contains():
                            return db.query("bench").filter(F("tags").contains("alpha")).all()
                        arm_results["sqler_contains"] = timer.measure(do_contains)

                        gc.collect()

                        def do_isin():
                            return db.query("bench").filter(
                                F("tags").isin(["alpha", "bravo", "charlie"])
                            ).all()
                        arm_results["sqler_isin"] = timer.measure(do_isin)

                    def measure_sqlite(conn=conn):
                        def do_sqlite_contains():
                            return array_contains(conn, "bench", "tags", "alpha")
                        arm_results["sqlite_contains"] = timer.measure(do_sqlite_contains)

                        gc.collect()

                        def do_sqlite_isin():
                            return array_isin(conn, "bench", "tags", ["alpha", "bravo", "charlie"])
                        arm_results["sqlite_isin"] = timer.measure(do_sqlite_isin)

                    arms = _alternate_arms(
                        f"{self.name}_{prefix}_{size}",
                        [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                    )
                    for label, fn in arms:
                        fn()
                        gc.collect()

                    for label, stats_key, baseline in [
                        (f"sqler_{prefix}_contains_{size}", "sqler_contains", "sqler"),
                        (f"sqler_{prefix}_isin_{size}", "sqler_isin", "sqler"),
                        (f"sqlite_{prefix}_contains_{size}", "sqlite_contains", "sqlite"),
                        (f"sqlite_{prefix}_isin_{size}", "sqlite_isin", "sqlite"),
                    ]:
                        results.append(BenchmarkResult(
                            scenario=self.name, suite=self.suite,
                            parameter="op", value=label,
                            timing=arm_results[stats_key], rows=size,
                            metadata={"storage_mode": mode, "baseline": baseline},
                        ))

                    conn.close()

        return results

    def teardown(self) -> None:
        pass


class AnyWhere:
    """Scenario 13: F(['events']).any().where(F('type') == 'a') vs table size.

    v1.2: sqlite baseline uses json_each(data, '$.events') — same SQL as sqler (H-2).
    """

    name = "any_where"
    suite = SUITE_NAME
    description = "F(['events']).any().where(condition) on arrays of objects"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)

            for size in config.scale.table_sizes:
                docs = self.gen.generate_with_events(size, events_per_doc=5)

                with tempfile.TemporaryDirectory() as tmpdir:
                    db = _setup_sqler_db(docs, mode, tmpdir if mode == "disk" else None)
                    conn = _setup_sqlite_mirror(docs, mode, tmpdir if mode == "disk" else None)

                    arm_results = {}

                    def measure_sqler(db=db):
                        def do_any_eq():
                            return db.query("bench").filter(
                                F(["events"]).any().where(F("type") == "purchase")
                            ).all()
                        arm_results["sqler_eq"] = timer.measure(do_any_eq)

                        gc.collect()

                        def do_any_gt():
                            return db.query("bench").filter(
                                F(["events"]).any().where(F("amount") > 250)
                            ).all()
                        arm_results["sqler_gt"] = timer.measure(do_any_gt)

                    def measure_sqlite(conn=conn):
                        def do_sqlite_eq():
                            return any_where_eq(conn, "bench", "events", "type", "purchase")
                        arm_results["sqlite_eq"] = timer.measure(do_sqlite_eq)

                        gc.collect()

                        def do_sqlite_gt():
                            return any_where_gt(conn, "bench", "events", "amount", 250)
                        arm_results["sqlite_gt"] = timer.measure(do_sqlite_gt)

                    arms = _alternate_arms(
                        f"{self.name}_{prefix}_{size}",
                        [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                    )
                    for label, fn in arms:
                        fn()
                        gc.collect()

                    for label, stats_key, baseline in [
                        (f"sqler_{prefix}_eq_{size}", "sqler_eq", "sqler"),
                        (f"sqler_{prefix}_gt_{size}", "sqler_gt", "sqler"),
                        (f"sqlite_{prefix}_eq_{size}", "sqlite_eq", "sqlite"),
                        (f"sqlite_{prefix}_gt_{size}", "sqlite_gt", "sqlite"),
                    ]:
                        results.append(BenchmarkResult(
                            scenario=self.name, suite=self.suite,
                            parameter="table_size", value=label,
                            timing=arm_results[stats_key], rows=size,
                            metadata={"storage_mode": mode, "baseline": baseline},
                        ))

                    conn.close()

        return results

    def teardown(self) -> None:
        pass


SUITE = [NestedFieldAccess(), ArrayContainsIsin(), AnyWhere()]
