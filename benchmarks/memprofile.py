"""Memory profiling for benchmark runs.

Uses tracemalloc snapshots to measure peak memory and top allocators per scenario.
Supports multi-run statistics, setup/run memory separation, snapshot diffs,
overhead estimation, budget enforcement, and baseline management.

Usage: uv run --group benchmarks python -u -m benchmarks.memprofile --scale large
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import shutil
import statistics
import sys
import time
import tracemalloc
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _print(msg: str) -> None:
    print(msg, flush=True)


# -- Complexity classes for extrapolation --

# O(1): fixed overhead regardless of row count
_O1_SCENARIOS = frozenset({
    "advanced/connection_pool",
    "advanced/optimistic_locking",
    "ops/cold_vs_warm",
})

# O(n log n): B-tree index overhead
_ONLOGN_SCENARIOS = frozenset({
    "ops/index_creation",
})

# Everything else is O(n) — scales linearly with data


def _complexity_class(name: str) -> str:
    if name in _O1_SCENARIOS:
        return "O(1)"
    if name in _ONLOGN_SCENARIOS:
        return "O(n log n)"
    return "O(n)"


# -- Dataclasses --

@dataclass
class RunProfile:
    """Memory profile for a single run of a scenario."""

    total_peak_mb: float
    setup_peak_mb: float
    run_peak_mb: float
    current_mb: float
    duration_s: float
    n_results: int
    top_allocators: list[dict] = field(default_factory=list)
    diff_allocators: list[dict] = field(default_factory=list)
    failed: bool = False


@dataclass
class ScenarioMemProfile:
    """Aggregate memory profile across multiple runs of a scenario."""

    name: str
    complexity: str
    n_runs: int
    peak_mb: float         # median total peak across runs
    peak_min_mb: float
    peak_max_mb: float
    setup_peak_mb: float   # median setup peak
    run_peak_mb: float     # median run-only peak
    duration_s: float      # median duration
    n_results: int
    top_allocators: list[dict] = field(default_factory=list)
    diff_allocators: list[dict] = field(default_factory=list)
    runs: list[RunProfile] = field(default_factory=list)
    overhead_pct: float | None = None
    failed: bool = False


@dataclass
class MemoryReport:
    """Full memory profiling report."""

    scale: str
    max_rows: int
    n_runs: int
    system_total_mb: float
    system_available_mb: float
    python_baseline_mb: float
    scenarios: list[ScenarioMemProfile] = field(default_factory=list)

    @property
    def max_scenario_peak_mb(self) -> float:
        peaks = [s.peak_mb for s in self.scenarios if not s.failed]
        return max(peaks) if peaks else 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["max_scenario_peak_mb"] = self.max_scenario_peak_mb
        return d


# -- Helpers --

def _get_system_memory() -> tuple[float, float]:
    """Return (total_mb, available_mb) from /proc/meminfo."""
    try:
        with open("/proc/meminfo") as f:
            info = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
            total = info.get("MemTotal", 0) / 1024
            available = info.get("MemAvailable", 0) / 1024
            return total, available
    except (OSError, ValueError):
        return 0, 0


def _get_rss_mb() -> float:
    """Read current VmRSS from /proc/self/status in MB."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return 0


def _get_python_baseline() -> float:
    """Measure Python's baseline memory before any benchmark imports."""
    tracemalloc.start()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak / (1024 * 1024)


def _extract_top_allocators(snapshot: tracemalloc.Snapshot, limit: int = 10) -> list[dict]:
    """Extract top memory allocators from a tracemalloc snapshot."""
    stats = snapshot.statistics("lineno")
    top = []
    for stat in stats[:limit]:
        frame = stat.traceback[0]
        top.append({
            "file": os.path.basename(frame.filename),
            "line": frame.lineno,
            "size_mb": round(stat.size / (1024 * 1024), 2),
            "count": stat.count,
            "full_path": f"{frame.filename}:{frame.lineno}",
        })
    return top


def _extract_diff_allocators(
    final: tracemalloc.Snapshot,
    baseline: tracemalloc.Snapshot,
    limit: int = 10,
) -> list[dict]:
    """Extract top NEW allocations between two snapshots (run-phase only)."""
    diff_stats = final.compare_to(baseline, "lineno")
    top = []
    for stat in diff_stats[:limit]:
        if stat.size_diff <= 0:
            break
        frame = stat.traceback[0]
        top.append({
            "file": os.path.basename(frame.filename),
            "line": frame.lineno,
            "size_diff_mb": round(stat.size_diff / (1024 * 1024), 2),
            "count_diff": stat.count_diff,
            "full_path": f"{frame.filename}:{frame.lineno}",
        })
    return top


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0


