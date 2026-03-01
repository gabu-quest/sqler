"""Chart generators — scaling lines, comparison bars, heatmaps with p95 bands.

v1.2: 4-arm support (sqler_mem, sqler_disk, sqlite_mem, sqlite_disk)
  - sqler = orange (#E69F00), sqlite = blue (#56B4E9)
  - memory = solid line, disk = dashed line
"""

from __future__ import annotations

import math
from pathlib import Path

from .theme import (
    BG_DARK,
    BG_PANEL,
    BORDER_COLOR,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    apply_dark_theme,
    color,
)

# Fixed colors for sqler vs sqlite grouped bars
SQLER_COLOR = "#E69F00"   # Orange (Okabe-Ito)
SQLITE_COLOR = "#56B4E9"  # Sky blue (Okabe-Ito)


def _ensure_theme():
    apply_dark_theme()


def _subtitle(ax, text: str):
    """Add a subtle system info subtitle below the title."""
    ax.text(
        0.5, 1.02, text,
        transform=ax.transAxes, ha="center", va="bottom",
        fontsize=8, color=TEXT_MUTED, style="italic",
    )


def _add_value_labels(ax, bars, fmt="{:.1f}", fontsize=8):
    """Add value labels on top of bar charts."""
    for bar in bars:
        height = bar.get_height()
        if height > 0:
            ax.text(
                bar.get_x() + bar.get_width() / 2, height,
                fmt.format(height),
                ha="center", va="bottom",
                fontsize=fontsize, color=TEXT_PRIMARY, fontweight="bold",
            )


def _arm_style(label: str) -> tuple[str, str]:
    """Determine color and linestyle for a series label.

    Returns (color, linestyle) based on:
      - sqler_* → orange, sqlite_* → blue
      - *_mem_* → solid, *_disk_* → dashed
    """
    if label.startswith("sqler"):
        c = SQLER_COLOR
    elif label.startswith("sqlite"):
        c = SQLITE_COLOR
    else:
        c = None  # caller should fallback to palette

    if "_disk_" in label or label.endswith("_disk"):
        ls = "--"
    else:
        ls = "-"

    return c, ls


def _detect_paired_results(results: list[dict]) -> bool:
    """Detect if results contain sqler_/sqlite_ prefix pairs."""
    values = {str(r.get("value", "")) for r in results}
    sqler_keys = {v for v in values if v.startswith("sqler_")}
    sqlite_keys = {v for v in values if v.startswith("sqlite_")}
    if not sqler_keys or not sqlite_keys:
        return False
    # Check there's at least one matching suffix
    sqler_suffixes = {v.removeprefix("sqler_") for v in sqler_keys}
    sqlite_suffixes = {v.removeprefix("sqlite_") for v in sqlite_keys}
    return bool(sqler_suffixes & sqlite_suffixes)


