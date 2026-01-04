"""Metrics collection and export for SQLer.

This module provides a metrics system that builds on SQLer's QueryLogger
to collect database performance metrics. It supports multiple export formats
including Prometheus, StatsD, and custom callbacks.

Usage::

    from sqler.metrics import MetricsCollector, metrics

    # Enable metrics collection (uses global collector)
    metrics.enable()

    # Get current metrics
    print(metrics.get_metrics())

    # Export as Prometheus format
    print(metrics.prometheus_export())

    # Add custom callback for real-time monitoring
    metrics.add_callback(lambda m: print(f"Query took {m.duration_ms}ms"))

For Prometheus integration::

    from sqler.metrics import metrics

    @app.get("/metrics")
    def prometheus_metrics():
        return Response(
            metrics.prometheus_export(),
            media_type="text/plain"
        )
"""

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from sqler.logging import QueryLog, query_logger


@dataclass
class QueryMetrics:
    """Aggregated metrics for queries."""

    total_queries: int = 0
    total_errors: int = 0
    total_duration_ms: float = 0
    min_duration_ms: float = float("inf")
    max_duration_ms: float = 0
    avg_duration_ms: float = 0

    # Histogram buckets (in ms)
    histogram: dict[str, int] = field(
        default_factory=lambda: {
            "le_1": 0,  # <= 1ms
            "le_5": 0,  # <= 5ms
            "le_10": 0,  # <= 10ms
            "le_25": 0,  # <= 25ms
            "le_50": 0,  # <= 50ms
            "le_100": 0,  # <= 100ms
            "le_250": 0,  # <= 250ms
            "le_500": 0,  # <= 500ms
            "le_1000": 0,  # <= 1s
            "le_inf": 0,  # > 1s
        }
    )

    def record(self, duration_ms: float, error: bool = False) -> None:
        """Record a query execution."""
        self.total_queries += 1
        self.total_duration_ms += duration_ms

        if error:
            self.total_errors += 1

        if duration_ms < self.min_duration_ms:
            self.min_duration_ms = duration_ms
        if duration_ms > self.max_duration_ms:
            self.max_duration_ms = duration_ms

        self.avg_duration_ms = self.total_duration_ms / self.total_queries

        # Update histogram
        if duration_ms <= 1:
            self.histogram["le_1"] += 1
        if duration_ms <= 5:
            self.histogram["le_5"] += 1
        if duration_ms <= 10:
            self.histogram["le_10"] += 1
        if duration_ms <= 25:
            self.histogram["le_25"] += 1
        if duration_ms <= 50:
            self.histogram["le_50"] += 1
        if duration_ms <= 100:
            self.histogram["le_100"] += 1
        if duration_ms <= 250:
            self.histogram["le_250"] += 1
        if duration_ms <= 500:
            self.histogram["le_500"] += 1
        if duration_ms <= 1000:
            self.histogram["le_1000"] += 1
        self.histogram["le_inf"] += 1  # Always increment inf bucket

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "total_queries": self.total_queries,
            "total_errors": self.total_errors,
            "total_duration_ms": self.total_duration_ms,
            "min_duration_ms": self.min_duration_ms if self.total_queries > 0 else 0,
            "max_duration_ms": self.max_duration_ms,
            "avg_duration_ms": self.avg_duration_ms,
            "histogram": dict(self.histogram),
        }


@dataclass
class TableMetrics:
    """Metrics per table."""

    inserts: int = 0
    updates: int = 0
    deletes: int = 0
    selects: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "inserts": self.inserts,
            "updates": self.updates,
            "deletes": self.deletes,
            "selects": self.selects,
        }