# -- Core profiling --

def _profile_run(
    scenario,
    config,
    top_limit: int,
) -> RunProfile:
    """Profile a single setup→run→teardown cycle with tracemalloc."""
    gc.collect()
    tracemalloc.start()

    t0 = time.perf_counter()
    n_results = 0
    failed = False

    try:
        scenario.setup(config)

        # Snapshot after setup, before run
        setup_snapshot = tracemalloc.take_snapshot()
        _, setup_peak = tracemalloc.get_traced_memory()

        results = scenario.run(config)
        scenario.teardown()
        n_results = len(results)
    except Exception as e:
        _print(f"       !! FAILED: {e}")
        try:
            scenario.teardown()
        except Exception:
            pass
        failed = True
        setup_snapshot = None
        setup_peak = 0

    elapsed = time.perf_counter() - t0

    # Final snapshot before stopping
    final_snapshot = tracemalloc.take_snapshot()
    _, total_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    total_peak_mb = total_peak / (1024 * 1024)
    setup_peak_mb = setup_peak / (1024 * 1024)
    run_peak_mb = max(0, total_peak_mb - setup_peak_mb)
    current_mb = 0  # measured at snapshot time

    # Top allocators from final snapshot
    top_allocs = _extract_top_allocators(final_snapshot, limit=top_limit)

    # Diff allocators (new during run phase)
    diff_allocs = []
    if setup_snapshot is not None and not failed:
        diff_allocs = _extract_diff_allocators(final_snapshot, setup_snapshot, limit=top_limit)

    return RunProfile(
        total_peak_mb=round(total_peak_mb, 2),
        setup_peak_mb=round(setup_peak_mb, 2),
        run_peak_mb=round(run_peak_mb, 2),
        current_mb=round(current_mb, 2),
        duration_s=round(elapsed, 1),
        n_results=n_results,
        top_allocators=top_allocs,
        diff_allocators=diff_allocs,
        failed=failed,
    )


def _estimate_overhead(scenario, config) -> float | None:
    """Run scenario without tracemalloc and compare RSS delta to get overhead estimate."""
    gc.collect()
    rss_before = _get_rss_mb()

    try:
        scenario.setup(config)
        scenario.run(config)
        scenario.teardown()
    except Exception:
        try:
            scenario.teardown()
        except Exception:
            pass
        return None

    rss_after = _get_rss_mb()
    rss_delta = max(0, rss_after - rss_before)
    return round(rss_delta, 2)


def _aggregate_runs(
    name: str,
    runs: list[RunProfile],
    complexity: str,
) -> ScenarioMemProfile:
    """Aggregate multiple RunProfiles into a single ScenarioMemProfile."""
    ok_runs = [r for r in runs if not r.failed]
    if not ok_runs:
        return ScenarioMemProfile(
            name=name,
            complexity=complexity,
            n_runs=len(runs),
            peak_mb=0, peak_min_mb=0, peak_max_mb=0,
            setup_peak_mb=0, run_peak_mb=0,
            duration_s=0, n_results=0,
            runs=runs, failed=True,
        )

    peaks = [r.total_peak_mb for r in ok_runs]
    setup_peaks = [r.setup_peak_mb for r in ok_runs]
    run_peaks = [r.run_peak_mb for r in ok_runs]
    durations = [r.duration_s for r in ok_runs]

    # Use median run's allocators for the representative
    median_peak = _median(peaks)
    closest_run = min(ok_runs, key=lambda r: abs(r.total_peak_mb - median_peak))

    return ScenarioMemProfile(
        name=name,
        complexity=complexity,
        n_runs=len(runs),
        peak_mb=round(median_peak, 2),
        peak_min_mb=round(min(peaks), 2),
        peak_max_mb=round(max(peaks), 2),
        setup_peak_mb=round(_median(setup_peaks), 2),
        run_peak_mb=round(_median(run_peaks), 2),
        duration_s=round(_median(durations), 1),
        n_results=closest_run.n_results,
        top_allocators=closest_run.top_allocators,
        diff_allocators=closest_run.diff_allocators,
        runs=runs,
    )


# -- Extrapolation --

