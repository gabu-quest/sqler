"""Benchmark configuration and scale profiles."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ScaleConfig:
    """Controls data sizes for benchmark runs."""

    name: str
    max_rows: int
    bulk_sizes: tuple[int, ...]
    table_sizes: tuple[int, ...]
    thread_counts: tuple[int, ...]

    @classmethod
    def small(cls) -> ScaleConfig:
        return cls(
            name="small",
            max_rows=10_000,
            bulk_sizes=(100, 1_000, 5_000, 10_000),
            table_sizes=(1_000, 5_000, 10_000),
            thread_counts=(2, 4),
        )

    @classmethod
    def medium(cls) -> ScaleConfig:
        return cls(
            name="medium",
            max_rows=50_000,
            bulk_sizes=(1_000, 5_000, 10_000, 25_000, 50_000),
            table_sizes=(10_000, 25_000, 50_000),
            thread_counts=(2, 4, 8),
        )

    @classmethod
    def large(cls) -> ScaleConfig:
        return cls(
            name="large",
            max_rows=100_000,
            bulk_sizes=(1_000, 10_000, 25_000, 50_000, 100_000),
            table_sizes=(25_000, 50_000, 100_000),
            thread_counts=(2, 4, 8, 16),
        )

    @classmethod
    def xlarge(cls) -> ScaleConfig:
        return cls(
            name="xlarge",
            max_rows=500_000,
            bulk_sizes=(10_000, 50_000, 100_000, 250_000, 500_000),
            table_sizes=(100_000, 250_000, 500_000),
            thread_counts=(2, 4, 8, 16, 32),
        )

    @classmethod
    def xxlarge(cls) -> ScaleConfig:
        return cls(
            name="xxlarge",
            max_rows=1_000_000,
            bulk_sizes=(10_000, 100_000, 250_000, 500_000, 1_000_000),
            table_sizes=(250_000, 500_000, 1_000_000),
            thread_counts=(2, 4, 8, 16, 32),
        )

    @classmethod
    def from_name(cls, name: str) -> ScaleConfig:
        configs = {
            "small": cls.small, "medium": cls.medium, "large": cls.large,
            "xlarge": cls.xlarge, "xxlarge": cls.xxlarge,
        }
        factory = configs.get(name)
        if factory is None:
            raise ValueError(f"Unknown scale: {name!r} (choose from {list(configs)})")
        return factory()


@dataclass(slots=True)
class BenchmarkConfig:
    """Full benchmark configuration."""

    scale: ScaleConfig = field(default_factory=ScaleConfig.small)
    warmup: int = 3
    iterations: int = 20
    output_dir: str = "benchmarks/results"
    suites: list[str] | None = None  # None = all suites
    storage: str = "memory"  # "memory" | "disk" | "both"
    verbose: bool = False