def plot_scaling_lines(results: list[dict], title: str, system_info: str,
                       output: Path, xlabel: str = "Rows", ylabel: str = "Time (ms)") -> Path:
    """Line chart showing how a metric scales with parameter value.

    Groups results by scenario name. Each line = one scenario variant.
    Adds p95 bands as translucent fill.

    v1.2: Detects _mem_/_disk_ for solid/dashed line styles.
    """
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker

    _ensure_theme()
    fig, ax = plt.subplots(figsize=(12, 7))

    # Group by unique series
    series: dict[str, dict] = {}
    for r in results:
        key = r.get("scenario", "unknown")
        val_str = str(r.get("value", ""))

        # Try to extract a numeric x-value and a series label
        if "_" in val_str:
            parts = val_str.rsplit("_", 1)
            try:
                x_val = int(parts[1])
                series_label = parts[0]
            except ValueError:
                x_val = r.get("rows", 0)
                series_label = val_str
        else:
            try:
                x_val = int(val_str)
            except (ValueError, TypeError):
                x_val = r.get("rows", 0)
            series_label = key

        if series_label not in series:
            series[series_label] = {"x": [], "median": [], "p95": [], "min": []}
        s = series[series_label]
        s["x"].append(x_val)
        s["median"].append(r["timing"]["median_ms"])
        p95 = r["timing"]["p95_ms"]
        # Handle NaN p95
        if isinstance(p95, float) and math.isnan(p95):
            s["p95"].append(r["timing"]["median_ms"])  # fallback to median
        else:
            s["p95"].append(p95)
        s["min"].append(r["timing"]["min_ms"])

    for i, (label, data) in enumerate(sorted(series.items())):
        c, ls = _arm_style(label)
        if c is None:
            c = color(i)

        # Sort by x value
        paired = sorted(zip(data["x"], data["median"], data["p95"], data["min"]))
        xs = [p[0] for p in paired]
        medians = [p[1] for p in paired]
        p95s = [p[2] for p in paired]

        marker = "o" if ls == "-" else "s"
        ax.plot(xs, medians, f"{marker}{ls}", color=c, label=f"{label} (median)", zorder=3)
        ax.fill_between(xs, medians, p95s, alpha=0.15, color=c, zorder=2)
        ax.plot(xs, p95s, "--", color=c, alpha=0.4, linewidth=1, zorder=2)

    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title, pad=20)
    _subtitle(ax, system_info)

    if all(x > 0 for data in series.values() for x in data["x"]):
        ax.set_xscale("log")
        ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{int(x):,}"))

    ax.legend(loc="upper left", framealpha=0.9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    # p95 band footnote
    fig.text(
        0.99, 0.01,
        "Solid line = median  |  Shaded band = p95 (95% of runs finish within this range)",
        ha="right", va="bottom", fontsize=7.5, color=TEXT_MUTED, style="italic",
    )

    svg_path = output.with_suffix(".svg")
    png_path = output.with_suffix(".png")
    fig.savefig(svg_path, format="svg")
    fig.savefig(png_path, format="png", dpi=200)
    plt.close(fig)

    return svg_path


def plot_comparison_bars(results: list[dict], title: str, system_info: str,
                         output: Path, ylabel: str = "Time (ms)",
                         show_throughput: bool = False) -> Path:
    """Grouped bar chart comparing different methods/approaches.

    Detects sqler_/sqlite_ prefix pairs and renders grouped bars when found.
    Falls back to single-bar layout when no pairs are detected.

    v1.2: Supports _mem_/_disk_ hatching to distinguish storage modes.
    """
    import matplotlib.pyplot as plt
    import numpy as np

    _ensure_theme()
    fig, ax = plt.subplots(figsize=(12, 7))

    paired = _detect_paired_results(results)

    if paired:
        # Grouped bar layout: two bars per parameter suffix
        result_map: dict[str, dict[str, dict]] = {}  # suffix -> {sqler: r, sqlite: r}
        for r in results:
            val = str(r.get("value", ""))
            if val.startswith("sqler_"):
                suffix = val.removeprefix("sqler_")
                result_map.setdefault(suffix, {})["sqler"] = r
            elif val.startswith("sqlite_"):
                suffix = val.removeprefix("sqlite_")
                result_map.setdefault(suffix, {})["sqlite"] = r

        suffixes = list(result_map.keys())
        x = np.arange(len(suffixes))
        width = 0.35

        sqler_medians = []
        sqler_p95s = []
        sqlite_medians = []
        sqlite_p95s = []
        sqler_tps = []
        sqlite_tps = []
        sqler_hatches = []
        sqlite_hatches = []

        for suffix in suffixes:
            pair = result_map[suffix]
            sq = pair.get("sqler", {})
            sl = pair.get("sqlite", {})

            sq_t = sq.get("timing", {})
            sl_t = sl.get("timing", {})

            sq_med = sq_t.get("median_ms", 0)
            sl_med = sl_t.get("median_ms", 0)
            sq_p95 = sq_t.get("p95_ms", sq_med)
            sl_p95 = sl_t.get("p95_ms", sl_med)

            # Handle NaN
            if isinstance(sq_p95, float) and math.isnan(sq_p95):
                sq_p95 = sq_med
            if isinstance(sl_p95, float) and math.isnan(sl_p95):
                sl_p95 = sl_med

            sqler_medians.append(sq_med)
            sqler_p95s.append(sq_p95)
            sqlite_medians.append(sl_med)
            sqlite_p95s.append(sl_p95)
            sqler_tps.append(sq.get("throughput", 0))
            sqlite_tps.append(sl.get("throughput", 0))

            # Hatch pattern for disk mode
            sqler_hatches.append("//" if "disk_" in suffix or suffix.startswith("disk") else "")
            sqlite_hatches.append("//" if "disk_" in suffix or suffix.startswith("disk") else "")

        bars1 = ax.bar(x - width / 2, sqler_medians, width, color=SQLER_COLOR,
                        edgecolor=SQLER_COLOR + "88", linewidth=1.5, zorder=3, label="sqler")
        bars2 = ax.bar(x + width / 2, sqlite_medians, width, color=SQLITE_COLOR,
                        edgecolor=SQLITE_COLOR + "88", linewidth=1.5, zorder=3, label="sqlite")

        # Apply hatching for disk bars
        for bar, hatch in zip(bars1, sqler_hatches):
            bar.set_hatch(hatch)
        for bar, hatch in zip(bars2, sqlite_hatches):
            bar.set_hatch(hatch)

        # p95 error caps
        sq_errors = [max(0, p - m) for p, m in zip(sqler_p95s, sqler_medians)]
        sl_errors = [max(0, p - m) for p, m in zip(sqlite_p95s, sqlite_medians)]

        ax.errorbar(x - width / 2, sqler_medians,
                     yerr=[[0] * len(sq_errors), sq_errors], fmt="none",
                     ecolor=TEXT_SECONDARY, capsize=4, capthick=1.5, zorder=4)
        ax.errorbar(x + width / 2, sqlite_medians,
                     yerr=[[0] * len(sl_errors), sl_errors], fmt="none",
                     ecolor=TEXT_SECONDARY, capsize=4, capthick=1.5, zorder=4)

        _add_value_labels(ax, bars1, fmt="{:.1f}ms")
        _add_value_labels(ax, bars2, fmt="{:.1f}ms")

        if show_throughput:
            for bar, tp in zip(bars1, sqler_tps):
                if tp > 0:
                    ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() * 0.5,
                            f"{tp:,.0f}/s", ha="center", va="center",
                            fontsize=7, color=BG_DARK, fontweight="bold")
            for bar, tp in zip(bars2, sqlite_tps):
                if tp > 0:
                    ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() * 0.5,
                            f"{tp:,.0f}/s", ha="center", va="center",
                            fontsize=7, color=BG_DARK, fontweight="bold")

        labels = [s.replace("_", "\n") for s in suffixes]
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=9)
        ax.legend(loc="upper right", framealpha=0.9)

    else:
        # Fallback: single-bar layout
        labels = []
        medians = []
        p95s = []
        colors = []
        throughputs = []

        for i, r in enumerate(results):
            val = str(r.get("value", f"item_{i}"))
            labels.append(val.replace("_", "\n"))
            medians.append(r["timing"]["median_ms"])
            p95 = r["timing"]["p95_ms"]
            if isinstance(p95, float) and math.isnan(p95):
                p95s.append(r["timing"]["median_ms"])
            else:
                p95s.append(p95)
            c, _ = _arm_style(val)
            colors.append(c if c else color(i))
            throughputs.append(r.get("throughput", 0))

        x = np.arange(len(labels))
        width = 0.55

        bars = ax.bar(x, medians, width, color=colors,
                       edgecolor=[c + "88" for c in colors],
                       linewidth=1.5, zorder=3)

        errors = [max(0, p - m) for p, m in zip(p95s, medians)]
        ax.errorbar(x, medians, yerr=[([0] * len(errors)), errors], fmt="none",
                     ecolor=TEXT_SECONDARY, capsize=6, capthick=1.5, zorder=4)

        _add_value_labels(ax, bars, fmt="{:.1f}ms")

        if show_throughput and any(t > 0 for t in throughputs):
            for bar, tp in zip(bars, throughputs):
                if tp > 0:
                    ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() * 0.5,
                            f"{tp:,.0f}/s", ha="center", va="center",
                            fontsize=8, color=BG_DARK, fontweight="bold")

        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=9)

    ax.set_xlabel("")
    ax.set_ylabel(ylabel)
    ax.set_title(title, pad=20)
    _subtitle(ax, system_info)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    fig.tight_layout()
    svg_path = output.with_suffix(".svg")
    png_path = output.with_suffix(".png")
    fig.savefig(svg_path, format="svg")
    fig.savefig(png_path, format="png", dpi=200)
    plt.close(fig)

    return svg_path


