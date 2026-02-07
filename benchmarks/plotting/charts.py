"""Chart generators — scaling lines, comparison bars, heatmaps with p95 bands."""

from __future__ import annotations

import json
from pathlib import Path

from .theme import (
    ACCENT_COLORS,
    BG_CARD,
    BG_DARK,
    BG_PANEL,
    BORDER_COLOR,
    GRID_COLOR,
    PALETTE,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    apply_dark_theme,
    color,
)


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


def plot_scaling_lines(results: list[dict], title: str, system_info: str,
                       output: Path, xlabel: str = "Rows", ylabel: str = "Time (ms)") -> Path:
    """Line chart showing how a metric scales with parameter value.

    Groups results by scenario name. Each line = one scenario variant.
    Adds p95 bands as translucent fill.
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
        s["p95"].append(r["timing"]["p95_ms"])
        s["min"].append(r["timing"]["min_ms"])

    for i, (label, data) in enumerate(sorted(series.items())):
        c = color(i)
        # Sort by x value
        paired = sorted(zip(data["x"], data["median"], data["p95"], data["min"]))
        xs = [p[0] for p in paired]
        medians = [p[1] for p in paired]
        p95s = [p[2] for p in paired]
        mins = [p[3] for p in paired]

        ax.plot(xs, medians, "o-", color=c, label=f"{label} (median)", zorder=3)
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

    Each result becomes one bar. Colors cycle through the palette.
    """
    import matplotlib.pyplot as plt
    import numpy as np

    _ensure_theme()
    fig, ax = plt.subplots(figsize=(12, 7))

    labels = []
    medians = []
    p95s = []
    colors = []
    throughputs = []

    for i, r in enumerate(results):
        val = str(r.get("value", f"item_{i}"))
        labels.append(val.replace("_", "\n"))
        medians.append(r["timing"]["median_ms"])
        p95s.append(r["timing"]["p95_ms"])
        colors.append(color(i))
        throughputs.append(r.get("throughput", 0))

    x = np.arange(len(labels))
    width = 0.55

    # Main bars
    bars = ax.bar(x, medians, width, color=colors, edgecolor=[c + "88" for c in colors],
                  linewidth=1.5, zorder=3)

    # p95 error caps
    errors = [p - m for p, m in zip(p95s, medians)]
    ax.errorbar(x, medians, yerr=[([0] * len(errors)), errors], fmt="none",
                ecolor=TEXT_SECONDARY, capsize=6, capthick=1.5, zorder=4)

    _add_value_labels(ax, bars, fmt="{:.1f}ms")

    # Throughput annotations if requested
    if show_throughput and any(t > 0 for t in throughputs):
        for i, (bar, tp) in enumerate(zip(bars, throughputs)):
            if tp > 0:
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() * 0.5,
                    f"{tp:,.0f}/s",
                    ha="center", va="center",
                    fontsize=8, color=BG_DARK, fontweight="bold",
                )

    ax.set_xlabel("")
    ax.set_ylabel(ylabel)
    ax.set_title(title, pad=20)
    _subtitle(ax, system_info)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9)
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

    # Custom colormap: dark blue → orange → gold
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
        colors_list.append(color(i))

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
