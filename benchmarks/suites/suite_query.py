"""Query benchmark suite — scenarios 5-10."""

from __future__ import annotations

from sqler import F, SQLerDB

from benchmarks.core.base import BenchmarkResult
from benchmarks.core.config import BenchmarkConfig
from benchmarks.core.timer import PrecisionTimer
from benchmarks.generators.documents import DocumentGenerator

SUITE_NAME = "query"


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
            target_value = docs[size // 2]["value"]  # Pick a value we know exists

            # Without index
            db_no_idx = SQLerDB.in_memory()
            db_no_idx.bulk_upsert("bench", docs)

            def query_no_idx(db=db_no_idx, v=target_value):
                return db.query("bench").filter(F("value") == v).all()

            no_idx_stats = timer.measure(query_no_idx)

            # With index
            db_idx = SQLerDB.in_memory()
            db_idx.bulk_upsert("bench", docs)
            db_idx.create_index("bench", "value")

            def query_idx(db=db_idx, v=target_value):
                return db.query("bench").filter(F("value") == v).all()

            idx_stats = timer.measure(query_idx)

            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="rows", value=f"no_index_{size}",
                timing=no_idx_stats, rows=size,
            ))
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="rows", value=f"indexed_{size}",
                timing=idx_stats, rows=size,
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
        size = config.scale.table_sizes[-1]  # Largest table size
        docs = self.gen.generate("small", size)

        db = SQLerDB.in_memory()
        db.bulk_upsert("bench", docs)

        max_val = 10000
        for selectivity, label in [(0.01, "1%"), (0.10, "10%"), (0.50, "50%")]:
            range_size = int(max_val * selectivity)
            lo = 5000 - range_size // 2
            hi = 5000 + range_size // 2

            def do_query(db=db, lo=lo, hi=hi):
                return db.query("bench").filter(
                    (F("value") > lo) & (F("value") < hi)
                ).all()

            stats = timer.measure(do_query)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="selectivity", value=label,
                timing=stats, rows=size,
            ))

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

        # 2 predicates
        def q2(db=db):
            return db.query("bench").filter(
                (F("value") > 5000) & (F("category") == "tech")
            ).all()

        # 3 predicates
        def q3(db=db):
            return db.query("bench").filter(
                (F("value") > 3000) & (F("category") == "tech") & (F("score") > 50)
            ).all()

        # 5 predicates
        def q5(db=db):
            return db.query("bench").filter(
                (F("value") > 3000)
                & (F("category") == "tech")
                & (F("score") > 50)
                & (F("status") == "active")
            ).filter(F("name").like("alpha%")).all()

        for label, fn in [("2_predicates", q2), ("3_predicates", q3), ("5_predicates", q5)]:
            stats = timer.measure(fn)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="predicates", value=label,
                timing=stats, rows=size,
            ))

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

        for n in (10, 100, 1000):
            def do_query(db=db, n=n):
                return db.query("bench").order_by("value").limit(n).all()

            stats = timer.measure(do_query)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="limit", value=n,
                timing=stats, rows=size,
            ))

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

        max_page = size // 20  # per_page=20
        for page in (1, 10, 50, 100, 500):
            if page > max_page:
                continue

            def do_query(db=db, page=page):
                return db.query("bench").order_by("value").paginate(page=page, per_page=20)

            stats = timer.measure(do_query)
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="page", value=page,
                timing=stats, rows=size,
            ))

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

        # count() vs len(all())
        def do_count(db=db):
            return db.query("bench").filter(F("value") > 5000).count()

        def do_len_all(db=db):
            return len(db.query("bench").filter(F("value") > 5000).all())

        count_stats = timer.measure(do_count)
        len_all_stats = timer.measure(do_len_all)

        # exists() vs bool(first())
        def do_exists(db=db):
            return db.query("bench").filter(F("value") > 5000).exists()

        def do_bool_first(db=db):
            return bool(db.query("bench").filter(F("value") > 5000).first())

        exists_stats = timer.measure(do_exists)
        first_stats = timer.measure(do_bool_first)

        for label, stats in [
            ("count()", count_stats),
            ("len(all())", len_all_stats),
            ("exists()", exists_stats),
            ("bool(first())", first_stats),
        ]:
            results.append(BenchmarkResult(
                scenario=self.name, suite=self.suite,
                parameter="method", value=label,
                timing=stats, rows=size,
            ))

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
