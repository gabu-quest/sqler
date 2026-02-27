"""Query benchmark suite — scenarios 5-10."""

from __future__ import annotations

import json
import sqlite3

from sqler import F, SQLerDB

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.sqlite_baseline import (
    count_filter,
    create_index,
    create_table,
    exists_filter,
    insert_many,
    query_filter,
    query_paginate,
    query_range,
    query_top_n,
)
from benchmarks.core.timer import PrecisionTimer
from benchmarks.generators.documents import DocumentGenerator

SUITE_NAME = "query"


def _setup_sqlite_mirror(docs: list[dict]) -> sqlite3.Connection:
    """Create an in-memory sqlite3 conn with same data for baseline."""
    conn = sqlite3.connect(":memory:")
    create_table(conn, "bench")
    insert_many(conn, "bench", docs)
    return conn


class EqualityFilter:
    """Scenario 5: Equality filter with and without index, varying table size."""

    name = "equality_filter"
    suite = SUITE_NAME
    description = "F('value') == x with/without create_index, varying table size"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for size in config.scale.table_sizes:
            docs = self.gen.generate("small", size)
            target_value = docs[size // 2]["value"]

            # sqler — without index
            db_no_idx = SQLerDB.in_memory()
            db_no_idx.bulk_upsert("bench", docs)

            def query_no_idx(db=db_no_idx, v=target_value):
                return db.query("bench").filter(F("value") == v).all()

            no_idx_stats = timer.measure(query_no_idx)

            # sqler — with index
            db_idx = SQLerDB.in_memory()
            db_idx.bulk_upsert("bench", docs)
            db_idx.create_index("bench", "value")

            def query_idx(db=db_idx, v=target_value):
                return db.query("bench").filter(F("value") == v).all()

            idx_stats = timer.measure(query_idx)

            # SQLite baseline — without index
            conn_no_idx = _setup_sqlite_mirror(docs)

            def sqlite_no_idx(conn=conn_no_idx, v=target_value):
                return query_filter(conn, "bench", "value", "=", v)

            sqlite_no_idx_stats = timer.measure(sqlite_no_idx)

            # SQLite baseline — with index
            conn_idx = _setup_sqlite_mirror(docs)
            create_index(conn_idx, "bench", "value")

            def sqlite_idx(conn=conn_idx, v=target_value):
                return query_filter(conn, "bench", "value", "=", v)

            sqlite_idx_stats = timer.measure(sqlite_idx)

            for label, stats, baseline in [
                (f"sqler_no_index_{size}", no_idx_stats, "sqler"),
                (f"sqler_indexed_{size}", idx_stats, "sqler"),
                (f"sqlite_no_index_{size}", sqlite_no_idx_stats, "sqlite"),
                (f"sqlite_indexed_{size}", sqlite_idx_stats, "sqlite"),
            ]:
                results.append(BenchmarkResult(
                    scenario=self.name, suite=self.suite,
                    parameter="rows", value=label,
                    timing=stats, rows=size,
                    metadata={"storage_mode": "memory", "baseline": baseline},
                ))

            conn_no_idx.close()
            conn_idx.close()

        return results

    def teardown(self) -> None:
        pass


class RangeQueries:
    """Scenario 6: Range queries with varying selectivity."""

    name = "range_queries"
    suite = SUITE_NAME
    description = "F('value') range queries at 1%, 10%, 50% selectivity"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)
        size = config.scale.table_sizes[-1]
        docs = self.gen.generate("small", size)

        # Derive max_val from actual data
        max_val = max(d["value"] for d in docs)

        db = SQLerDB.in_memory()
        db.bulk_upsert("bench", docs)

        conn = _setup_sqlite_mirror(docs)

        for selectivity, label in [(0.01, "1%"), (0.10, "10%"), (0.50, "50%")]:
            range_size = int(max_val * selectivity)
            mid = max_val // 2
            lo = mid - range_size // 2
            hi = mid + range_size // 2

            # sqler
            def do_query(db=db, lo=lo, hi=hi):
                return db.query("bench").filter(
                    (F("value") > lo) & (F("value") < hi)
                ).all()

            stats = timer.measure(do_query)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="selectivity", value=f"sqler_{label}",
                timing=stats, rows=size,
                metadata={"storage_mode": "memory", "baseline": "sqler"},
            ))

            # SQLite baseline
            def do_sqlite(conn=conn, lo=lo, hi=hi):
                return query_range(conn, "bench", "value", lo, hi)

            sqlite_stats = timer.measure(do_sqlite)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="selectivity", value=f"sqlite_{label}",
                timing=sqlite_stats, rows=size,
                metadata={"storage_mode": "memory", "baseline": "sqlite"},
            ))

        conn.close()
        return results

    def teardown(self) -> None:
        pass


