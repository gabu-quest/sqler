"""Query benchmark suite — scenarios 5-10.

v1.2 fairness fixes:
  - create_conn() with matched PRAGMAs (H-1, M-2)
  - All baseline queries return deserialized dicts via fetch_as_dicts (H-5)
  - .exists() for S10 bool check (M-10)
  - Arm alternation per scenario (M-1)
  - gc.collect() between arms (M-4)
  - Disk mode support via --storage flag
  - Result count assertions between sqler and sqlite arms
"""

from __future__ import annotations

import gc
import os
import tempfile

from sqler import F, SQLerDB

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.sqlite_baseline import (
    count_filter,
    create_conn,
    create_index,
    create_table,
    exists_filter,
    fetch_as_dicts,
    insert_many,
    query_complex_filter,
    query_filter,
    query_paginate,
    query_range,
    query_top_n,
)
from benchmarks.core.timer import PrecisionTimer
from benchmarks.generators.documents import DocumentGenerator

SUITE_NAME = "query"


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
    """Create a sqler DB with data, matching storage mode."""
    if mode == "memory":
        db = SQLerDB.in_memory()
    else:
        path = os.path.join(tmpdir, "sqler.db") if tmpdir else "sqler.db"
        db = SQLerDB.on_disk(path)
    db.bulk_upsert("bench", docs)
    return db


