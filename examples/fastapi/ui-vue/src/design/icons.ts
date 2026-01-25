/**
 * Phosphor + Naive UI integration helpers.
 *
 * Phosphor recommends NOT globally installing all icons to preserve tree-shaking.
 * Instead, import the icons you use. You can still provide defaults via provide/inject.
 */
import { provide } from "vue";
import { ds } from "./tokens";

/**
 * Call this in setup() near the app root (above where icons render).
 * Sets global defaults for Phosphor icons via provide/inject.
 */
export function providePhosphorDefaults() {
  provide("color", ds.icons.defaults.color);
  provide("size", ds.icons.defaults.size);
  provide("weight", ds.icons.defaults.weight);
  provide("mirrored", ds.icons.defaults.mirrored);
}