class ComplexFilterChains:
    """Scenario 7: Multiple predicate chains (2, 3, 5 predicates)."""

    name = "complex_filters"
    suite = SUITE_NAME
    description = "2/3/5 predicates with & and |, measure compilation + execution"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)
        size = config.scale.table_sizes[-1]
        docs = self.gen.generate("medium", size)

        db = SQLerDB.in_memory()
        db.bulk_upsert("bench", docs)

        conn = _setup_sqlite_mirror(docs)

        # sqler: 2 predicates
        def q2(db=db):
            return db.query("bench").filter(
                (F("value") > 5000) & (F("category") == "tech")
            ).all()

        # sqler: 3 predicates
        def q3(db=db):
            return db.query("bench").filter(
                (F("value") > 3000) & (F("category") == "tech") & (F("score") > 50)
            ).all()

        # sqler: 5 predicates
        def q5(db=db):
            return db.query("bench").filter(
                (F("value") > 3000)
                & (F("category") == "tech")
                & (F("score") > 50)
                & (F("status") == "active")
            ).filter(F("name").like("alpha%")).all()

        # SQLite: 2 predicates
        def sq2(conn=conn):
            return conn.execute(
                "SELECT data FROM [bench] WHERE "
                "json_extract(data, '$.value') > ? AND "
                "json_extract(data, '$.category') = ?",
                (5000, "tech"),
            ).fetchall()

        # SQLite: 3 predicates
        def sq3(conn=conn):
            return conn.execute(
                "SELECT data FROM [bench] WHERE "
                "json_extract(data, '$.value') > ? AND "
                "json_extract(data, '$.category') = ? AND "
                "json_extract(data, '$.score') > ?",
                (3000, "tech", 50),
            ).fetchall()

        # SQLite: 5 predicates
        def sq5(conn=conn):
            return conn.execute(
                "SELECT data FROM [bench] WHERE "
                "json_extract(data, '$.value') > ? AND "
                "json_extract(data, '$.category') = ? AND "
                "json_extract(data, '$.score') > ? AND "
                "json_extract(data, '$.status') = ? AND "
                "json_extract(data, '$.name') LIKE ?",
                (3000, "tech", 50, "active", "alpha%"),
            ).fetchall()

        for label, fn, baseline in [
            ("sqler_2_predicates", q2, "sqler"),
            ("sqler_3_predicates", q3, "sqler"),
            ("sqler_5_predicates", q5, "sqler"),
            ("sqlite_2_predicates", sq2, "sqlite"),
            ("sqlite_3_predicates", sq3, "sqlite"),
            ("sqlite_5_predicates", sq5, "sqlite"),
        ]:
            stats = timer.measure(fn)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="predicates", value=label,
                timing=stats, rows=size,
                metadata={"storage_mode": "memory", "baseline": baseline},
            ))

        conn.close()
        return results

    def teardown(self) -> None:
        pass


class TopN:
    """Scenario 8: ORDER BY + LIMIT (top-N) at various N values."""

    name = "top_n"
    suite = SUITE_NAME
    description = "query.order_by('value').limit(N) for N=10/100/1000"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)
        size = config.scale.table_sizes[-1]
        docs = self.gen.generate("small", size)

        db = SQLerDB.in_memory()
        db.bulk_upsert("bench", docs)

        conn = _setup_sqlite_mirror(docs)

        for n in (10, 100, 1000):
            # sqler
            def do_query(db=db, n=n):
                return db.query("bench").order_by("value").limit(n).all()

            stats = timer.measure(do_query)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="limit", value=f"sqler_{n}",
                timing=stats, rows=size,
                metadata={"storage_mode": "memory", "baseline": "sqler"},
            ))

            # SQLite baseline
            def do_sqlite(conn=conn, n=n):
                return query_top_n(conn, "bench", "value", n)

            sqlite_stats = timer.measure(do_sqlite)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="limit", value=f"sqlite_{n}",
                timing=sqlite_stats, rows=size,
                metadata={"storage_mode": "memory", "baseline": "sqlite"},
            ))

        conn.close()
        return results

    def teardown(self) -> None:
        pass