def _setup_sqlite_mirror(docs: list[dict], mode: str = "memory",
                          tmpdir: str | None = None):
    """Create a sqlite3 conn with matched PRAGMAs and same data for baseline."""
    if mode == "memory":
        conn = create_conn()
    else:
        path = os.path.join(tmpdir, "sqlite.db") if tmpdir else "sqlite.db"
        conn = create_conn(path, "disk")
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

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)

            for size in config.scale.table_sizes:
                docs = self.gen.generate("small", size)
                target_value = docs[size // 2]["value"]

                with tempfile.TemporaryDirectory() as tmpdir:
                    arm_results = {}

                    def measure_sqler():
                        # Without index
                        db_no_idx = _setup_sqler_db(
                            docs, mode, os.path.join(tmpdir, "sqler_noidx") if mode == "disk" else None,
                        )
                        if mode == "disk":
                            os.makedirs(os.path.join(tmpdir, "sqler_noidx"), exist_ok=True)
                            db_no_idx = SQLerDB.on_disk(os.path.join(tmpdir, "sqler_noidx.db"))
                            db_no_idx.bulk_upsert("bench", docs)

                        def query_no_idx(db=db_no_idx, v=target_value):
                            return db.query("bench").filter(F("value") == v).all()

                        arm_results["sqler_no_index"] = timer.measure(query_no_idx)

                        gc.collect()

                        # With index
                        if mode == "memory":
                            db_idx = SQLerDB.in_memory()
                        else:
                            db_idx = SQLerDB.on_disk(os.path.join(tmpdir, "sqler_idx.db"))
                        db_idx.bulk_upsert("bench", docs)
                        db_idx.create_index("bench", "value")

                        def query_idx(db=db_idx, v=target_value):
                            return db.query("bench").filter(F("value") == v).all()

                        arm_results["sqler_indexed"] = timer.measure(query_idx)

                    def measure_sqlite():
                        # Without index
                        conn_no_idx = _setup_sqlite_mirror(
                            docs, mode, tmpdir if mode == "disk" else None,
                        )

                        def sqlite_no_idx(conn=conn_no_idx, v=target_value):
                            return query_filter(conn, "bench", "value", "=", v)

                        arm_results["sqlite_no_index"] = timer.measure(sqlite_no_idx)
                        conn_no_idx.close()

                        gc.collect()

                        # With index
                        if mode == "memory":
                            conn_idx = create_conn()
                        else:
                            conn_idx = create_conn(os.path.join(tmpdir, "sqlite_idx.db"), "disk")
                        create_table(conn_idx, "bench")
                        insert_many(conn_idx, "bench", docs)
                        create_index(conn_idx, "bench", "value")

                        def sqlite_idx(conn=conn_idx, v=target_value):
                            return query_filter(conn, "bench", "value", "=", v)

                        arm_results["sqlite_indexed"] = timer.measure(sqlite_idx)
                        conn_idx.close()

                    arms = _alternate_arms(
                        f"{self.name}_{prefix}_{size}",
                        [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                    )
                    for label, fn in arms:
                        fn()
                        gc.collect()

                    for label, stats_key, baseline in [
                        (f"sqler_{prefix}_no_index_{size}", "sqler_no_index", "sqler"),
                        (f"sqler_{prefix}_indexed_{size}", "sqler_indexed", "sqler"),
                        (f"sqlite_{prefix}_no_index_{size}", "sqlite_no_index", "sqlite"),
                        (f"sqlite_{prefix}_indexed_{size}", "sqlite_indexed", "sqlite"),
                    ]:
                        results.append(BenchmarkResult(
                            scenario=self.name, suite=self.suite,
                            parameter="rows", value=label,
                            timing=arm_results[stats_key], rows=size,
                            metadata={"storage_mode": mode, "baseline": baseline},
                        ))

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

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)
            size = config.scale.table_sizes[-1]
            docs = self.gen.generate("small", size)
            max_val = max(d["value"] for d in docs)

            with tempfile.TemporaryDirectory() as tmpdir:
                db = _setup_sqler_db(docs, mode, tmpdir if mode == "disk" else None)
                conn = _setup_sqlite_mirror(docs, mode, tmpdir if mode == "disk" else None)

                for selectivity, sel_label in [(0.01, "1%"), (0.10, "10%"), (0.50, "50%")]:
                    range_size = int(max_val * selectivity)
                    mid = max_val // 2
                    lo = mid - range_size // 2
                    hi = mid + range_size // 2
                    arm_results = {}

                    def measure_sqler(db=db, lo=lo, hi=hi):
                        def do_query():
                            return db.query("bench").filter(
                                (F("value") > lo) & (F("value") < hi)
                            ).all()
                        arm_results["sqler"] = timer.measure(do_query)

                    def measure_sqlite(conn=conn, lo=lo, hi=hi):
                        def do_sqlite():
                            return query_range(conn, "bench", "value", lo, hi)
                        arm_results["sqlite"] = timer.measure(do_sqlite)

                    arms = _alternate_arms(
                        f"{self.name}_{prefix}_{sel_label}",
                        [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                    )
                    for label, fn in arms:
                        fn()
                        gc.collect()

                    for arm_label, baseline in [("sqler", "sqler"), ("sqlite", "sqlite")]:
                        results.append(BenchmarkResult(
                            scenario=self.name, suite=self.suite,
                            parameter="selectivity", value=f"{arm_label}_{prefix}_{sel_label}",
                            timing=arm_results[arm_label], rows=size,
                            metadata={"storage_mode": mode, "baseline": baseline},
                        ))

                conn.close()

        return results

    def teardown(self) -> None:
        pass


class ComplexFilterChains:
    """Scenario 7: Multiple predicate chains (2, 3, 5 predicates).

    v1.2: SQLite baseline now deserializes JSON via fetch_as_dicts (H-5).
    """

    name = "complex_filters"
    suite = SUITE_NAME
    description = "2/3/5 predicates with & and |, measure compilation + execution"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)
            size = config.scale.table_sizes[-1]
            docs = self.gen.generate("medium", size)

            with tempfile.TemporaryDirectory() as tmpdir:
                db = _setup_sqler_db(docs, mode, tmpdir if mode == "disk" else None)
                conn = _setup_sqlite_mirror(docs, mode, tmpdir if mode == "disk" else None)

                # Define query pairs: (predicate_count, sqler_fn, sqlite_fn)
                queries = []

                # 2 predicates
                def q2_sqler(db=db):
                    return db.query("bench").filter(
                        (F("value") > 5000) & (F("category") == "tech")
                    ).all()

                def q2_sqlite(conn=conn):
                    return query_complex_filter(
                        conn, "bench",
                        "json_extract(data, '$.value') > ? AND json_extract(data, '$.category') = ?",
                        (5000, "tech"),
                    )

                queries.append(("2_predicates", q2_sqler, q2_sqlite))

                # 3 predicates
                def q3_sqler(db=db):
                    return db.query("bench").filter(
                        (F("value") > 3000) & (F("category") == "tech") & (F("score") > 50)
                    ).all()

                def q3_sqlite(conn=conn):
                    return query_complex_filter(
                        conn, "bench",
                        "json_extract(data, '$.value') > ? AND "
                        "json_extract(data, '$.category') = ? AND "
                        "json_extract(data, '$.score') > ?",
                        (3000, "tech", 50),
                    )

                queries.append(("3_predicates", q3_sqler, q3_sqlite))

                # 5 predicates
                def q5_sqler(db=db):
                    return db.query("bench").filter(
                        (F("value") > 3000)
                        & (F("category") == "tech")
                        & (F("score") > 50)
                        & (F("status") == "active")
                    ).filter(F("name").like("alpha%")).all()

                def q5_sqlite(conn=conn):
                    return query_complex_filter(
                        conn, "bench",
                        "json_extract(data, '$.value') > ? AND "
                        "json_extract(data, '$.category') = ? AND "
                        "json_extract(data, '$.score') > ? AND "
                        "json_extract(data, '$.status') = ? AND "
                        "json_extract(data, '$.name') LIKE ?",
                        (3000, "tech", 50, "active", "alpha%"),
                    )

                queries.append(("5_predicates", q5_sqler, q5_sqlite))

                for pred_label, sqler_fn, sqlite_fn in queries:
                    arm_results = {}

                    def measure_sqler(fn=sqler_fn):
                        arm_results["sqler"] = timer.measure(fn)

                    def measure_sqlite(fn=sqlite_fn):
                        arm_results["sqlite"] = timer.measure(fn)

                    arms = _alternate_arms(
                        f"{self.name}_{prefix}_{pred_label}",
                        [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                    )
                    for label, fn in arms:
                        fn()
                        gc.collect()

                    for arm_label, baseline in [("sqler", "sqler"), ("sqlite", "sqlite")]:
                        results.append(BenchmarkResult(
                            scenario=self.name, suite=self.suite,
                            parameter="predicates", value=f"{arm_label}_{prefix}_{pred_label}",
                            timing=arm_results[arm_label], rows=size,
                            metadata={"storage_mode": mode, "baseline": baseline},
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

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)
            size = config.scale.table_sizes[-1]
            docs = self.gen.generate("small", size)

            with tempfile.TemporaryDirectory() as tmpdir:
                db = _setup_sqler_db(docs, mode, tmpdir if mode == "disk" else None)
                conn = _setup_sqlite_mirror(docs, mode, tmpdir if mode == "disk" else None)

                for n in (10, 100, 1000):
                    arm_results = {}

                    def measure_sqler(db=db, n=n):
                        def do_query():
                            return db.query("bench").order_by("value").limit(n).all()
                        arm_results["sqler"] = timer.measure(do_query)

                    def measure_sqlite(conn=conn, n=n):
                        def do_sqlite():
                            return query_top_n(conn, "bench", "value", n)
                        arm_results["sqlite"] = timer.measure(do_sqlite)

                    arms = _alternate_arms(
                        f"{self.name}_{prefix}_{n}",
                        [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                    )
                    for label, fn in arms:
                        fn()
                        gc.collect()

                    for arm_label, baseline in [("sqler", "sqler"), ("sqlite", "sqlite")]:
                        results.append(BenchmarkResult(
                            scenario=self.name, suite=self.suite,
                            parameter="limit", value=f"{arm_label}_{prefix}_{n}",
                            timing=arm_results[arm_label], rows=size,
                            metadata={"storage_mode": mode, "baseline": baseline},
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

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)
            size = config.scale.table_sizes[-1]
            docs = self.gen.generate("small", size)

            with tempfile.TemporaryDirectory() as tmpdir:
                db = _setup_sqler_db(docs, mode, tmpdir if mode == "disk" else None)
                conn = _setup_sqlite_mirror(docs, mode, tmpdir if mode == "disk" else None)

                max_page = size // 20
                for page in (1, 10, 50, 100, 500):
                    if page > max_page:
                        continue

                    arm_results = {}

                    def measure_sqler(db=db, page=page):
                        def do_query():
                            return db.query("bench").order_by("value").paginate(page=page, per_page=20)
                        arm_results["sqler"] = timer.measure(do_query)

                    def measure_sqlite(conn=conn, page=page):
                        def do_sqlite():
                            return query_paginate(conn, "bench", "value", page, 20)
                        arm_results["sqlite"] = timer.measure(do_sqlite)

                    arms = _alternate_arms(
                        f"{self.name}_{prefix}_{page}",
                        [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                    )
                    for label, fn in arms:
                        fn()
                        gc.collect()

                    for arm_label, baseline in [("sqler", "sqler"), ("sqlite", "sqlite")]:
                        results.append(BenchmarkResult(
                            scenario=self.name, suite=self.suite,
                            parameter="page", value=f"{arm_label}_{prefix}_{page}",
                            timing=arm_results[arm_label], rows=size,
                            metadata={"storage_mode": mode, "baseline": baseline},
                        ))

                conn.close()

        return results

    def teardown(self) -> None:
        pass


class CountVsMaterialize:
    """Scenario 10: count() vs len(all()), exists() vs bool(first()).

    v1.2: SQLite baseline uses fetch_as_dicts for fetchall+len path.
    """

    name = "count_vs_materialize"
    suite = SUITE_NAME
    description = "count() vs len(all()), exists() vs bool(first())"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)
            size = config.scale.table_sizes[-1]
            docs = self.gen.generate("small", size)

            with tempfile.TemporaryDirectory() as tmpdir:
                db = _setup_sqler_db(docs, mode, tmpdir if mode == "disk" else None)
                conn = _setup_sqlite_mirror(docs, mode, tmpdir if mode == "disk" else None)

                arm_results = {}

                def measure_sqler():
                    def do_count(db=db):
                        return db.query("bench").filter(F("value") > 5000).count()

                    def do_len_all(db=db):
                        return len(db.query("bench").filter(F("value") > 5000).all())

                    def do_exists(db=db):
                        return db.query("bench").filter(F("value") > 5000).exists()

                    def do_bool_first(db=db):
                        return bool(db.query("bench").filter(F("value") > 5000).first())

                    arm_results["sqler_count()"] = timer.measure(do_count)
                    gc.collect()
                    arm_results["sqler_len(all())"] = timer.measure(do_len_all)
                    gc.collect()
                    arm_results["sqler_exists()"] = timer.measure(do_exists)
                    gc.collect()
                    arm_results["sqler_bool(first())"] = timer.measure(do_bool_first)

                def measure_sqlite():
                    def do_sqlite_count(conn=conn):
                        return count_filter(conn, "bench", "value", ">", 5000)

                    def do_sqlite_fetchall(conn=conn):
                        # v1.2: deserialize JSON like sqler's .all() (H-5)
                        cursor = conn.execute(
                            "SELECT data FROM [bench] WHERE json_extract(data, '$.value') > ?",
                            (5000,),
                        )
                        return len(fetch_as_dicts(cursor))

                    def do_sqlite_exists(conn=conn):
                        return exists_filter(conn, "bench", "value", ">", 5000)

                    def do_sqlite_limit1(conn=conn):
                        return bool(conn.execute(
                            "SELECT 1 FROM [bench] WHERE json_extract(data, '$.value') > ? LIMIT 1",
                            (5000,),
                        ).fetchone())

                    arm_results["sqlite_COUNT(*)"] = timer.measure(do_sqlite_count)
                    gc.collect()
                    arm_results["sqlite_fetchall+len"] = timer.measure(do_sqlite_fetchall)
                    gc.collect()
                    arm_results["sqlite_EXISTS"] = timer.measure(do_sqlite_exists)
                    gc.collect()
                    arm_results["sqlite_LIMIT1"] = timer.measure(do_sqlite_limit1)

                arms = _alternate_arms(
                    f"{self.name}_{prefix}",
                    [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                )
                for label, fn in arms:
                    fn()
                    gc.collect()

                for label, baseline in [
                    (f"sqler_{prefix}_count()", "sqler"),
                    (f"sqler_{prefix}_len(all())", "sqler"),
                    (f"sqler_{prefix}_exists()", "sqler"),
                    (f"sqler_{prefix}_bool(first())", "sqler"),
                    (f"sqlite_{prefix}_COUNT(*)", "sqlite"),
                    (f"sqlite_{prefix}_fetchall+len", "sqlite"),
                    (f"sqlite_{prefix}_EXISTS", "sqlite"),
                    (f"sqlite_{prefix}_LIMIT1", "sqlite"),
                ]:
                    # Map label to arm_results key
                    # Strip prefix from label to find the key
                    for key in arm_results:
                        stripped = label.removeprefix(f"sqler_{prefix}_").removeprefix(f"sqlite_{prefix}_")
                        if key.endswith(stripped) or key == stripped:
                            results.append(BenchmarkResult(
                                scenario=self.name, suite=self.suite,
                                parameter="method", value=label,
                                timing=arm_results[key], rows=size,
                                metadata={"storage_mode": mode, "baseline": baseline},
                            ))
                            break

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
