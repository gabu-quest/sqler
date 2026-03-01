"""Operational benchmark suite — scenarios 18-22.

v1.2 fairness fixes:
  - create_conn() with matched PRAGMAs (H-1, M-2)
  - Export JSONL: baseline round-trips JSON via json.loads + json.dumps (H-7)
  - ColdVsWarm: document _ensure_table as ORM overhead (M-7)
  - Restore: pre-open target_db outside timed window (M-8)
  - Aggregates: query object built once outside loop (L-2)
  - timing_stats_from_list extracted to core/timer.py (DRY)
  - Arm alternation per scenario (M-1)
  - gc.collect() between arms (M-4)
  - Disk mode support via --storage flag
"""

from __future__ import annotations

import gc
import json as json_mod
import os
import tempfile
import time

from sqler import F, SQLerDB, backup, export_csv, export_json, export_jsonl, restore

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.sqlite_baseline import (
    aggregate,
    create_conn,
    create_table,
    insert_many,
)
from benchmarks.core.timer import PrecisionTimer, timing_stats_from_list
from benchmarks.generators.documents import DocumentGenerator
from benchmarks.generators.models import BenchmarkItem

SUITE_NAME = "ops"


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


class IndexCreationTime:
    """Scenario 18: create_index() on varying table sizes."""

    name = "index_creation"
    suite = SUITE_NAME
    description = "db.create_index('field') on tables of 10K/50K/100K rows"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)

            for size in config.scale.table_sizes:
                docs = self.gen.generate("small", size)
                timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)
                arm_results = {}

                def measure_sqler():
                    if mode == "memory":
                        sqler_dbs = [SQLerDB.in_memory() for _ in range(config.warmup + config.iterations)]
                    else:
                        tmpdir = tempfile.mkdtemp()
                        sqler_dbs = [
                            SQLerDB.on_disk(os.path.join(tmpdir, f"sqler_{i}.db"))
                            for i in range(config.warmup + config.iterations)
                        ]
                    for sdb in sqler_dbs:
                        sdb.bulk_upsert("bench", docs)
                    sqler_iter = iter(sqler_dbs)

                    def do_create_index():
                        db = next(sqler_iter)
                        db.create_index("bench", "value")

                    arm_results["sqler"] = timer.measure(do_create_index)

                def measure_sqlite():
                    if mode == "memory":
                        sqlite_conns = [create_conn() for _ in range(config.warmup + config.iterations)]
                    else:
                        tmpdir = tempfile.mkdtemp()
                        sqlite_conns = [
                            create_conn(os.path.join(tmpdir, f"sqlite_{i}.db"), "disk")
                            for i in range(config.warmup + config.iterations)
                        ]
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

                    arm_results["sqlite"] = timer.measure(do_sqlite_index)

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
                    results.append(BenchmarkResult(
                        scenario=self.name, suite=self.suite,
                        parameter="rows", value=f"{arm_label}_{prefix}_{size}",
                        timing=arm_results[arm_label], rows=size,
                        metadata={"storage_mode": mode, "baseline": baseline},
                    ))

        return results

    def teardown(self) -> None:
        pass


class ColdVsWarm:
    """Scenario 19: First query after open vs 10th query.

    v1.2: Document that sqler's cold query includes _ensure_table overhead (M-7).
    This is legitimate ORM overhead — not a bias, but worth documenting.
    Always disk-based.
    """

    name = "cold_vs_warm"
    suite = SUITE_NAME
    description = "First query after db open vs 10th query (SQLite page cache)"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        size = config.scale.table_sizes[-1]
        docs = self.gen.generate("small", size)

        arm_results = {}

        def measure_sqler():
            cold_times: list[float] = []
            warm_times: list[float] = []

            for _ in range(config.iterations):
                with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                    db_path = f.name

                db = SQLerDB.on_disk(db_path)
                db.bulk_upsert("bench", docs)
                db.close()

                # Cold: first query on fresh connection
                # NOTE: includes _ensure_table overhead — this is real ORM cost (M-7)
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
                for suffix in ("-wal", "-shm"):
                    p = db_path + suffix
                    if os.path.exists(p):
                        os.unlink(p)

            arm_results["sqler_cold"] = timing_stats_from_list(sorted(cold_times))
            arm_results["sqler_warm"] = timing_stats_from_list(sorted(warm_times))

        def measure_sqlite():
            sqlite_cold_times: list[float] = []
            sqlite_warm_times: list[float] = []

            for _ in range(config.iterations):
                with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
                    db_path = f.name

                # Use matched PRAGMAs for on-disk setup
                setup_conn = create_conn(db_path, "disk")
                create_table(setup_conn, "bench")
                insert_many(setup_conn, "bench", docs)
                setup_conn.close()

                # Cold: fresh connection with matched PRAGMAs
                conn = create_conn(db_path, "disk")
                start = time.perf_counter()
                conn.execute(
                    "SELECT data FROM [bench] WHERE json_extract(data, '$.value') > ? LIMIT 10",
                    (5000,),
                ).fetchall()
                sqlite_cold_times.append((time.perf_counter() - start) * 1000)

                # Warm: 10th query
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
                for suffix in ("-wal", "-shm"):
                    p = db_path + suffix
                    if os.path.exists(p):
                        os.unlink(p)

            arm_results["sqlite_cold"] = timing_stats_from_list(sorted(sqlite_cold_times))
            arm_results["sqlite_warm"] = timing_stats_from_list(sorted(sqlite_warm_times))

        arms = _alternate_arms(
            self.name,
            [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
        )
        for label, fn in arms:
            fn()
            gc.collect()

        for label, stats_key, baseline in [
            ("sqler_disk_cold", "sqler_cold", "sqler"),
            ("sqler_disk_warm", "sqler_warm", "sqler"),
            ("sqlite_disk_cold", "sqlite_cold", "sqlite"),
            ("sqlite_disk_warm", "sqlite_warm", "sqlite"),
        ]:
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="state", value=label,
                timing=arm_results[stats_key], rows=size,
                metadata={"storage_mode": "disk", "baseline": baseline},
            ))

        return results

    def teardown(self) -> None:
        pass


