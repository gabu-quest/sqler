import sqlite3
from abc import ABC, abstractmethod
from typing import Any, List, Optional


class AdapterError(Exception):
    """Base exception for database adapter errors."""

    pass


class AdapterABC(ABC):
    """Abstract base for a synchronous DB adapter."""

    @abstractmethod
    def connect(self) -> None:
        """Connect to the db"""
        pass

    @abstractmethod
    def close(self) -> None:
        """Close connection to db"""
        pass

    @abstractmethod
    def execute(self, query: str, params: Optional[List[Any]] = None) -> sqlite3.Cursor:
        """Execute a single query with optional params.
        returns a sqlite3 cursor
        """
        pass

    @abstractmethod
    def executemany(self, query: str, param_list: List[List[Any]]) -> sqlite3.Cursor:
        """Executes a query with many params and returns cursor."""
        pass

    @abstractmethod
    def executescript(self, script: str) -> sqlite3.Cursor:
        """Executes the script passed to it and returns cursor."""
        pass

    @abstractmethod
    def commit(self) -> None:
        """Commit the current transaction."""
        pass

    @abstractmethod
    def __enter__(self):
        """Enter context manager."""
        pass

    @abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager."""
        pass


class AsyncAdapterABC(ABC):
    """Abstract base for an asynchronous DB adapter."""

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the db"""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close connection to db"""
        pass

    @abstractmethod
    async def execute(self, query: str, params: Optional[List[Any]] = None) -> Any:
        """Execute a single query with optional params.
        Returns a cursor-like object.
        """
        pass

    @abstractmethod
    async def executemany(self, query: str, param_list: List[List[Any]]) -> Any:
        """Executes a query with many params."""
        pass

    @abstractmethod
    async def executescript(self, script: str) -> Any:
        """Executes the script passed to it."""
        pass

    @abstractmethod
    async def commit(self) -> None:
        """Commit the current transaction."""
        pass

    @abstractmethod
    async def __aenter__(self):
        """Enter async context manager."""
        pass

    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context manager."""
        pass