def plot_heatmap(data: list[list[float]], row_labels: list[str], col_labels: list[str],
                 title: str, system_info: str, output: Path,
                 value_fmt: str = "{:.1f}ms") -> Path:
    """Heatmap grid (e.g., aggregates x table sizes)."""
    import matplotlib.pyplot as plt
    import numpy as np

    _ensure_theme()
    fig, ax = plt.subplots(figsize=(max(8, len(col_labels) * 1.8), max(5, len(row_labels) * 0.8)))

    arr = np.array(data)

    # Custom colormap: dark blue -> orange -> gold
    from matplotlib.colors import LinearSegmentedColormap
    cmap = LinearSegmentedColormap.from_list(
        "sqler_heat",
        [BG_PANEL, "#0072B2", "#E69F00", "#FFD700"],
        N=256,
    )

    im = ax.imshow(arr, cmap=cmap, aspect="auto")

    ax.set_xticks(range(len(col_labels)))
    ax.set_yticks(range(len(row_labels)))
    ax.set_xticklabels(col_labels, fontsize=10)
    ax.set_yticklabels(row_labels, fontsize=10)

    # Add text annotations
    for i in range(len(row_labels)):
        for j in range(len(col_labels)):
            val = arr[i, j]
            text_color = BG_DARK if val > arr.mean() else TEXT_PRIMARY
            ax.text(j, i, value_fmt.format(val),
                    ha="center", va="center", fontsize=9,
                    color=text_color, fontweight="bold")

    ax.set_title(title, pad=20)
    _subtitle(ax, system_info)

    cbar = fig.colorbar(im, ax=ax, pad=0.02, shrink=0.8)
    cbar.ax.yaxis.set_tick_params(color=TEXT_SECONDARY)
    cbar.outline.set_edgecolor(BORDER_COLOR)

    fig.tight_layout()
    svg_path = output.with_suffix(".svg")
    png_path = output.with_suffix(".png")
    fig.savefig(svg_path, format="svg")
    fig.savefig(png_path, format="png", dpi=200)
    plt.close(fig)

    return svg_path


