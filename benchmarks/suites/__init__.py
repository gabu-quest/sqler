"""Benchmark suites registry."""

from __future__ import annotations

from .suite_advanced import SUITE as ADVANCED_SUITE
from .suite_hydration import SUITE as HYDRATION_SUITE
from .suite_insert import SUITE as INSERT_SUITE
from .suite_json import SUITE as JSON_SUITE
from .suite_ops import SUITE as OPS_SUITE
from .suite_query import SUITE as QUERY_SUITE
from .suite_tabular import SUITE as TABULAR_SUITE

ALL_SUITES: dict[str, list] = {
    "insert": INSERT_SUITE,
    "query": QUERY_SUITE,
    "json": JSON_SUITE,
    "advanced": ADVANCED_SUITE,
    "ops": OPS_SUITE,
    "hydration": HYDRATION_SUITE,
    "tabular": TABULAR_SUITE,
}


def get_scenarios(suite_names: list[str] | None = None) -> list:
    """Get scenarios for the given suites, or all if None."""
    if suite_names is None:
        suite_names = list(ALL_SUITES.keys())
    scenarios = []
    for name in suite_names:
        if name not in ALL_SUITES:
            raise ValueError(f"Unknown suite: {name!r} (choose from {list(ALL_SUITES)})")
        scenarios.extend(ALL_SUITES[name])
    return scenarios
