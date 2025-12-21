"""Shared utility functions for SQLer models.

This module provides common helper functions used across both sync and async
model implementations.
"""


def compute_numeric_scalar_deltas(orig: dict, target: dict) -> dict[str, int]:
    """Compute numeric deltas between two document states.

    For each integer field in target, computes the difference from orig.
    Used for intent rebasing in optimistic locking scenarios.

    Args:
        orig: Original document state.
        target: Target document state.

    Returns:
        dict[str, int]: Mapping of field names to their delta values.
    """
    deltas: dict[str, int] = {}
    for k, v in target.items():
        if isinstance(v, int):
            base = orig.get(k, 0)
            if isinstance(base, int):
                dv = v - base
                if dv != 0:
                    deltas[k] = dv
    return deltas


def apply_numeric_scalar_deltas(base: dict, delta: dict[str, int]) -> dict:
    """Apply numeric deltas to a document.

    For each field in delta, adds the delta value to the corresponding
    field in base (or sets it if the field doesn't exist).

    Args:
        base: Base document to apply deltas to.
        delta: Mapping of field names to delta values.

    Returns:
        dict: New document with deltas applied.
    """
    out = {**base}
    for k, dv in delta.items():
        cur = out.get(k, 0)
        if isinstance(cur, int):
            out[k] = cur + dv
        else:
            out[k] = dv
    return out
