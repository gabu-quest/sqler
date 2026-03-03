"""Hydration benchmark suite — msgspec vs dataclass vs raw dict.

Measures the cost of converting raw dicts into model instances (hydration),
which benchmarks proved is the dominant cost in bulk reads (~60% of wall time
at 50K rows with Pydantic).

Scenario A: Pure hydration (no SQL) — isolates model_validate() cost.
Scenario B: End-to-end queryset.all() — full pipeline including SQL + hydration.

Fairness: identical data, same DB, arm alternation, GC between arms.
"""

from __future__ import annotations

import copy
import gc
import os
import tempfile

from sqler import MSGSPEC_AVAILABLE, SQLerDB

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.timer import PrecisionTimer
from benchmarks.generators.documents import DocumentGenerator
from benchmarks.generators.models import BenchmarkItemLite, BenchmarkItemMsgspec

SUITE_NAME = "hydration"


def _storage_modes(config: BenchmarkConfig) -> list[str]:
    if config.storage == "both":
        return ["memory", "disk"]
    return [config.storage]


def _mode_prefix(mode: str) -> str:
    return "mem" if mode == "memory" else "disk"


def _alternate_arms(scenario_name: str, arms: list[tuple]) -> list[tuple]:
    """Deterministic arm order alternation based on scenario name hash."""
    if hash(scenario_name) % 2:
        return list(reversed(arms))
    return list(arms)


class PureHydration:
    """Scenario A: Pure model_validate() cost — no SQL involved.

    Pre-generates N dicts, then measures the cost of hydrating them into
    model instances via each backend's model_validate().

    Arms:
      - lite: SQLerLiteModel.model_validate() (dataclass)
      - msgspec: SQLerMsgspecModel.model_validate() (msgspec Struct)
      - raw_msgspec_convert: msgspec.convert() directly (ceiling)
      - dict_passthrough: raw dict copy (floor)
    """

    name = "pure_hydration"
    suite = SUITE_NAME
    description = "model_validate() cost: dataclass vs msgspec vs raw dict"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        if not MSGSPEC_AVAILABLE:
            return []

        import msgspec

        results = []
        row_count = min(50_000, config.scale.max_rows)
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        docs = self.gen.generate("small", row_count)
        # Add _id to each doc (simulates what queryset produces)
        for i, d in enumerate(docs, 1):
            d["_id"] = i

        arm_results = {}

        def measure_lite():
            def do_lite(docs=docs):
                for d in docs:
                    BenchmarkItemLite.model_validate(d)

            arm_results["lite"] = timer.measure(do_lite)

        def measure_msgspec():
            def do_msgspec(docs=docs):
                for d in docs:
                    BenchmarkItemMsgspec.model_validate(d)

            arm_results["msgspec"] = timer.measure(do_msgspec)

        def measure_raw_convert():
            def do_raw(docs=docs):
                for d in docs:
                    filtered = {k: v for k, v in d.items() if not k.startswith("_")}
                    msgspec.convert(filtered, BenchmarkItemMsgspec, strict=False)

            arm_results["raw_msgspec_convert"] = timer.measure(do_raw)

        def measure_dict():
            def do_dict(docs=docs):
                for d in docs:
                    copy.copy(d)

            arm_results["dict_passthrough"] = timer.measure(do_dict)

        arms = _alternate_arms(
            f"{self.name}_{row_count}",
            [
                ("lite", measure_lite),
                ("msgspec", measure_msgspec),
                ("raw_convert", measure_raw_convert),
                ("dict", measure_dict),
            ],
        )
        for label, fn in arms:
            fn()
            gc.collect()

        for label, stats_key in [
            ("lite", "lite"),
            ("msgspec", "msgspec"),
            ("raw_msgspec_convert", "raw_msgspec_convert"),
            ("dict_passthrough", "dict_passthrough"),
        ]:
            stats = arm_results[stats_key]
            tp = row_count / (stats.median_ms / 1000) if stats.median_ms > 0 else 0
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="backend", value=f"{label}_{row_count}",
                timing=stats, rows=row_count,
                throughput=round(tp, 1),
                metadata={"baseline": label},
            ))

        return results

    def teardown(self) -> None:
        pass


class QuerysetHydration:
    """Scenario B: End-to-end queryset.all() — SQL + hydration.

    Inserts N rows into SQLite, then measures the full pipeline:
    query execution + JSON parsing + model hydration.

    Arms:
      - lite_all: SQLerLiteModel.all() (dataclass queryset)
      - msgspec_all: SQLerMsgspecModel.all() (msgspec queryset)
      - as_dicts: queryset.as_dicts() (no hydration baseline)
    """

    name = "queryset_hydration"
    suite = SUITE_NAME
    description = "queryset.all() end-to-end: lite vs msgspec vs as_dicts()"

    def setup(self, config: BenchmarkConfig) -> None:
        self.gen = DocumentGenerator(seed=42)

    def run(self, config: BenchmarkConfig) -> list[BenchmarkResult]:
        if not MSGSPEC_AVAILABLE:
            return []

        results = []
        row_count = min(50_000, config.scale.max_rows)
        timer = PrecisionTimer(warmup=config.warmup, iterations=config.iterations)

        docs = self.gen.generate("small", row_count)

        for mode in _storage_modes(config):
            prefix = _mode_prefix(mode)

            with tempfile.TemporaryDirectory() as tmpdir:
                # Create a shared database with pre-loaded data
                if mode == "memory":
                    db = SQLerDB.in_memory()
                else:
                    db = SQLerDB.on_disk(os.path.join(tmpdir, "hydration.db"))

                db.bulk_upsert("bench", [d.copy() for d in docs])

                # Bind both model types to the same table
                BenchmarkItemLite.set_db(db, table="bench")
                BenchmarkItemMsgspec.set_db(db, table="bench")

                arm_results = {}

                def measure_lite():
                    def do_lite():
                        BenchmarkItemLite.all()

                    arm_results["lite_all"] = timer.measure(do_lite)

                def measure_msgspec():
                    def do_msgspec():
                        BenchmarkItemMsgspec.all()

                    arm_results["msgspec_all"] = timer.measure(do_msgspec)

                def measure_dicts():
                    def do_dicts():
                        BenchmarkItemLite.query().as_dicts()

                    arm_results["as_dicts"] = timer.measure(do_dicts)

                arms = _alternate_arms(
                    f"{self.name}_{prefix}_{row_count}",
                    [
                        ("lite", measure_lite),
                        ("msgspec", measure_msgspec),
                        ("dicts", measure_dicts),
                    ],
                )
                for label, fn in arms:
                    fn()
                    gc.collect()

                for label, stats_key in [
                    (f"lite_all_{prefix}", "lite_all"),
                    (f"msgspec_all_{prefix}", "msgspec_all"),
                    (f"as_dicts_{prefix}", "as_dicts"),
                ]:
                    stats = arm_results[stats_key]
                    tp = row_count / (stats.median_ms / 1000) if stats.median_ms > 0 else 0
                    results.append(BenchmarkResult(
                        scenario=self.name, suite=self.suite,
                        parameter="backend", value=f"{label}_{row_count}",
                        timing=stats, rows=row_count,
                        throughput=round(tp, 1),
                        metadata={"storage_mode": mode, "baseline": stats_key},
                    ))

                if mode == "disk":
                    db.close()

        return results

    def teardown(self) -> None:
        pass


SUITE = [PureHydration(), QuerysetHydration()]
