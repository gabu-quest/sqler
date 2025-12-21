"""Query logging utilities for SQLer.

This module provides query logging functionality for debugging and
performance monitoring.
"""

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

# Default logger for SQLer
logger = logging.getLogger("sqler")


@dataclass
class QueryLog:
    """Record of a single query execution."""

    sql: str
    params: list[Any]
    duration_ms: float
    timestamp: datetime = field(default_factory=datetime.now)
    rows_affected: Optional[int] = None
    error: Optional[str] = None

    def __str__(self) -> str:
        status = "OK" if self.error is None else f"ERROR: {self.error}"
        return f"[{self.duration_ms:.2f}ms] {self.sql} | params={self.params} | {status}"


class QueryLogger:
    """Configurable query logger for debugging and profiling.

    Usage::

        from sqler.logging import query_logger

        # Enable logging to console
        query_logger.enable()

        # Execute queries - they will be logged
        db.find_document("users", 1)

        # Get all logged queries
        for log in query_logger.logs:
            print(log)

        # Get slow queries (> 100ms)
        slow = query_logger.get_slow_queries(threshold_ms=100)

        # Disable logging
        query_logger.disable()

        # Clear logs
        query_logger.clear()
    """

    def __init__(self, max_logs: int = 1000):
        self._enabled = False
        self._logs: list[QueryLog] = []
        self._max_logs = max_logs
        self._callbacks: list[Callable[[QueryLog], None]] = []
        self._log_to_logger = False
        self._log_level = logging.DEBUG

    def enable(self, log_to_logger: bool = True, level: int = logging.DEBUG) -> None:
        """Enable query logging.

        Args:
            log_to_logger: Also log to Python logger.
            level: Logging level for logger output.
        """
        self._enabled = True
        self._log_to_logger = log_to_logger
        self._log_level = level

    def disable(self) -> None:
        """Disable query logging."""
        self._enabled = False

    @property
    def enabled(self) -> bool:
        """Return True if logging is enabled."""
        return self._enabled

    @property
    def logs(self) -> list[QueryLog]:
        """Return all logged queries."""
        return list(self._logs)

    def clear(self) -> None:
        """Clear all logged queries."""
        self._logs.clear()

    def add_callback(self, callback: Callable[[QueryLog], None]) -> None:
        """Add a callback to be called for each logged query.

        Args:
            callback: Function to call with each QueryLog.
        """
        self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[QueryLog], None]) -> None:
        """Remove a previously added callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def log(
        self,
        sql: str,
        params: list[Any],
        duration_ms: float,
        rows_affected: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Log a query execution.

        Args:
            sql: SQL statement executed.
            params: Query parameters.
            duration_ms: Execution time in milliseconds.
            rows_affected: Number of rows affected (if known).
            error: Error message if query failed.
        """
        if not self._enabled:
            return

        log_entry = QueryLog(
            sql=sql,
            params=params,
            duration_ms=duration_ms,
            rows_affected=rows_affected,
            error=error,
        )

        # Add to log buffer (with size limit)
        self._logs.append(log_entry)
        if len(self._logs) > self._max_logs:
            self._logs.pop(0)

        # Log to Python logger if enabled
        if self._log_to_logger:
            logger.log(self._log_level, str(log_entry))

        # Call callbacks
        for callback in self._callbacks:
            try:
                callback(log_entry)
            except Exception:
                pass  # Don't let callback errors break logging

    def get_slow_queries(self, threshold_ms: float = 100) -> list[QueryLog]:
        """Get all queries slower than threshold.

        Args:
            threshold_ms: Threshold in milliseconds.

        Returns:
            list[QueryLog]: Queries exceeding the threshold.
        """
        return [log for log in self._logs if log.duration_ms >= threshold_ms]

    def get_failed_queries(self) -> list[QueryLog]:
        """Get all queries that failed with an error."""
        return [log for log in self._logs if log.error is not None]

    def get_stats(self) -> dict[str, Any]:
        """Get statistics about logged queries.

        Returns:
            dict: Statistics including count, avg time, slowest, etc.
        """
        if not self._logs:
            return {
                "count": 0,
                "total_time_ms": 0,
                "avg_time_ms": 0,
                "min_time_ms": 0,
                "max_time_ms": 0,
                "error_count": 0,
            }

        durations = [log.duration_ms for log in self._logs]
        return {
            "count": len(self._logs),
            "total_time_ms": sum(durations),
            "avg_time_ms": sum(durations) / len(durations),
            "min_time_ms": min(durations),
            "max_time_ms": max(durations),
            "error_count": len([log for log in self._logs if log.error]),
        }

    @contextmanager
    def capture(self):
        """Context manager to capture queries within a scope.

        Usage::

            with query_logger.capture() as captured:
                db.find_document("users", 1)
                db.find_document("users", 2)
            print(f"Captured {len(captured)} queries")
        """
        start_idx = len(self._logs)
        was_enabled = self._enabled
        self._enabled = True
        captured: list[QueryLog] = []
        try:
            yield captured
        finally:
            captured.extend(self._logs[start_idx:])
            if not was_enabled:
                self._enabled = False


@contextmanager
def timed_query():
    """Context manager to time a query execution.

    Usage::

        with timed_query() as timer:
            cursor.execute(sql, params)
        duration_ms = timer.duration_ms
    """

    class Timer:
        def __init__(self):
            self.start_time = time.perf_counter()
            self.end_time: Optional[float] = None

        @property
        def duration_ms(self) -> float:
            end = self.end_time or time.perf_counter()
            return (end - self.start_time) * 1000

    timer = Timer()
    try:
        yield timer
    finally:
        timer.end_time = time.perf_counter()


# Global query logger instance
query_logger = QueryLogger()
