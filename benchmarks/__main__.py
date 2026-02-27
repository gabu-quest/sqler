"""CLI entry point: uv run python -m benchmarks"""

from __future__ import annotations

import argparse


def cmd_run(args: argparse.Namespace) -> None:
    """Run benchmark scenarios."""
    from benchmarks.core.config import BenchmarkConfig, ScaleConfig
    from benchmarks.core.runner import BenchmarkRunner
    from benchmarks.suites import get_scenarios

    scale = ScaleConfig.from_name(args.scale)
    config = BenchmarkConfig(
        scale=scale,
        warmup=args.warmup,
        iterations=args.iterations,
        output_dir=args.output,
        suites=args.suite or None,
        verbose=args.verbose,
    )

    print("\n  sqler benchmark suite")
    print(f"  Scale: {scale.name} (max {scale.max_rows:,} rows)")
    print(f"  Warmup: {config.warmup}, Iterations: {config.iterations}")

    runner = BenchmarkRunner(config)
    print(f"  {runner.system_info.summary_line()}\n")

    suites = args.suite if args.suite else None
    scenarios = get_scenarios(suites)
    print(f"  Running {len(scenarios)} scenarios...\n")

    results = runner.run(scenarios)
    path = runner.save_results(results, tag=args.tag)

    print(f"\n  Done! {len(results)} measurements saved to {path}")


def cmd_list(args: argparse.Namespace) -> None:
    """List available scenarios."""
    from benchmarks.suites import ALL_SUITES

    print("\n  Available benchmark suites:\n")
    for suite_name, scenarios in ALL_SUITES.items():
        print(f"  [{suite_name}]")
        for s in scenarios:
            print(f"    - {s.name}: {s.description}")
        print()


def cmd_plot(args: argparse.Namespace) -> None:
    """Generate charts from benchmark results."""
    from benchmarks.plotting.report import generate_report

    generate_report(args.input, args.output)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="benchmarks",
        description="sqler benchmark suite — real benchmarks using only public APIs",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    run_p = sub.add_parser("run", help="Run benchmark scenarios")
    run_p.add_argument("--scale", choices=["small", "medium", "large"], default="small")
    run_p.add_argument("--suite", action="append", help="Run specific suite(s)")
    run_p.add_argument("--warmup", type=int, default=3)
    run_p.add_argument("--iterations", type=int, default=20)
    run_p.add_argument("--output", default="benchmarks/results")
    run_p.add_argument("--tag", help="Tag for output filename")
    run_p.add_argument("--verbose", "-v", action="store_true")
    run_p.set_defaults(func=cmd_run)

    # list
    list_p = sub.add_parser("list", help="List available scenarios")
    list_p.set_defaults(func=cmd_list)

    # plot
    plot_p = sub.add_parser("plot", help="Generate charts from results")
    plot_p.add_argument("--input", "-i", default="benchmarks/results/latest.json")
    plot_p.add_argument("--output", "-o", default="benchmarks/results")
    plot_p.set_defaults(func=cmd_plot)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
