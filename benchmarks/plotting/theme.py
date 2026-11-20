"""Dark theme + Okabe-Ito colorblind-safe palette for gorgeous charts."""

from __future__ import annotations

# Okabe-Ito colorblind-safe palette — 8 distinct colors
# https://jfly.uni-koeln.de/color/
PALETTE = [
    "#E69F00",  # Orange
    "#56B4E9",  # Sky blue
    "#009E73",  # Bluish green
    "#F0E442",  # Yellow
    "#0072B2",  # Blue
    "#D55E00",  # Vermillion
    "#CC79A7",  # Reddish purple
    "#999999",  # Grey
]

# Extended palette for scenarios needing more colors
ACCENT_COLORS = {
    "highlight": "#FFD700",  # Gold
    "danger": "#FF4444",  # Red
    "success": "#00CC66",  # Green
    "muted": "#666666",  # Dark grey
}

# Dark theme colors
BG_DARK = "#1a1a2e"
BG_PANEL = "#16213e"
BG_CARD = "#0f3460"
TEXT_PRIMARY = "#e8e8e8"
TEXT_SECONDARY = "#a0a0a0"
TEXT_MUTED = "#707070"
GRID_COLOR = "#2a2a4a"
BORDER_COLOR = "#3a3a5a"

# Gradient accent for headers/highlights
GRADIENT_START = "#E69F00"
GRADIENT_END = "#D55E00"


def apply_dark_theme() -> None:
    """Apply dark theme to matplotlib globally."""
    import matplotlib as mpl
    import matplotlib.pyplot as plt

    plt.style.use("dark_background")

    mpl.rcParams.update(
        {
            # Figure
            "figure.facecolor": BG_DARK,
            "figure.edgecolor": BG_DARK,
            "figure.figsize": (12, 7),
            "figure.dpi": 150,
            # Axes
            "axes.facecolor": BG_PANEL,
            "axes.edgecolor": BORDER_COLOR,
            "axes.labelcolor": TEXT_PRIMARY,
            "axes.titlesize": 14,
            "axes.titleweight": "bold",
            "axes.titlepad": 16,
            "axes.labelsize": 11,
            "axes.labelpad": 8,
            "axes.grid": True,
            "axes.axisbelow": True,
            "axes.prop_cycle": mpl.cycler(color=PALETTE),
            # Grid
            "grid.color": GRID_COLOR,
            "grid.linewidth": 0.5,
            "grid.alpha": 0.6,
            # Ticks
            "xtick.color": TEXT_SECONDARY,
            "ytick.color": TEXT_SECONDARY,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            # Legend
            "legend.facecolor": BG_CARD,
            "legend.edgecolor": BORDER_COLOR,
            "legend.fontsize": 10,
            "legend.framealpha": 0.9,
            # Text
            "text.color": TEXT_PRIMARY,
            "font.family": "sans-serif",
            "font.sans-serif": ["DejaVu Sans", "Inter", "Segoe UI", "Helvetica Neue", "Arial"],
            "font.size": 11,
            # Lines
            "lines.linewidth": 2.5,
            "lines.markersize": 7,
            "lines.markeredgewidth": 0.5,
            "lines.markeredgecolor": BG_DARK,
            # Savefig
            "savefig.facecolor": BG_DARK,
            "savefig.edgecolor": BG_DARK,
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.3,
        }
    )


def color(index: int) -> str:
    """Get palette color by index (wraps around)."""
    return PALETTE[index % len(PALETTE)]