class PaginationDepth:
    """Scenario 9: Paginate at varying depths."""

    name = "pagination_depth"
    suite = SUITE_NAME
    description = "query.paginate(page=P) for P=1/10/50/100/500"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)
        size = config.scale.table_sizes[-1]
        docs = self.gen.generate("small", size)

        db = SQLerDB.in_memory()
        db.bulk_upsert("bench", docs)

        conn = _setup_sqlite_mirror(docs)

        max_page = size // 20  # per_page=20
        for page in (1, 10, 50, 100, 500):
            if page > max_page:
                continue

            # sqler
            def do_query(db=db, page=page):
                return db.query("bench").order_by("value").paginate(page=page, per_page=20)

            stats = timer.measure(do_query)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="page", value=f"sqler_{page}",
                timing=stats, rows=size,
                metadata={"storage_mode": "memory", "baseline": "sqler"},
            ))

            # SQLite baseline
            def do_sqlite(conn=conn, page=page):
                return query_paginate(conn, "bench", "value", page, 20)

            sqlite_stats = timer.measure(do_sqlite)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="page", value=f"sqlite_{page}",
                timing=sqlite_stats, rows=size,
                metadata={"storage_mode": "memory", "baseline": "sqlite"},
            ))

        conn.close()
        return results

    def teardown(self) -> None:
        pass


class CountVsMaterialize:
    """Scenario 10: count() vs len(all()), exists() vs bool(first())."""

    name = "count_vs_materialize"
    suite = SUITE_NAME
    description = "count() vs len(all()), exists() vs bool(first())"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)
        size = config.scale.table_sizes[-1]
        docs = self.gen.generate("small", size)

        db = SQLerDB.in_memory()
        db.bulk_upsert("bench", docs)

        conn = _setup_sqlite_mirror(docs)

        # sqler: count() vs len(all())
        def do_count(db=db):
            return db.query("bench").filter(F("value") > 5000).count()

        def do_len_all(db=db):
            return len(db.query("bench").filter(F("value") > 5000).all())

        count_stats = timer.measure(do_count)
        len_all_stats = timer.measure(do_len_all)

        # sqler: exists() vs bool(first())
        def do_exists(db=db):
            return db.query("bench").filter(F("value") > 5000).exists()

        def do_bool_first(db=db):
            return bool(db.query("bench").filter(F("value") > 5000).first())

        exists_stats = timer.measure(do_exists)
        first_stats = timer.measure(do_bool_first)

        # SQLite baselines
        def do_sqlite_count(conn=conn):
            return count_filter(conn, "bench", "value", ">", 5000)

        def do_sqlite_fetchall(conn=conn):
            rows = conn.execute(
                "SELECT data FROM [bench] WHERE json_extract(data, '$.value') > ?",
                (5000,),
            ).fetchall()
            return len(rows)

        def do_sqlite_exists(conn=conn):
            return exists_filter(conn, "bench", "value", ">", 5000)

        def do_sqlite_limit1(conn=conn):
            return bool(conn.execute(
                "SELECT 1 FROM [bench] WHERE json_extract(data, '$.value') > ? LIMIT 1",
                (5000,),
            ).fetchone())

        sqlite_count_stats = timer.measure(do_sqlite_count)
        sqlite_fetchall_stats = timer.measure(do_sqlite_fetchall)
        sqlite_exists_stats = timer.measure(do_sqlite_exists)
        sqlite_limit1_stats = timer.measure(do_sqlite_limit1)

        for label, stats, baseline in [
            ("sqler_count()", count_stats, "sqler"),
            ("sqler_len(all())", len_all_stats, "sqler"),
            ("sqler_exists()", exists_stats, "sqler"),
            ("sqler_bool(first())", first_stats, "sqler"),
            ("sqlite_COUNT(*)", sqlite_count_stats, "sqlite"),
            ("sqlite_fetchall+len", sqlite_fetchall_stats, "sqlite"),
            ("sqlite_EXISTS", sqlite_exists_stats, "sqlite"),
            ("sqlite_LIMIT1", sqlite_limit1_stats, "sqlite"),
        ]:
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="method", value=label,
                timing=stats, rows=size,
                metadata={"storage_mode": "memory", "baseline": baseline},
            ))

        conn.close()
        return results

    def teardown(self) -> None:
        pass


SUITE = [
    EqualityFilter(),
    RangeQueries(),
    ComplexFilterChains(),
    TopN(),
    PaginationDepth(),
    CountVsMaterialize(),
]