class MetricsCollector:
    """Collects and aggregates database metrics.

    This collector hooks into SQLer's QueryLogger to automatically track:
    - Query counts and durations
    - Error rates
    - Latency histograms (Prometheus-compatible)
    - Per-table operation counts

    Usage::

        collector = MetricsCollector()
        collector.enable()

        # ... run queries ...

        metrics = collector.get_metrics()
        print(f"Total queries: {metrics['queries']['total_queries']}")

        # Prometheus export
        print(collector.prometheus_export())
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._enabled = False
        self._start_time: Optional[datetime] = None

        # Core metrics
        self._queries = QueryMetrics()
        self._tables: dict[str, TableMetrics] = defaultdict(TableMetrics)

        # Slow query tracking
        self._slow_threshold_ms = 100.0
        self._slow_queries: list[dict[str, Any]] = []
        self._max_slow_queries = 100

        # Custom labels
        self._labels: dict[str, str] = {}

        # Callbacks for real-time monitoring
        self._callbacks: list[Callable[[QueryLog], None]] = []

    def enable(
        self,
        slow_threshold_ms: float = 100.0,
        labels: Optional[dict[str, str]] = None,
    ) -> None:
        """Enable metrics collection.

        Args:
            slow_threshold_ms: Threshold for slow query tracking.
            labels: Custom labels to add to all metrics.
        """
        with self._lock:
            if self._enabled:
                return

            self._enabled = True
            self._start_time = datetime.now()
            self._slow_threshold_ms = slow_threshold_ms

            if labels:
                self._labels.update(labels)

            # Hook into query logger
            query_logger.add_callback(self._on_query)
            query_logger.enable(log_to_logger=False)

    def disable(self) -> None:
        """Disable metrics collection."""
        with self._lock:
            if not self._enabled:
                return

            self._enabled = False
            query_logger.remove_callback(self._on_query)

    def reset(self) -> None:
        """Reset all metrics to zero."""
        with self._lock:
            self._queries = QueryMetrics()
            self._tables.clear()
            self._slow_queries.clear()
            self._start_time = datetime.now() if self._enabled else None

    def _on_query(self, log: QueryLog) -> None:
        """Callback for each query execution."""
        with self._lock:
            # Record query metrics
            self._queries.record(log.duration_ms, error=log.error is not None)

            # Parse SQL for table metrics
            self._record_table_op(log.sql)

            # Track slow queries
            if log.duration_ms >= self._slow_threshold_ms:
                self._slow_queries.append(
                    {
                        "sql": log.sql[:200],  # Truncate
                        "duration_ms": log.duration_ms,
                        "timestamp": log.timestamp.isoformat(),
                        "error": log.error,
                    }
                )
                if len(self._slow_queries) > self._max_slow_queries:
                    self._slow_queries.pop(0)

            # Call user callbacks
            for callback in self._callbacks:
                try:
                    callback(log)
                except Exception:
                    pass

    def _record_table_op(self, sql: str) -> None:
        """Parse SQL to record per-table operations."""
        sql_upper = sql.strip().upper()

        # Extract table name and operation type
        table = None
        op = None

        if sql_upper.startswith("INSERT INTO"):
            op = "inserts"
            parts = sql.split()
            if len(parts) >= 3:
                table = parts[2].strip("(").lower()

        elif sql_upper.startswith("UPDATE"):
            op = "updates"
            parts = sql.split()
            if len(parts) >= 2:
                table = parts[1].lower()

        elif sql_upper.startswith("DELETE FROM"):
            op = "deletes"
            parts = sql.split()
            if len(parts) >= 3:
                table = parts[2].lower()

        elif sql_upper.startswith("SELECT"):
            op = "selects"
            # Try to find FROM clause
            from_idx = sql_upper.find(" FROM ")
            if from_idx != -1:
                after_from = sql[from_idx + 6 :].strip()
                table = after_from.split()[0].lower() if after_from else None

        if table and op:
            # Clean table name
            table = table.strip('"').strip("'").strip("`")
            if not table.startswith("sqlite_"):
                metrics = self._tables[table]
                setattr(metrics, op, getattr(metrics, op) + 1)

    def add_callback(self, callback: Callable[[QueryLog], None]) -> None:
        """Add a callback for real-time query monitoring.

        Args:
            callback: Function called with QueryLog for each query.
        """
        with self._lock:
            self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[QueryLog], None]) -> None:
        """Remove a previously added callback."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

    def get_metrics(self) -> dict[str, Any]:
        """Get all collected metrics.

        Returns:
            Dict with queries, tables, slow queries, and metadata.
        """
        with self._lock:
            return {
                "queries": self._queries.to_dict(),
                "tables": {t: m.to_dict() for t, m in self._tables.items()},
                "slow_queries": list(self._slow_queries),
                "slow_threshold_ms": self._slow_threshold_ms,
                "labels": dict(self._labels),
                "start_time": self._start_time.isoformat() if self._start_time else None,
                "uptime_seconds": (
                    (datetime.now() - self._start_time).total_seconds() if self._start_time else 0
                ),
            }

    def prometheus_export(self) -> str:
        """Export metrics in Prometheus text format.

        Returns:
            String in Prometheus exposition format.
        """
        with self._lock:
            lines = []
            labels_str = ",".join(f'{k}="{v}"' for k, v in self._labels.items())
            label_suffix = f"{{{labels_str}}}" if labels_str else ""

            # Query counter
            lines.append("# HELP sqler_queries_total Total number of SQL queries executed")
            lines.append("# TYPE sqler_queries_total counter")
            lines.append(f"sqler_queries_total{label_suffix} {self._queries.total_queries}")

            # Error counter
            lines.append("# HELP sqler_query_errors_total Total number of failed queries")
            lines.append("# TYPE sqler_query_errors_total counter")
            lines.append(f"sqler_query_errors_total{label_suffix} {self._queries.total_errors}")

            # Duration histogram
            lines.append("# HELP sqler_query_duration_ms_bucket Query duration histogram")
            lines.append("# TYPE sqler_query_duration_ms_bucket histogram")

            bucket_bounds = [
                ("1", "le_1"),
                ("5", "le_5"),
                ("10", "le_10"),
                ("25", "le_25"),
                ("50", "le_50"),
                ("100", "le_100"),
                ("250", "le_250"),
                ("500", "le_500"),
                ("1000", "le_1000"),
                ("+Inf", "le_inf"),
            ]
            for bound, key in bucket_bounds:
                count = self._queries.histogram[key]
                if labels_str:
                    lines.append(
                        f'sqler_query_duration_ms_bucket{{le="{bound}",{labels_str}}} {count}'
                    )
                else:
                    lines.append(f'sqler_query_duration_ms_bucket{{le="{bound}"}} {count}')

            lines.append(
                f"sqler_query_duration_ms_sum{label_suffix} {self._queries.total_duration_ms}"
            )
            lines.append(
                f"sqler_query_duration_ms_count{label_suffix} {self._queries.total_queries}"
            )

            # Per-table metrics
            lines.append("# HELP sqler_table_operations_total Operations per table")
            lines.append("# TYPE sqler_table_operations_total counter")
            for table, table_metrics in self._tables.items():
                for op in ["inserts", "updates", "deletes", "selects"]:
                    count = getattr(table_metrics, op)
                    if labels_str:
                        lines.append(
                            f'sqler_table_operations_total{{table="{table}",operation="{op}",{labels_str}}} {count}'
                        )
                    else:
                        lines.append(
                            f'sqler_table_operations_total{{table="{table}",operation="{op}"}} {count}'
                        )

            # Slow query count
            lines.append("# HELP sqler_slow_queries_total Number of slow queries")
            lines.append("# TYPE sqler_slow_queries_total counter")
            lines.append(f"sqler_slow_queries_total{label_suffix} {len(self._slow_queries)}")

            return "\n".join(lines) + "\n"

    def statsd_export(self) -> list[tuple[str, float, str]]:
        """Export metrics in StatsD format.

        Returns:
            List of (metric_name, value, type) tuples.
            Types: 'c' = counter, 'g' = gauge, 'ms' = timing
        """
        with self._lock:
            metrics_list = []

            # Counters
            metrics_list.append(("sqler.queries.total", self._queries.total_queries, "c"))
            metrics_list.append(("sqler.queries.errors", self._queries.total_errors, "c"))

            # Gauges
            if self._queries.total_queries > 0:
                metrics_list.append(("sqler.queries.avg_ms", self._queries.avg_duration_ms, "g"))
                metrics_list.append(("sqler.queries.max_ms", self._queries.max_duration_ms, "g"))

            # Per-table
            for table, table_metrics in self._tables.items():
                for op in ["inserts", "updates", "deletes", "selects"]:
                    count = getattr(table_metrics, op)
                    metrics_list.append((f"sqler.table.{table}.{op}", count, "c"))

            return metrics_list


# Global metrics collector instance
metrics = MetricsCollector()
