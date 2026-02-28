"""Memory profile regression detection.

Compares two memprofile JSON reports and flags scenarios
that regressed beyond a threshold percentage.

Usage:
    uv run --group benchmarks python -m benchmarks.memcompare \\
        benchmarks/results/baselines/memprofile_large_baseline.json \\
        benchmarks/results/memprofile_large_memcheck.json \\
        --threshold 10
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def compare(baseline_path: str, current_path: str, threshold_pct: float = 10) -> list[dict]:
    """Compare two memprofile reports and return regressions."""
    baseline = json.loads(Path(baseline_path).read_text())
    current = json.loads(Path(current_path).read_text())

    baseline_by_name = {s["name"]: s for s in baseline["scenarios"]}
    regressions = []

    for scenario in current["scenarios"]:
        name = scenario["name"]
        if name not in baseline_by_name:
            continue

        base_peak = baseline_by_name[name]["peak_mb"]
        curr_peak = scenario["peak_mb"]

        if base_peak > 0:
            delta_pct = ((curr_peak - base_peak) / base_peak) * 100
        elif curr_peak > 0:
            delta_pct = float("inf")
        else:
            delta_pct = 0

        regressions.append({
            "name": name,
            "baseline_mb": base_peak,
            "current_mb": curr_peak,
            "delta_mb": round(curr_peak - base_peak, 2),
            "delta_pct": round(delta_pct, 1),
            "regressed": delta_pct > threshold_pct,
        })

    return regressions


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare memory profiles for regressions")
    parser.add_argument("baseline", help="Path to baseline memprofile JSON")
    parser.add_argument("current", help="Path to current memprofile JSON")
    parser.add_argument("--threshold", type=float, default=10,
                        help="Regression threshold in %% (default 10)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if not Path(args.baseline).exists():
        print(f"Baseline not found: {args.baseline}", file=sys.stderr)
        sys.exit(2)
    if not Path(args.current).exists():
        print(f"Current report not found: {args.current}", file=sys.stderr)
        sys.exit(2)

    results = compare(args.baseline, args.current, args.threshold)
    regressed = [r for r in results if r["regressed"]]

    if args.json:
        print(json.dumps({"threshold_pct": args.threshold, "results": results}, indent=2))
    else:
        print(f"\n  Memory Regression Report (threshold: {args.threshold}%)")
        print(f"  {'='*60}")
        print(f"\n  {'Scenario':<35} {'Base MB':>8} {'Curr MB':>8} {'Delta':>8} {'Status':>10}")
        print(f"  {'-'*35} {'-'*8} {'-'*8} {'-'*8} {'-'*10}")

        for r in sorted(results, key=lambda x: x["delta_pct"], reverse=True):
            status = "REGRESSED" if r["regressed"] else "ok"
            sign = "+" if r["delta_pct"] > 0 else ""
            print(f"  {r['name']:<35} {r['baseline_mb']:8.1f} {r['current_mb']:8.1f} "
                  f"{sign}{r['delta_pct']:6.1f}%  {status:>10}")

        print()
        if regressed:
            print(f"  {len(regressed)} regression(s) found (>{args.threshold}% increase)")
        else:
            print(f"  No regressions (all within {args.threshold}% threshold)")
        print()

    sys.exit(1 if regressed else 0)


if __name__ == "__main__":
    main()