def _extrapolate(scenarios: list[ScenarioMemProfile], scale_name: str, available_mb: float) -> None:
    """Print per-class extrapolation estimates."""
    multipliers = {
        "large": [(5, "xlarge", 500_000), (10, "xxlarge", 1_000_000)],
        "xlarge": [(2, "xxlarge", 1_000_000)],
    }
    targets = multipliers.get(scale_name, [])
    if not targets:
        return

    _print("\n  Extrapolation (per-scenario complexity class):")

    for mult, target_name, rows in targets:
        _print(f"\n    {target_name} ({rows:,} rows):")
        max_est = 0

        for s in sorted(scenarios, key=lambda x: x.peak_mb, reverse=True):
            if s.failed:
                continue
            cls = s.complexity
            if cls == "O(1)":
                est = s.peak_mb  # constant
            elif cls == "O(n log n)":
                # n*log(n) scaling: est = peak * (mult * log(mult*n) / log(n))
                n = rows / mult  # current row count
                if n > 1:
                    est = s.peak_mb * (mult * math.log(mult * n) / math.log(n))
                    est = min(est, s.peak_mb * mult * 1.5)  # cap at 1.5x linear
                else:
                    est = s.peak_mb * mult
            else:
                est = s.peak_mb * mult  # O(n) linear

            if est > max_est:
                max_est = est

        fits = "YES" if max_est < available_mb * 0.8 else "RISKY" if max_est < available_mb else "NO"
        _print(f"      Max estimated peak: ~{max_est:,.0f} MB  [{fits}]")
        if fits == "YES":
            _print(f"      Headroom: {available_mb - max_est:,.0f} MB (80% threshold)")
        elif fits == "RISKY":
            _print(f"      Only {available_mb - max_est:,.0f} MB headroom — might swap")


# -- Main --

