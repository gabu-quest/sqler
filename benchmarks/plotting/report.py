"""Report generator — creates markdown report with charts from benchmark results."""

from __future__ import annotations

import json
import math
from pathlib import Path

from .charts import (
    plot_comparison_bars,
    plot_heatmap,
    plot_scaling_lines,
    plot_throughput_comparison,
)
from .theme import apply_dark_theme


def generate_report(input_path: str, output_dir: str) -> None:
    """Generate full benchmark report with charts and markdown."""
    data = json.loads(Path(input_path).read_text())
    results = data["results"]
    system_info = _format_system_info(data.get("system", {}))
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    charts_dir = out / "charts"
    charts_dir.mkdir(exist_ok=True)

    apply_dark_theme()

    chart_paths: dict[str, Path] = {}

    # Group results by scenario
    by_scenario: dict[str, list[dict]] = {}
    for r in results:
        scenario = r["scenario"]
        if scenario not in by_scenario:
            by_scenario[scenario] = []
        by_scenario[scenario].append(r)

    # --- Insert Suite Charts ---
    if "bulk_insert_scaling" in by_scenario:
        chart_paths["bulk_scaling"] = plot_scaling_lines(
            by_scenario["bulk_insert_scaling"],
            "Bulk Insert Scaling — bulk_upsert() vs sqlite3 executemany",
            system_info,
            charts_dir / "01_bulk_scaling",
            xlabel="Rows", ylabel="Time (ms)",
        )

    if "single_vs_bulk" in by_scenario:
        chart_paths["single_vs_bulk"] = plot_comparison_bars(
            by_scenario["single_vs_bulk"],
            "Single Save vs Bulk Upsert — sqler vs sqlite3",
            system_info,
            charts_dir / "02_single_vs_bulk",
            show_throughput=True,
        )

    if "doc_size_impact" in by_scenario:
        chart_paths["doc_sizes"] = plot_comparison_bars(
            by_scenario["doc_size_impact"],
            "Document Size Impact — sqler vs sqlite3",
            system_info,
            charts_dir / "03_doc_sizes",
            show_throughput=True,
        )

    if "model_overhead" in by_scenario:
        chart_paths["model_overhead"] = plot_throughput_comparison(
            by_scenario["model_overhead"],
            "Model Overhead — raw vs Pydantic vs Lite vs sqlite3",
            system_info,
            charts_dir / "04_model_overhead",
        )

    # --- Query Suite Charts ---
    if "equality_filter" in by_scenario:
        chart_paths["equality"] = plot_scaling_lines(
            by_scenario["equality_filter"],
            "Equality Filter ± Index — sqler vs sqlite3",
            system_info,
            charts_dir / "05_equality_filter",
        )

    if "range_queries" in by_scenario:
        chart_paths["range"] = plot_comparison_bars(
            by_scenario["range_queries"],
            "Range Query by Selectivity — sqler vs sqlite3",
            system_info,
            charts_dir / "06_range_queries",
        )

    if "complex_filters" in by_scenario:
        chart_paths["complex"] = plot_comparison_bars(
            by_scenario["complex_filters"],
            "Complex Filter Chains — sqler vs sqlite3",
            system_info,
            charts_dir / "07_complex_filters",
        )

    if "top_n" in by_scenario:
        chart_paths["top_n"] = plot_comparison_bars(
            by_scenario["top_n"],
            "Top-N Query — sqler vs sqlite3",
            system_info,
            charts_dir / "08_top_n",
        )

    if "pagination_depth" in by_scenario:
        chart_paths["pagination"] = plot_scaling_lines(
            by_scenario["pagination_depth"],
            "Pagination — sqler vs sqlite3",
            system_info,
            charts_dir / "09_pagination",
        )

    if "count_vs_materialize" in by_scenario:
        chart_paths["count_vs_mat"] = plot_comparison_bars(
            by_scenario["count_vs_materialize"],
            "Count vs Materialize — sqler vs sqlite3",
            system_info,
            charts_dir / "10_count_vs_materialize",
        )

    # --- JSON Suite Charts ---
    if "nested_field_access" in by_scenario:
        chart_paths["nested"] = plot_comparison_bars(
            by_scenario["nested_field_access"],
            "Nested JSON Field Access — sqler vs sqlite3",
            system_info,
            charts_dir / "11_nested_access",
        )

    if "array_contains_isin" in by_scenario:
        chart_paths["array_ops"] = plot_scaling_lines(
            by_scenario["array_contains_isin"],
            "Array Operations — sqler vs sqlite3",
            system_info,
            charts_dir / "12_array_ops",
        )

    if "any_where" in by_scenario:
        chart_paths["any_where"] = plot_scaling_lines(
            by_scenario["any_where"],
            "any().where() — sqler vs sqlite3",
            system_info,
            charts_dir / "13_any_where",
        )

    # --- Advanced Suite Charts ---
    if "full_text_search" in by_scenario:
        chart_paths["fts"] = plot_comparison_bars(
            by_scenario["full_text_search"],
            "Full-Text Search — sqler vs sqlite3 FTS5",
            system_info,
            charts_dir / "14_fts",
        )

    if "optimistic_locking" in by_scenario:
        chart_paths["locking"] = plot_comparison_bars(
            by_scenario["optimistic_locking"],
            "Optimistic Locking — sqler vs sqlite3",
            system_info,
            charts_dir / "15_optimistic_locking",
            show_throughput=True,
        )

    if "query_cache" in by_scenario:
        chart_paths["cache"] = plot_comparison_bars(
            by_scenario["query_cache"],
            "Query Cache — sqler vs sqlite3 Manual Cache",
            system_info,
            charts_dir / "16_query_cache",
        )

    if "connection_pool" in by_scenario:
        chart_paths["pool"] = plot_comparison_bars(
            by_scenario["connection_pool"],
            "Connection Pool — sqler vs sqlite3",
            system_info,
            charts_dir / "17_connection_pool",
            show_throughput=True,
        )

    # --- Ops Suite Charts ---
    if "index_creation" in by_scenario:
        chart_paths["index"] = plot_scaling_lines(
            by_scenario["index_creation"],
            "Index Creation — sqler vs sqlite3",
            system_info,
            charts_dir / "18_index_creation",
        )

    if "cold_vs_warm" in by_scenario:
        chart_paths["cold_warm"] = plot_comparison_bars(
            by_scenario["cold_vs_warm"],
            "Cold vs Warm Query — sqler vs sqlite3",
            system_info,
            charts_dir / "19_cold_vs_warm",
        )

    if "export_performance" in by_scenario:
        chart_paths["export"] = plot_comparison_bars(
            by_scenario["export_performance"],
            "Export Performance — sqler vs sqlite3",
            system_info,
            charts_dir / "20_export",
        )

    if "backup_restore" in by_scenario:
        chart_paths["backup"] = plot_comparison_bars(
            by_scenario["backup_restore"],
            "Backup & Restore — sqler vs sqlite3",
            system_info,
            charts_dir / "21_backup_restore",
        )

    if "aggregates" in by_scenario:
        # Build heatmap data: rows = aggregate types, cols = table sizes
        # Generate side-by-side heatmaps for sqler and sqlite
        agg_results = by_scenario["aggregates"]

        for prefix, label in [("sqler", "sqler"), ("sqlite", "sqlite3")]:
            agg_types: list[str] = []
            sizes_seen: list[int] = []
            agg_data: dict[str, dict[int, float]] = {}

            for r in agg_results:
                val = str(r["value"])
                if not val.startswith(f"{prefix}_"):
                    continue
                # Parse: sqler_sum_1000 -> agg_type=sum, size=1000
                rest = val.removeprefix(f"{prefix}_")
                parts = rest.rsplit("_", 1)
                if len(parts) == 2:
                    agg_type, size_str = parts
                    try:
                        size = int(size_str)
                    except ValueError:
                        continue
                    if agg_type not in agg_data:
                        agg_data[agg_type] = {}
                        agg_types.append(agg_type)
                    if size not in sizes_seen:
                        sizes_seen.append(size)
                    agg_data[agg_type][size] = r["timing"]["median_ms"]

            if agg_data:
                sizes_seen.sort()
                heatmap_data = []
                for at in agg_types:
                    row = [agg_data[at].get(s, 0) for s in sizes_seen]
                    heatmap_data.append(row)

                chart_key = f"aggregates_{prefix}"
                chart_paths[chart_key] = plot_heatmap(
                    heatmap_data,
                    agg_types,
                    [f"{s:,}" for s in sizes_seen],
                    f"Aggregate Performance — {label}",
                    system_info,
                    charts_dir / f"22_aggregates_{prefix}",
                )

    # --- Generate Markdown Report ---
    _write_markdown_report(out / "REPORT.md", data, chart_paths, charts_dir)

    print("\n  Report generated:")
    print(f"    Markdown: {out / 'REPORT.md'}")
    print(f"    Charts:   {charts_dir}/ ({len(chart_paths)} charts)")


