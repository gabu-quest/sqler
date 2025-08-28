import sqlite3
from abc import ABC, abstractmethod
from typing import Any, List, Optional


class AdapterError(Exception):
    """Base exception for database adapter errors."""

    pass


class NotConnectedError(ConnectionError):
    """Raised when attempting to execute operations without an active connection."""

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
    def executemany(self, query: str, param_list: List[List[Any]]) -> None:
        """Executes a query with many params."""
        pass

    @abstractmethod
    def executescript(self, script: str) -> None:
        """Executes the script passed to it."""
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
