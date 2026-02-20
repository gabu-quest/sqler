"""Tests for set_db() DeprecationWarning on all model classes.

Verifies that set_db() emits a DeprecationWarning with the correct message
on all 4 base model classes, and that bind() on lite models triggers
the warning via set_db().
"""

from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqler import SQLerDB, SQLerModel, SQLerSafeModel
from sqler.db.async_db import AsyncSQLerDB
from sqler.models.async_model import AsyncSQLerModel
from sqler.models.async_safe import AsyncSQLerSafeModel
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel


# ── Sync model classes ────────────────────────────────────────────────────


class PydanticItem(SQLerModel):
    __tablename__ = "dep_pydantic_items"
    name: str


class PydanticSafeItem(SQLerSafeModel):
    __tablename__ = "dep_pydantic_safe_items"
    name: str


@dataclass
class LiteItem(SQLerLiteModel):
    __tablename__ = "dep_lite_items"
    name: str


@dataclass
class LiteSafeItem(SQLerLiteSafeModel):
    __tablename__ = "dep_lite_safe_items"
    name: str


# ── Async model classes ───────────────────────────────────────────────────


class AsyncPydanticItem(AsyncSQLerModel):
    __tablename__ = "dep_async_pydantic_items"
    name: str


class AsyncPydanticSafeItem(AsyncSQLerSafeModel):
    __tablename__ = "dep_async_pydantic_safe_items"
    name: str


@dataclass
class AsyncLiteItem(AsyncSQLerLiteModel):
    __tablename__ = "dep_async_lite_items"
    name: str


@dataclass
class AsyncLiteSafeItem(AsyncSQLerLiteSafeModel):
    __tablename__ = "dep_async_lite_safe_items"
    name: str


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def sync_db():
    return SQLerDB.in_memory(shared=False)


@pytest_asyncio.fixture
async def async_db():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════════════════
# Sync base model deprecation
# ══════════════════════════════════════════════════════════════════════════


class TestSQLerModelSetDbDeprecation:
    """set_db() on SQLerModel emits DeprecationWarning."""

    def test_emits_deprecation_warning(self, sync_db):
        with pytest.warns(DeprecationWarning, match="PydanticItem.set_db.*deprecated"):
            PydanticItem.set_db(sync_db)

    def test_warning_mentions_alternatives(self, sync_db):
        with pytest.warns(DeprecationWarning, match=r"\.using\(db\).*\.save\(db=db\)"):
            PydanticItem.set_db(sync_db)

    def test_warning_mentions_future_removal(self, sync_db):
        with pytest.warns(DeprecationWarning, match="will be removed"):
            PydanticItem.set_db(sync_db)


class TestSQLerLiteModelSetDbDeprecation:
    """set_db() on SQLerLiteModel emits DeprecationWarning."""

    def test_emits_deprecation_warning(self, sync_db):
        with pytest.warns(DeprecationWarning, match="LiteItem.set_db.*deprecated"):
            LiteItem.set_db(sync_db)

    def test_warning_mentions_alternatives(self, sync_db):
        with pytest.warns(DeprecationWarning, match=r"\.using\(db\).*\.save\(db=db\)"):
            LiteItem.set_db(sync_db)


class TestAsyncSQLerModelSetDbDeprecation:
    """set_db() on AsyncSQLerModel emits DeprecationWarning."""

    def test_emits_deprecation_warning(self, async_db):
        with pytest.warns(DeprecationWarning, match="AsyncPydanticItem.set_db.*deprecated"):
            AsyncPydanticItem.set_db(async_db)

    def test_warning_mentions_alternatives(self, async_db):
        with pytest.warns(DeprecationWarning, match=r"\.using\(db\).*\.save\(db=db\)"):
            AsyncPydanticItem.set_db(async_db)


class TestAsyncSQLerLiteModelSetDbDeprecation:
    """set_db() on AsyncSQLerLiteModel emits DeprecationWarning."""

    def test_emits_deprecation_warning(self, async_db):
        with pytest.warns(DeprecationWarning, match="AsyncLiteItem.set_db.*deprecated"):
            AsyncLiteItem.set_db(async_db)

    def test_warning_mentions_alternatives(self, async_db):
        with pytest.warns(DeprecationWarning, match=r"\.using\(db\).*\.save\(db=db\)"):
            AsyncLiteItem.set_db(async_db)


# ══════════════════════════════════════════════════════════════════════════
# Safe model set_db() propagation (via super())
# ══════════════════════════════════════════════════════════════════════════


class TestSafeModelSetDbPropagation:
    """Safe model set_db() calls super(), which emits the warning."""

    def test_pydantic_safe_emits_warning(self, sync_db):
        with pytest.warns(DeprecationWarning, match="PydanticSafeItem.set_db.*deprecated"):
            PydanticSafeItem.set_db(sync_db)

    def test_lite_safe_emits_warning(self, sync_db):
        with pytest.warns(DeprecationWarning, match="LiteSafeItem.set_db.*deprecated"):
            LiteSafeItem.set_db(sync_db)

    def test_async_pydantic_safe_emits_warning(self, async_db):
        with pytest.warns(DeprecationWarning, match="AsyncPydanticSafeItem.set_db.*deprecated"):
            AsyncPydanticSafeItem.set_db(async_db)

    def test_async_lite_safe_emits_warning(self, async_db):
        with pytest.warns(DeprecationWarning, match="AsyncLiteSafeItem.set_db.*deprecated"):
            AsyncLiteSafeItem.set_db(async_db)


# ══════════════════════════════════════════════════════════════════════════
# bind() on lite models triggers warning (via set_db())
# ══════════════════════════════════════════════════════════════════════════


class TestLiteBindDeprecation:
    """bind() is an alias for set_db(), so it triggers the same warning."""

    def test_lite_bind_emits_warning(self, sync_db):
        with pytest.warns(DeprecationWarning, match="LiteItem.set_db.*deprecated"):
            LiteItem.bind(sync_db)

    def test_async_lite_bind_emits_warning(self, async_db):
        with pytest.warns(DeprecationWarning, match="AsyncLiteItem.set_db.*deprecated"):
            AsyncLiteItem.bind(async_db)
