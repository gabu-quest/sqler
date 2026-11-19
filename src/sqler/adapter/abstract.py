import sqlite3
from abc import ABC, abstractmethod
from typing import Any, Iterator, List, NoReturn, Optional, Protocol


class AdapterError(Exception):
    """Base exception for database adapter errors."""

    pass


class CursorLike(Protocol):
    """What ``execute`` gives back: the part of ``sqlite3.Cursor`` that reads a statement's
    result. A ``sqlite3.Cursor`` is one; so are the rows an adapter has already read."""

    @property
    def description(self) -> Any: ...

    @property
    def rowcount(self) -> int: ...

    @property
    def lastrowid(self) -> Optional[int]: ...

    def fetchone(self) -> Any: ...

    def fetchmany(self, size: Optional[int] = ...) -> list[Any]: ...

    def fetchall(self) -> list[Any]: ...

    def __iter__(self) -> Iterator[Any]: ...

    def __next__(self) -> Any: ...

    def close(self) -> None: ...


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
    def execute(self, query: str, params: Optional[List[Any]] = None) -> CursorLike:
        """Execute a single query with optional params.
        returns a cursor: a ``sqlite3.Cursor``, or rows already read that answer as one
        """
        pass

    def _execute_judged(self, query: str, params: Optional[List[Any]] = None) -> CursorLike:
        """``execute`` for sqler's own senders, which read some failures of a statement as an
        answer. An adapter that does not tell the two apart sends it as any statement: by the
        time the failure is read, its ``execute`` has ended what it ends."""
        return self.execute(query, params)

    def _end_failed_statement(self, exc: Exception) -> NoReturn:
        """Raise ``exc``, the failure of a statement sent with ``_execute_judged`` that its
        sender did not read as an answer, once what a failed statement ends is ended: here
        ``execute`` ended it already."""
        raise exc

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

    async def _execute_judged(self, query: str, params: Optional[List[Any]] = None) -> Any:
        """Async twin of :meth:`AdapterABC._execute_judged`."""
        return await self.execute(query, params)

    async def _end_failed_statement(self, exc: Exception) -> NoReturn:
        """Async twin of :meth:`AdapterABC._end_failed_statement`."""
        raise exc

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

    def pool_stats(self) -> Any:
        """Return the adapter's connection-pool statistics.

        Not abstract: an adapter without a pool need not define it. The default
        raises ``NotImplementedError``; ``AsyncSQLiteAdapter`` returns a ``PoolStats``.
        """
        raise NotImplementedError(f"{type(self).__name__} does not implement pool_stats()")
