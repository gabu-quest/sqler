"""Shared utility functions for SQLer models.

This module provides common helper functions used across both sync and async
model implementations, including intent rebasing for optimistic locking.
"""

from dataclasses import dataclass
from typing import Callable, Optional, Set


@dataclass
class RebaseConfig:
    """Configuration for intent rebasing in optimistic locking.

    Intent rebasing allows automatic conflict resolution for simple counter
    operations. When a save fails due to stale version, the library can
    automatically rebase the user's intent onto the latest version.

    This is safe for commutative operations like counter increments/decrements,
    but not for complex business logic.

    Attributes:
        enabled: Whether rebasing is enabled at all.
        allowed_fields: Set of field names eligible for rebasing.
            If None, all integer fields with delta ±1 are eligible.
        max_delta: Maximum absolute delta value allowed for rebasing.
        max_retries: Maximum number of rebase attempts before giving up.
        custom_validator: Optional function to validate if a delta can be rebased.
            Receives (field_name, delta_value) and returns bool.
    """

    enabled: bool = True
    allowed_fields: Optional[Set[str]] = None
    max_delta: int = 1
    max_retries: int = 128
    custom_validator: Optional[Callable[[str, int], bool]] = None

    def can_rebase(self, field_name: str, delta: int) -> bool:
        """Check if a specific field delta can be rebased.

        Args:
            field_name: Name of the field.
            delta: Delta value to check.

        Returns:
            bool: True if this delta can be automatically rebased.
        """
        if not self.enabled:
            return False

        # Check allowed fields
        if self.allowed_fields is not None and field_name not in self.allowed_fields:
            return False

        # Check max delta
        if abs(delta) > self.max_delta:
            return False

        # Custom validation
        if self.custom_validator is not None:
            return self.custom_validator(field_name, delta)

        return True


# Default configuration - allows count field with ±1 (backward compatible)
DEFAULT_REBASE_CONFIG = RebaseConfig(
    enabled=True,
    allowed_fields={"count"},
    max_delta=1,
)

# Permissive configuration - allows any integer field with ±1
PERMISSIVE_REBASE_CONFIG = RebaseConfig(
    enabled=True,
    allowed_fields=None,  # Any field
    max_delta=1,
)

# Disabled configuration
NO_REBASE_CONFIG = RebaseConfig(enabled=False)


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


def can_rebase_deltas(
    delta: dict[str, int],
    config: Optional[RebaseConfig] = None,
) -> bool:
    """Check if a set of deltas can be rebased.

    Args:
        delta: Mapping of field names to delta values.
        config: Rebase configuration to use. Defaults to DEFAULT_REBASE_CONFIG.

    Returns:
        bool: True if all deltas in the set can be rebased.
    """
    if not delta:
        return False

    cfg = config or DEFAULT_REBASE_CONFIG

    if not cfg.enabled:
        return False

    # Only allow single-field deltas for safety
    if len(delta) != 1:
        return False

    for field_name, delta_value in delta.items():
        if not cfg.can_rebase(field_name, delta_value):
            return False

    return True