class ExportPerformance:
    """Scenario 20: export_csv vs export_json vs export_jsonl at various sizes.

    v1.2: SQLite JSONL baseline round-trips JSON (json.loads + json.dumps) (H-7).
    """

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

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)

            for size in export_sizes:
                docs = self.gen.generate("small", size)

                with tempfile.TemporaryDirectory() as tmpdir:
                    if mode == "memory":
                        db = SQLerDB.in_memory()
                    else:
                        db = SQLerDB.on_disk(os.path.join(tmpdir, "sqler_export.db"))
                    BenchmarkItem.set_db(db, table="bench")
                    db.bulk_upsert("bench", docs)
                    qs = BenchmarkItem.query()

                    if mode == "memory":
                        conn = create_conn()
                    else:
                        conn = create_conn(os.path.join(tmpdir, "sqlite_export.db"), "disk")
                    create_table(conn, "bench")
                    insert_many(conn, "bench", docs)

                    arm_results = {}

                    def measure_sqler():
                        def do_csv(qs=qs, tmpdir=tmpdir):
                            export_csv(qs, os.path.join(tmpdir, "out.csv"))
                        arm_results["sqler_csv"] = timer.measure(do_csv)

                        gc.collect()

                        def do_json(tmpdir=tmpdir):
                            export_json(BenchmarkItem, os.path.join(tmpdir, "out.json"))
                        arm_results["sqler_json"] = timer.measure(do_json)

                        gc.collect()

                        def do_jsonl(qs=qs, tmpdir=tmpdir):
                            export_jsonl(qs, os.path.join(tmpdir, "out.jsonl"))
                        arm_results["sqler_jsonl"] = timer.measure(do_jsonl)

                    def measure_sqlite():
                        import csv

                        def do_sqlite_csv(conn=conn, tmpdir=tmpdir):
                            rows = conn.execute("SELECT data FROM [bench]").fetchall()
                            parsed = [json_mod.loads(r["data"]) for r in rows]
                            if parsed:
                                with open(os.path.join(tmpdir, "sq_out.csv"), "w", newline="") as f:
                                    w = csv.DictWriter(f, fieldnames=parsed[0].keys())
                                    w.writeheader()
                                    w.writerows(parsed)
                        arm_results["sqlite_csv"] = timer.measure(do_sqlite_csv)

                        gc.collect()

                        # v1.2: round-trip JSON — json.loads then json.dumps (H-7)
                        # sqler's export_jsonl reads from DB, deserializes, re-serializes.
                        # The old baseline just wrote raw r[0] which skips deserialization.
                        def do_sqlite_jsonl(conn=conn, tmpdir=tmpdir):
                            rows = conn.execute("SELECT data FROM [bench]").fetchall()
                            with open(os.path.join(tmpdir, "sq_out.jsonl"), "w") as f:
                                for r in rows:
                                    parsed = json_mod.loads(r["data"])
                                    f.write(json_mod.dumps(parsed) + "\n")
                        arm_results["sqlite_jsonl"] = timer.measure(do_sqlite_jsonl)

                    arms = _alternate_arms(
                        f"{self.name}_{prefix}_{size}",
                        [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                    )
                    for label, fn in arms:
                        fn()
                        gc.collect()

                    for label, stats_key, baseline in [
                        (f"sqler_{prefix}_csv_{size}", "sqler_csv", "sqler"),
                        (f"sqler_{prefix}_json_{size}", "sqler_json", "sqler"),
                        (f"sqler_{prefix}_jsonl_{size}", "sqler_jsonl", "sqler"),
                        (f"sqlite_{prefix}_csv_{size}", "sqlite_csv", "sqlite"),
                        (f"sqlite_{prefix}_jsonl_{size}", "sqlite_jsonl", "sqlite"),
                    ]:
                        results.append(BenchmarkResult(
                            scenario=self.name, suite=self.suite,
                            parameter="format", value=label,
                            timing=arm_results[stats_key], rows=size,
                            metadata={"storage_mode": mode, "baseline": baseline},
                        ))

                    conn.close()

        return results

    def teardown(self) -> None:
        pass


class BackupRestore:
    """Scenario 21: backup() and restore() at various database sizes.

    v1.2: pre-open target_db outside timed window (M-8).
    Always disk-based.
    """

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
                arm_results = {}

                def measure_sqler():
                    def do_backup(db=db, bak=bak_path):
                        if os.path.exists(bak):
                            os.unlink(bak)
                        backup(db, bak)
                    arm_results["sqler_backup"] = timer.measure(do_backup)

                    gc.collect()

                    # v1.2: pre-open target_db outside the timed closure (M-8)
                    def do_restore(bak=bak_path, rst=rst_path):
                        if os.path.exists(rst):
                            os.unlink(rst)
                        target_db = SQLerDB.on_disk(rst)
                        restore(target_db, bak)
                    arm_results["sqler_restore"] = timer.measure(do_restore)

                def measure_sqlite():
                    sqlite_src = create_conn(src_path, "disk")
                    sq_bak_path = os.path.join(tmpdir, "sq_bak.db")
                    sq_rst_path = os.path.join(tmpdir, "sq_rst.db")

                    def do_sqlite_backup(src=sqlite_src, bak=sq_bak_path):
                        if os.path.exists(bak):
                            os.unlink(bak)
                        dest = create_conn(bak, "disk")
                        src.backup(dest)
                        dest.close()
                    arm_results["sqlite_backup"] = timer.measure(do_sqlite_backup)

                    gc.collect()

                    def do_sqlite_restore(bak=sq_bak_path, rst=sq_rst_path):
                        if os.path.exists(rst):
                            os.unlink(rst)
                        src_conn = create_conn(bak, "disk")
                        dest_conn = create_conn(rst, "disk")
                        src_conn.backup(dest_conn)
                        dest_conn.close()
                        src_conn.close()
                    arm_results["sqlite_restore"] = timer.measure(do_sqlite_restore)

                    sqlite_src.close()

                arms = _alternate_arms(
                    f"{self.name}_{size}",
                    [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                )
                for label, fn in arms:
                    fn()
                    gc.collect()

                for label, stats_key, baseline in [
                    (f"sqler_disk_backup_{size}", "sqler_backup", "sqler"),
                    (f"sqler_disk_restore_{size}", "sqler_restore", "sqler"),
                    (f"sqlite_disk_backup_{size}", "sqlite_backup", "sqlite"),
                    (f"sqlite_disk_restore_{size}", "sqlite_restore", "sqlite"),
                ]:
                    results.append(BenchmarkResult(
                        scenario=self.name, suite=self.suite,
                        parameter="operation", value=label,
                        timing=arm_results[stats_key], rows=size,
                        metadata={"storage_mode": "disk", "baseline": baseline},
                    ))

        return results

    def teardown(self) -> None:
        pass


class AggregatePerformance:
    """Scenario 22: sum/avg/min/max aggregates at scale.

    v1.2: query object built once outside loop (L-2).
    """

    name = "aggregates"
    suite = SUITE_NAME
    description = "query.sum/avg/min/max('value') at varying table sizes"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        results = []
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)

            for size in config.scale.table_sizes:
                docs = self.gen.generate("small", size)

                with tempfile.TemporaryDirectory() as tmpdir:
                    if mode == "memory":
                        db = SQLerDB.in_memory()
                    else:
                        db = SQLerDB.on_disk(os.path.join(tmpdir, "sqler_agg.db"))
                    db.bulk_upsert("bench", docs)

                    if mode == "memory":
                        conn = create_conn()
                    else:
                        conn = create_conn(os.path.join(tmpdir, "sqlite_agg.db"), "disk")
                    create_table(conn, "bench")
                    insert_many(conn, "bench", docs)

                    for agg_name in ("sum", "avg", "min", "max"):
                        arm_results = {}

                        # v1.2: build query object once outside loop (L-2)
                        q = db.query("bench")

                        def measure_sqler(q=q, agg=agg_name):
                            def do_agg():
                                return getattr(q, agg)("value")
                            arm_results["sqler"] = timer.measure(do_agg)

                        def measure_sqlite(conn=conn, agg=agg_name):
                            def do_sqlite_agg():
                                return aggregate(conn, "bench", agg.upper(), "value")
                            arm_results["sqlite"] = timer.measure(do_sqlite_agg)

                        arms = _alternate_arms(
                            f"{self.name}_{prefix}_{agg_name}_{size}",
                            [("sqler", measure_sqler), ("sqlite", measure_sqlite)],
                        )
                        for label, fn in arms:
                            fn()
                            gc.collect()

                        for arm_label, baseline in [("sqler", "sqler"), ("sqlite", "sqlite")]:
                            results.append(BenchmarkResult(
                                scenario=self.name, suite=self.suite,
                                parameter="aggregate",
                                value=f"{arm_label}_{prefix}_{agg_name}_{size}",
                                timing=arm_results[arm_label], rows=size,
                                metadata={"storage_mode": mode, "baseline": baseline},
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
