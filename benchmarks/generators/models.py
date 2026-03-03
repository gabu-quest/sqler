"""Benchmark model definitions using sqler public API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqler import MSGSPEC_AVAILABLE, SQLerLiteModel, SQLerModel, SQLerSafeModel


class BenchmarkItem(SQLerModel):
    """Pydantic-based model for insert/query benchmarks."""

    name: str
    value: int = 0
    category: str = ""
    tags: list[str] = []
    score: float = 0.0
    description: str = ""


@dataclass
class BenchmarkItemLite(SQLerLiteModel):
    """Dataclass-based model for comparing overhead vs Pydantic."""

    name: str = ""
    value: int = 0
    category: str = ""
    tags: list[str] = field(default_factory=list)
    score: float = 0.0
    description: str = ""


class BenchmarkCounter(SQLerSafeModel):
    """Optimistic-locking model for contention benchmarks."""

    name: str
    tally: int = 0


class BenchmarkArticle(SQLerModel):
    """Model for FTS benchmarks."""

    title: str
    content: str
    author: str = ""
    tags: list[str] = []


# Msgspec model — only available when msgspec is installed
BenchmarkItemMsgspec: Optional[type] = None

if MSGSPEC_AVAILABLE:
    from sqler import SQLerMsgspecModel

    class _BenchmarkItemMsgspec(SQLerMsgspecModel):
        """Msgspec Struct-based model for hydration benchmarks."""

        name: str = ""
        value: int = 0
        category: str = ""
        tags: list[str] = []
        score: float = 0.0
        description: str = ""

    BenchmarkItemMsgspec = _BenchmarkItemMsgspec