def plot_throughput_comparison(results: list[dict], title: str, system_info: str,
                               output: Path) -> Path:
    """Horizontal bar chart showing throughput (rows/sec) — great for insert comparisons."""
    import matplotlib.pyplot as plt
    import numpy as np

    _ensure_theme()
    fig, ax = plt.subplots(figsize=(12, max(5, len(results) * 0.6)))

    labels = []
    throughputs = []
    colors_list = []

    for i, r in enumerate(sorted(results, key=lambda x: x.get("throughput", 0))):
        val = str(r.get("value", f"item_{i}"))
        labels.append(val.replace("_", " "))
        throughputs.append(r.get("throughput", 0))
        c, _ = _arm_style(val)
        colors_list.append(c if c else color(i))

    y = np.arange(len(labels))
    bars = ax.barh(y, throughputs, height=0.6, color=colors_list,
                   edgecolor=[c + "88" for c in colors_list], linewidth=1.5, zorder=3)

    # Value labels
    for bar, tp in zip(bars, throughputs):
        if tp > 0:
            ax.text(
                bar.get_width() + max(throughputs) * 0.02,
                bar.get_y() + bar.get_height() / 2,
                f"{tp:,.0f} rows/s",
                ha="left", va="center",
                fontsize=9, color=TEXT_PRIMARY, fontweight="bold",
            )

    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=10)
    ax.set_xlabel("Throughput (rows/sec)")
    ax.set_title(title, pad=20)
    _subtitle(ax, system_info)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    fig.tight_layout()
    svg_path = output.with_suffix(".svg")
    png_path = output.with_suffix(".png")
    fig.savefig(svg_path, format="svg")
    fig.savefig(png_path, format="png", dpi=200)
    plt.close(fig)

    return svg_path