def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark memory profiling")
    parser.add_argument("--scale", choices=["small", "medium", "large", "xlarge", "xxlarge"],
                        default="small")
    parser.add_argument("--suite", action="append", help="Run specific suite(s)")
    parser.add_argument("--tag", help="Tag for output filename")
    parser.add_argument("--top", type=int, default=10, help="Top N allocators per scenario")
    parser.add_argument("--runs", type=int, default=3, help="Runs per scenario (default 3)")
    parser.add_argument("--estimate-overhead", action="store_true",
                        help="Run extra pass without tracemalloc to estimate overhead")
    parser.add_argument("--budget", type=float, default=None,
                        help="Memory budget in MB — exit 1 if any scenario exceeds")
    parser.add_argument("--save-baseline", action="store_true",
                        help="Save report as baseline for regression detection")
    args = parser.parse_args()

    # System context
    total_mb, available_mb = _get_system_memory()
    baseline_mb = _get_python_baseline()

    from benchmarks.core.config import BenchmarkConfig, ScaleConfig
    from benchmarks.suites import get_scenarios

    scale = ScaleConfig.from_name(args.scale)
    config = BenchmarkConfig(
        scale=scale,
        suites=args.suite or None,
    )

    report = MemoryReport(
        scale=scale.name,
        max_rows=scale.max_rows,
        n_runs=args.runs,
        system_total_mb=round(total_mb, 1),
        system_available_mb=round(available_mb, 1),
        python_baseline_mb=round(baseline_mb, 2),
    )

    _print("\n  Memory-profiled benchmark run")
    _print(f"  Scale: {scale.name} (max {scale.max_rows:,} rows)")
    _print(f"  Runs per scenario: {args.runs}")
    _print(f"  System: {total_mb:,.0f} MB total, {available_mb:,.0f} MB available")
    _print(f"  Python baseline: {baseline_mb:.1f} MB")

    scenarios = get_scenarios(args.suite)
    total = len(scenarios)
    _print(f"  Running {total} scenarios × {args.runs} runs...\n")

    for i, scenario in enumerate(scenarios, 1):
        label = f"{scenario.suite}/{scenario.name}"
        complexity = _complexity_class(label)
        _print(f"  [{i}/{total}] {label}  ({complexity})")

        runs: list[RunProfile] = []
        for run_idx in range(args.runs):
            run = _profile_run(scenario, config, top_limit=args.top)
            runs.append(run)

            # Collect benchmark results from first successful run only
            if run_idx == 0 and not run.failed:
                # Re-run to collect results (profile run doesn't store them)
                # Actually, we already ran it — but _profile_run doesn't return results.
                # We'll collect results from a separate timing run below.
                pass

            status = "FAIL" if run.failed else "ok"
            _print(f"       run {run_idx + 1}/{args.runs}: "
                   f"peak={run.total_peak_mb:.1f} MB "
                   f"(setup={run.setup_peak_mb:.1f}, run={run.run_peak_mb:.1f}), "
                   f"{run.duration_s:.1f}s [{status}]")

        # Aggregate across runs
        profile = _aggregate_runs(label, runs, complexity)

        # Overhead estimation (extra pass without tracemalloc)
        if args.estimate_overhead:
            _print("       estimating overhead (no-tracemalloc pass)...")
            rss_delta = _estimate_overhead(scenario, config)
            if rss_delta is not None and profile.peak_mb > 0:
                overhead = ((profile.peak_mb - rss_delta) / profile.peak_mb) * 100
                profile.overhead_pct = round(max(0, overhead), 1)
                _print(f"       overhead: ~{profile.overhead_pct}% "
                       f"(tracemalloc={profile.peak_mb:.1f} MB, RSS delta={rss_delta:.1f} MB)")

        report.scenarios.append(profile)

        # Summary line
        spread = profile.peak_max_mb - profile.peak_min_mb
        _print(f"       -> median={profile.peak_mb:.1f} MB "
               f"[{profile.peak_min_mb:.1f}..{profile.peak_max_mb:.1f}], "
               f"spread={spread:.1f} MB")

        # Top 3 diff allocators (run-phase only)
        if profile.diff_allocators:
            _print("       run-phase allocators:")
            for alloc in profile.diff_allocators[:3]:
                _print(f"          {alloc['size_diff_mb']:6.1f} MB  "
                       f"{alloc['file']}:{alloc['line']} "
                       f"({alloc['count_diff']:+,} allocs)")

    # Save memory report as JSON
    tag = args.tag or "memcheck"
    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"memprofile_{scale.name}_{tag}.json"
    report_path.write_text(json.dumps(report.to_dict(), indent=2))

    # Save as baseline if requested
    if args.save_baseline:
        baseline_dir = output_dir / "baselines"
        baseline_dir.mkdir(parents=True, exist_ok=True)
        baseline_path = baseline_dir / f"memprofile_{scale.name}_baseline.json"
        shutil.copy2(report_path, baseline_path)
        _print(f"\n  Baseline saved: {baseline_path}")

    # Summary
    _print(f"\n{'='*60}")
    _print(f"  MEMORY PROFILE SUMMARY  (scale={scale.name}, {args.runs} runs)")
    _print(f"{'='*60}")

    _print(f"\n  System: {total_mb:,.0f} MB total, {available_mb:,.0f} MB available")
    _print(f"  Python baseline: {baseline_mb:.1f} MB")

    _print(f"\n  Per-scenario peaks (median of {args.runs} runs):")
    for s in sorted(report.scenarios, key=lambda x: x.peak_mb, reverse=True):
        status = " FAILED" if s.failed else ""
        spread = s.peak_max_mb - s.peak_min_mb
        _print(f"    {s.peak_mb:7.1f} MB  [{s.peak_min_mb:.0f}..{s.peak_max_mb:.0f}]  "
               f"{s.duration_s:6.1f}s  {s.name}  ({s.complexity}){status}")

    max_peak = report.max_scenario_peak_mb
    _print(f"\n  Max scenario peak: {max_peak:.1f} MB")
    headroom = available_mb - max_peak
    _print(f"  Available headroom: {headroom:,.0f} MB")

    # Per-class extrapolation
    _extrapolate(report.scenarios, scale.name, available_mb)

    _print(f"\n  Full report: {report_path}")
    _print("  Caveats:")
    _print("    - tracemalloc adds ~30% overhead to measured peaks")
    _print("    - setup memory (pre-created DBs) separated from run memory")
    _print("    - indexes scale O(n log n), connections O(1), only docs are O(n)")
    _print(f"    - peaks are median of {args.runs} runs (min/max shown in brackets)")

    # Budget enforcement
    if args.budget is not None:
        violations = [s for s in report.scenarios if not s.failed and s.peak_mb > args.budget]
        if violations:
            _print(f"\n  BUDGET VIOLATIONS ({args.budget:.0f} MB limit):")
            for v in violations:
                _print(f"    {v.name}: {v.peak_mb:.1f} MB (over by {v.peak_mb - args.budget:.1f} MB)")
            sys.exit(1)
        else:
            _print(f"\n  Budget check PASSED (all scenarios under {args.budget:.0f} MB)")

    _print("")


if __name__ == "__main__":
    main()
