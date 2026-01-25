"""SQLer Lite - Dataclass-based models for Pyodide compatibility.

This module provides Pydantic-free alternatives using standard library
dataclasses. All models maintain API compatibility with their Pydantic
counterparts.

Usage:
    from dataclasses import dataclass
    from sqler import SQLerLiteModel, SQLerDB

    @dataclass
    class User(SQLerLiteModel):
        __tablename__ = "users"
        name: str
        email: str

    db = SQLerDB(":memory:")
    User.set_db(db)

    user = User(name="Alice", email="alice@example.com")
    user.save()
"""

from sqler.models.lite.base import SQLerLiteModelBase
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel

__all__ = [
    "SQLerLiteModelBase",
    "SQLerLiteModel",
    "SQLerLiteSafeModel",
]

# Async models imported separately to avoid import errors
# when aiosqlite is not installed
try:
    from sqler.models.lite.async_model import AsyncSQLerLiteModel
    from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel

    __all__.extend(["AsyncSQLerLiteModel", "AsyncSQLerLiteSafeModel"])
except ImportError:
    pass
