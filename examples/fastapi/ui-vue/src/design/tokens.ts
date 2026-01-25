/* eslint-disable @typescript-eslint/consistent-type-definitions */
/**
 * Unified Design System — Naive UI Cyberpunk Edition
 * Single source of truth for all tokens.
 *
 * Edit THIS file. Everything else should reference it.
 *
 * Core metaphor:
 * - Neutrals are inert panels (void / gunmetal)
 * - Accents are emitted signals (cyan / magenta)
 * - Meaning is stateful (idle vs active vs alert)
 */

export type DsMode = "dark" | "light";

export const ds = {
  meta: {
    name: "Unified Design System — Naive UI Cyberpunk Edition",
    version: "1.0.0",
    updated: "2025-12-13",
    notes:
      "Dark-first, emissive accents. No text glow. Glows are reserved for focus/active states and primary actions.",
  },

  color: {
    palette: {
      // Neutrals (inert)
      void: "#07080d",
      abyss: "#0b0f1a",
      panel: "#0f1423",
      panel2: "#121a2b",
      steel: "#192235",
      chrome: "#9aa4b2",

      // Text whites (slightly cool)
      frost: "#e6f1ff",

      // Emissive accents (signals)
      neonCyan: "#00e5ff",
      neonMagenta: "#ff2bd6",
      neonRed: "#ff3b3b",
      acidGreen: "#a8ff60",
      amber: "#ffcc00",
      violet: "#8b5cf6",
    },

    mode: {
      dark: {
        bg: "#07080d",
        surface1: "#0f1423",
        surface2: "#121a2b",
        surface3: "#192235",
        paperSurface: "#0b0f1a",

        text1: "#e6f1ff",
        text2: "rgba(230, 241, 255, 0.78)",
        text3: "rgba(230, 241, 255, 0.58)",
        textDisabled: "rgba(230, 241, 255, 0.38)",

        border: "rgba(230, 241, 255, 0.10)",
        divider: "rgba(230, 241, 255, 0.08)",
        hover: "rgba(0, 229, 255, 0.06)",
        pressed: "rgba(0, 229, 255, 0.10)",
        focusRing: "#00e5ff",
      },

      light: {
        bg: "#f6f7fb",
        surface1: "#ffffff",
        surface2: "#eef1f7",
        surface3: "#e6eaf3",
        paperSurface: "#ffffff",

        text1: "#0b1020",
        text2: "rgba(11, 16, 32, 0.78)",
        text3: "rgba(11, 16, 32, 0.58)",
        textDisabled: "rgba(11, 16, 32, 0.38)",

        border: "rgba(11, 16, 32, 0.14)",
        divider: "rgba(11, 16, 32, 0.10)",
        hover: "rgba(0, 229, 255, 0.06)",
        pressed: "rgba(0, 229, 255, 0.12)",
        focusRing: "#00bcd4",
      },
    } satisfies Record<DsMode, Record<string, string>>,

    semantic: {
      // Cyberpunk “signals” — emitted, not reflective
      primary: "#00e5ff",   // neon cyan (power flow)
      info: "#ff2bd6",      // neon magenta (identity channel)
      success: "#a8ff60",   // acid green
      warning: "#ffcc00",   // amber
      error: "#ff3b3b",     // hot red
      link: "#00e5ff",
      noteBorder: "rgba(0, 229, 255, 0.55)", // “circuit” edge
    },
  },

  typography: {
    font: {
      body: "Inter, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
      heading: "'Space Grotesk', Inter, system-ui, sans-serif",
      mono: "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    },
    size: {
      base: "14px",
      tiny: "12px",
      small: "13px",
      medium: "14px",
      large: "15px",
      huge: "16px",
      // Content headings (raw HTML / markdown)
      h1: "2.25rem",
      h2: "1.75rem",
      h3: "1.35rem",
    },
    weight: {
      normal: 400,
      strong: 600,
      heading: 650,
    },
    letterSpacing: {
      // Cyberpunk UI tends to be slightly “labeled”
      heading: "0.02em",
      label: "0.04em",
    },
    lineHeight: {
      body: 1.65,
      heading: 1.15,
      compact: 1.25,
    },
  },

  spacing: {
    1: "0.25rem",
    2: "0.5rem",
    3: "1rem",
    4: "2rem",
    5: "3rem",
    6: "4rem",
  },

  radius: {
    // Sharper corners: panels, not paper cards
    sm: "3px",
    md: "5px",
    lg: "8px",
  },

  shadow: {
    // Depth is primarily used for overlays; glow is reserved for focus/active states.
    1: "0 8px 22px rgba(0, 0, 0, 0.55)",
    2: "0 14px 34px rgba(0, 0, 0, 0.60)",
    3: "0 22px 60px rgba(0, 0, 0, 0.65)",
  },

  effects: {
    glow: {
      primary: "0 0 0 2px rgba(0, 229, 255, 0.40), 0 0 18px rgba(0, 229, 255, 0.18)",
      info: "0 0 0 2px rgba(255, 43, 214, 0.32), 0 0 18px rgba(255, 43, 214, 0.16)",
      error: "0 0 0 2px rgba(255, 59, 59, 0.30), 0 0 18px rgba(255, 59, 59, 0.14)",
    },
  },

  motion: {
    duration: {
      fast: "100ms",
      base: "160ms",
      slow: "220ms",
    },
    easing: {
      inOut: "cubic-bezier(.4, 0, .2, 1)",
      out: "cubic-bezier(0, 0, .2, 1)",
      in: "cubic-bezier(.4, 0, 1, 1)",
    },
  },

  icons: {
    defaults: {
      // Default is “system idle”: clean, not flashy
      color: "currentColor",
      size: "1.05em",
      weight: "regular" as const,
      mirrored: false,
    },
    size: {
      xs: "0.9em",
      sm: "1em",
      md: "1.05em",
      lg: "1.25em",
      xl: "1.5em",
      hero: "2em",
    },
    weight: {
      ui: "regular" as const,
      infrastructure: "thin" as const, // secondary HUD/telemetry only
      emphasis: "duotone" as const,      // alerts / high-salience
      selected: "fill" as const,         // active/toggled
    },
  },

  breakpoints: {
    xsMax: 399,
    smMin: 400,
    mdMin: 640,
    lgMin: 960,
  },
} as const;

export type DesignSystem = typeof ds;

export function normalizeMode(mode: DsMode | boolean): DsMode {
  return mode === true ? "dark" : mode === false ? "light" : mode;
}

export function rgba(hex: string, alpha: number): string {
  const h = hex.replace("#", "").trim();
  if (!/^[0-9a-fA-F]{6}$/.test(h)) return hex;
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  const a = Math.max(0, Math.min(1, alpha));
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}