def _format_system_info(info: dict) -> str:
    if not info:
        return ""
    return (
        f"Python {info.get('python_version', '?')} | "
        f"sqler {info.get('sqler_version', '?')} | "
        f"SQLite {info.get('sqlite_version', '?')} | "
        f"{info.get('platform_system', '?')} {info.get('platform_machine', '?')} "
        f"({info.get('cpu_count', '?')} cores)"
    )


def _fmt_p95(val) -> str:
    """Format p95 value, handling NaN."""
    if isinstance(val, float) and math.isnan(val):
        return "\u2014"
    return f"{val:.2f}"


def _write_markdown_report(path: Path, data: dict, chart_paths: dict[str, Path],
                            charts_dir: Path) -> None:
    """Write the final markdown report."""
    system = data.get("system", {})
    config = data.get("config", {})
    summary = data.get("summary", {})
    results = data.get("results", [])

    lines = [
        "# sqler Benchmark Report",
        "",
        f"**Scale**: {config.get('scale', 'unknown')} | "
        f"**Scenarios**: {summary.get('total_scenarios', '?')} | "
        f"**Measurements**: {summary.get('total_measurements', '?')}",
        "",
        f"> {_format_system_info(system)}",
        "",
    ]

    # Summary table
    lines.extend([
        "## Summary",
        "",
        "| Suite | Scenario | Parameter | Median (ms) | P95 (ms) | Throughput |",
        "|-------|----------|-----------|-------------|----------|------------|",
    ])

    for r in results:
        tp = f"{r.get('throughput', 0):,.0f}/s" if r.get("throughput", 0) > 0 else "\u2014"
        p95_str = _fmt_p95(r["timing"]["p95_ms"])
        lines.append(
            f"| {r['suite']} | {r['scenario']} | "
            f"{r.get('value', '')} | "
            f"{r['timing']['median_ms']:.2f} | "
            f"{p95_str} | "
            f"{tp} |"
        )

    lines.append("")

    # Reading the charts
    lines.extend([
        "## Reading the Charts",
        "",
        "Every benchmark runs multiple iterations. The numbers you see are:",
        "",
        "- **Median** \u2014 the middle value across all iterations. "
        "Half the runs were faster, half were slower. "
        "More stable than the mean because a single slow run (GC pause, OS scheduling) doesn't skew it.",
        "- **P95 (95th percentile)** \u2014 95% of runs finished at or below this time. "
        "This is your realistic worst-case: the latency your users will occasionally hit "
        "but not often enough to show up in the median.",
        "- **Shaded bands** (on scaling line charts) \u2014 the area between median and p95. "
        "A narrow band means the operation is predictable. "
        "A wide band means variance is high \u2014 expect occasional slow runs, "
        "often from SQLite page cache misses, GC pauses, or OS-level contention.",
        "- **Error caps** (on bar charts) \u2014 the vertical whisker above each bar extends to p95. "
        "Same interpretation: how much slower than the median can a single run get.",
        "- **Orange bars** = sqler, **Blue bars** = raw sqlite3 baseline.",
        "",
    ])

    # Charts section
    lines.extend(["## Charts", ""])

    chart_titles = {
        "bulk_scaling": "Bulk Insert Scaling",
        "single_vs_bulk": "Single Save vs Bulk Upsert",
        "doc_sizes": "Document Size Impact",
        "model_overhead": "Model Overhead",
        "equality": "Equality Filter \u00b1 Index",
        "range": "Range Queries",
        "complex": "Complex Filter Chains",
        "top_n": "Top-N Queries",
        "pagination": "Pagination Depth",
        "count_vs_mat": "Count vs Materialize",
        "nested": "Nested JSON Field Access",
        "array_ops": "Array Operations",
        "any_where": "any().where() Queries",
        "fts": "Full-Text Search",
        "locking": "Optimistic Locking",
        "cache": "Query Cache",
        "pool": "Connection Pool",
        "index": "Index Creation",
        "cold_warm": "Cold vs Warm",
        "export": "Export Performance",
        "backup": "Backup & Restore",
        "aggregates_sqler": "Aggregate Performance (sqler)",
        "aggregates_sqlite": "Aggregate Performance (sqlite3)",
    }

    for key, chart_path in chart_paths.items():
        title = chart_titles.get(key, key)
        rel_path = chart_path.relative_to(charts_dir.parent) if chart_path else ""
        lines.extend([
            f"### {title}",
            "",
            f"![{title}]({rel_path})",
            "",
        ])

    # Methodology caveat
    lines.extend([
        "## Methodology Caveats (v1.1)",
        "",
        "> **These results are not apples-to-apples for measuring ORM overhead.**",
        ">",
        "> Two known biases favour sqler in v1.1 data:",
        ">",
        "> 1. **PRAGMA mismatch** \u2014 sqler's `in_memory()` applies tuned PRAGMAs "
        "(32 MB cache, `synchronous=OFF`, `journal_mode=MEMORY`, `locking_mode=EXCLUSIVE`) "
        "while the sqlite3 baseline uses SQLite defaults (2 MB cache, `synchronous=FULL`). "
        "This alone explains most of sqler's apparent query speed advantage.",
        "> 2. **Array SQL difference** \u2014 sqler's `contains()`/`isin()` generate "
        "`json_each(data, '$.path')` (direct path, faster) while the baseline uses "
        "`json_each(json_extract(data, '$.path'))` (extract then iterate, slower).",
        ">",
        "> The v1.1 data is still useful for showing sqler-with-tuning vs naive-sqlite, "
        "but it does NOT isolate ORM overhead. A fair re-run with matched PRAGMAs and "
        "matched SQL is planned (see `TODO-SCRUTINY.md`).",
        "",
    ])

    # Future work
    lines.extend([
        "## Known Gaps / Future Work",
        "",
        "1. **Fair baseline comparison** \u2014 sqlite3 baseline needs matched PRAGMAs and matched SQL (see caveats above)",
        "2. **On-disk benchmarks** \u2014 all 22 scenarios currently run in-memory only (except 4 ops scenarios)",
        "3. **Result count verification** \u2014 no assertion that sqler and sqlite return same row counts",
        "4. **Deep nested JSON path equality** \u2014 `$.level_0.level_1.field` direct equality untested",
        "5. **Auxiliary inverted index tables** \u2014 No public API for array membership tables",
        "",
        "---",
        "",
        "*Generated by sqler benchmark suite \u2014 real data, sqler vs sqlite3, no fiction.*",
    ])

    path.write_text("\n".join(lines))
