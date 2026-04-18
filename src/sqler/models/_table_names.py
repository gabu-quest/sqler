"""Shared helpers for deriving default SQLite table names from model class names.

These were previously duplicated byte-for-byte across the three model backends
(Pydantic, Lite, Msgspec). Centralizing here prevents drift — any future change
to pluralization or reserved-word handling lands in one place.
"""

from __future__ import annotations


def _pluralize(word: str) -> str:
    """Pluralize a word using common English rules.

    Handles:
    - Words ending in consonant + y → ies (category → categories)
    - Words ending in s, x, z, ch, sh → es (box → boxes)
    - Regular words → s (user → users)

    For irregular plurals (person, child, etc.), use ``__tablename__``.
    """
    w = word.lower()
    if w.endswith("s"):
        return w  # Already plural or ends in s
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        return w[:-1] + "ies"  # category → categories
    if w.endswith(("s", "x", "z", "ch", "sh")):
        return w + "es"  # box → boxes, class → classes
    return w + "s"  # user → users


def _default_table_name(name: str) -> str:
    """Generate default table name from class name.

    Examples:
        User → users
        Category → categories
        Address → addresses
        As → as_tbl (SQL reserved word)
    """
    lower = name.lower()
    # Check reserved words BEFORE pluralization (for words like "by", "as")
    # Also include words that pluralize to reserved words (a → as)
    reserved = {"a", "as", "by", "and", "or", "not", "null", "index", "table"}
    if lower in reserved:
        return lower + "_tbl"
    return _pluralize(name)
