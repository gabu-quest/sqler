/* eslint-disable @typescript-eslint/consistent-type-definitions */
/**
 * Naive UI theme mapping for the Unified Design System — Cyberpunk Edition.
 *
 * Rule:
 * - Neutrals are inert panels.
 * - Accents are emitted signals.
 * - Glows are reserved for focus/active states and primary actions.
 *
 * Edit tokens.ts. Keep this mapping stable unless you are changing strategy.
 */
import type { GlobalThemeOverrides } from "naive-ui";
import { ds, type DsMode, rgba } from "./tokens";

function common(mode: DsMode): GlobalThemeOverrides["common"] {
  const m = ds.color.mode[mode];
  const s = ds.color.semantic;
  return {
    // Semantic signals
    primaryColor: s.primary,
    primaryColorHover: rgba(s.primary, 0.90),
    primaryColorPressed: rgba(s.primary, 0.78),
    primaryColorSuppl: rgba(s.primary, 0.92),

    infoColor: s.info,
    infoColorHover: rgba(s.info, 0.88),
    infoColorPressed: rgba(s.info, 0.78),
    infoColorSuppl: rgba(s.info, 0.92),

    successColor: s.success,
    successColorHover: rgba(s.success, 0.90),
    successColorPressed: rgba(s.success, 0.78),
    successColorSuppl: rgba(s.success, 0.92),

    warningColor: s.warning,
    warningColorHover: rgba(s.warning, 0.92),
    warningColorPressed: rgba(s.warning, 0.80),
    warningColorSuppl: rgba(s.warning, 0.92),

    errorColor: s.error,
    errorColorHover: rgba(s.error, 0.90),
    errorColorPressed: rgba(s.error, 0.78),
    errorColorSuppl: rgba(s.error, 0.92),

    // Surfaces
    baseColor: mode === "dark" ? ds.color.palette.void : "#ffffff",
    bodyColor: m.bg,
    cardColor: m.surface1,
    popoverColor: m.surface1,
    modalColor: m.surface1,
    tableColor: m.surface1,
    inputColor: m.surface2,
    codeColor: m.surface2,
    actionColor: m.surface2,
    tagColor: m.surface2,
    tabColor: m.surface2,

    hoverColor: m.hover,
    pressedColor: m.pressed,
    dividerColor: m.divider,
    borderColor: m.border,

    // Text
    textColorBase: m.text1,
    textColor1: m.text1,
    textColor2: m.text2,
    textColor3: m.text3,
    textColorDisabled: m.textDisabled,
    placeholderColor: m.text3,
    placeholderColorDisabled: m.textDisabled,

    // Icons
    iconColor: m.text2,
    iconColorHover: m.text1,
    iconColorPressed: m.text1,
    iconColorDisabled: m.textDisabled,

    // @ts-expect-error - tolerated across Naive versions
    focusColor: m.focusRing,

    // Typography
    fontFamily: ds.typography.font.body,
    fontFamilyMono: ds.typography.font.mono,
    fontWeight: String(ds.typography.weight.normal),
    fontWeightStrong: String(ds.typography.weight.strong),
    lineHeight: String(ds.typography.lineHeight.body),

    fontSize: ds.typography.size.base,
    fontSizeTiny: ds.typography.size.tiny,
    fontSizeSmall: ds.typography.size.small,
    fontSizeMedium: ds.typography.size.medium,
    fontSizeLarge: ds.typography.size.large,
    fontSizeHuge: ds.typography.size.huge,

    // Geometry & depth
    borderRadius: ds.radius.md,
    borderRadiusSmall: ds.radius.sm,
    boxShadow1: ds.shadow[1],
    boxShadow2: ds.shadow[2],
    boxShadow3: ds.shadow[3],
  };
}

function components(mode: DsMode): GlobalThemeOverrides {
  const m = ds.color.mode[mode];
  const s = ds.color.semantic;

  // Focus glow: visible, emitted, but not a text glow.
  const focusGlow = ds.effects.glow.primary;

  return {
    common: common(mode),

    Button: {
      borderRadiusMedium: ds.radius.md,
      heightMedium: "34px",

      // Primary is an “active signal”: bright + readable.
      colorPrimary: s.primary,
      colorHoverPrimary: rgba(s.primary, 0.90),
      colorPressedPrimary: rgba(s.primary, 0.80),
      textColorPrimary: mode === "dark" ? ds.color.palette.void : ds.color.mode.light.text1,

      // Default (tonal panel button)
      color: m.surface2,
      colorHover: m.surface3,
      colorPressed: m.pressed,
      textColor: m.text1,
      border: `1px solid ${m.border}`,
      borderHover: `1px solid ${rgba(s.primary, mode === "dark" ? 0.55 : 0.40)}`,
      borderPressed: `1px solid ${rgba(s.primary, mode === "dark" ? 0.70 : 0.55)}`,

      // Focus glow (emissive edge)
      boxShadowFocus: focusGlow,
    },

    Input: {
      borderRadius: ds.radius.md,
      color: m.surface2,
      colorFocus: m.surface2,
      colorFocusError: m.surface2,
      colorFocusWarning: m.surface2,

      border: `1px solid ${m.border}`,
      borderHover: `1px solid ${rgba(s.primary, mode === "dark" ? 0.45 : 0.30)}`,
      borderFocus: `1px solid ${rgba(s.primary, mode === "dark" ? 0.80 : 0.55)}`,
      boxShadowFocus: focusGlow,

      textColor: m.text1,
      placeholderColor: m.text3,
      caretColor: s.primary,
    },

    Card: {
      borderRadius: ds.radius.lg,
      color: m.surface1,
      titleTextColor: m.text1,
      textColor: m.text2,
      borderColor: m.border,
    },

    DataTable: {
      thColor: m.surface2,
      tdColor: "transparent",
      tdColorHover: m.hover,
      tdColorStriped: rgba("#ffffff", mode === "dark" ? 0.03 : 0.02),
      thTextColor: m.text1,
      tdTextColor: m.text2,
      borderColor: m.divider,
    },

    Modal: {
      borderRadius: ds.radius.lg,
      color: m.surface1,
      titleTextColor: m.text1,
      textColor: m.text2,
      boxShadow: ds.shadow[3],
    },

    Tag: {
      borderRadius: ds.radius.sm,
      color: m.surface2,
      textColor: m.text2,
      borderColor: m.border,
    },

    Tooltip: {
      color: mode === "dark" ? "#000000" : "#0b1020",
      textColor: "#e6f1ff",
      borderRadius: ds.radius.sm,
      boxShadow: ds.shadow[2],
      border: `1px solid ${rgba(s.primary, mode === "dark" ? 0.35 : 0.25)}`,
    },

    Code: {
      borderRadius: ds.radius.md,
      color: m.surface2,
      textColor: m.text1,
      fontFamily: ds.typography.font.mono,
    },

    Alert: {
      borderRadius: ds.radius.md,
      color: m.surface1,
      textColor: m.text2,
      titleTextColor: m.text1,
    },
  };
}

export function createDsNaiveThemeOverrides(mode: DsMode): GlobalThemeOverrides {
  return components(mode);
}
