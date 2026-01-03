"""Query result caching for SQLer.

Provides an in-memory cache with TTL support, automatic invalidation,
and cache statistics for monitoring.

Usage::

    from sqler.cache import QueryCache, cached_query

    # Create a cache
    cache = QueryCache(max_size=1000, default_ttl_seconds=300)

    # Cache query results manually
    key = "active_users"
    if not cache.has(key):
        users = User.query().filter(F.active == True).all()
        cache.set(key, users, ttl_seconds=60)
    else:
        users = cache.get(key)

    # Use decorator for automatic caching
    @cached_query(ttl_seconds=60)
    def get_active_users():
        return User.query().filter(F.active == True).all()

    # Invalidate on writes
    cache.invalidate_pattern("users:*")
    cache.invalidate_table("user")
"""

import functools
import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    """A cached value with metadata."""

    value: T
    created_at: float
    expires_at: Optional[float]
    hits: int = 0
    table: Optional[str] = None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def ttl_remaining(self) -> Optional[float]:
        if self.expires_at is None:
            return None
        remaining = self.expires_at - time.time()
        return max(0, remaining)


@dataclass
class CacheStats:
    """Cache statistics for monitoring."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    expirations: int = 0
    invalidations: int = 0
    size: int = 0
    max_size: int = 0
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "expirations": self.expirations,
            "invalidations": self.invalidations,
            "size": self.size,
            "max_size": self.max_size,
            "hit_rate": self.hit_rate,
            "uptime_seconds": (datetime.now() - self.created_at).total_seconds(),
        }


class QueryCache:
    """Thread-safe in-memory cache for query results.

    Features:
    - LRU eviction when max size is reached
    - TTL-based expiration
    - Table-based invalidation
    - Pattern-based invalidation
    - Comprehensive statistics

    Usage::

        cache = QueryCache(max_size=1000, default_ttl_seconds=300)

        # Set and get
        cache.set("users:active", users, ttl_seconds=60)
        users = cache.get("users:active")

        # Check existence
        if cache.has("users:active"):
            ...

        # Invalidate
        cache.invalidate("users:active")
        cache.invalidate_table("user")
        cache.invalidate_pattern("users:*")

        # Stats
        print(cache.stats.hit_rate)
    """

    def __init__(
        self,
        max_size: int = 1000,
        default_ttl_seconds: Optional[float] = 300,
        cleanup_interval_seconds: float = 60,
    ):
        """Initialize the cache.

        Args:
            max_size: Maximum number of entries
            default_ttl_seconds: Default TTL for entries (None = no expiry)
            cleanup_interval_seconds: How often to run cleanup
        """
        self.max_size = max_size
        self.default_ttl_seconds = default_ttl_seconds
        self.cleanup_interval_seconds = cleanup_interval_seconds

        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._stats = CacheStats(max_size=max_size)
        self._table_keys: dict[str, set[str]] = {}  # table -> set of keys

        # Start background cleanup
        self._cleanup_thread: Optional[threading.Thread] = None
        self._running = False

    def start_cleanup(self) -> None:
        """Start background cleanup thread."""
        if self._running:
            return

        self._running = True
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def stop_cleanup(self) -> None:
        """Stop background cleanup thread."""
        self._running = False

    def _cleanup_loop(self) -> None:
        """Background cleanup loop."""
        while self._running:
            time.sleep(self.cleanup_interval_seconds)
            self._cleanup_expired()

    def _cleanup_expired(self) -> None:
        """Remove expired entries."""
        with self._lock:
            expired_keys = [key for key, entry in self._cache.items() if entry.is_expired]
            for key in expired_keys:
                self._remove_key(key)
                self._stats.expirations += 1

    def _remove_key(self, key: str) -> None:
        """Remove a key (must hold lock)."""
        if key in self._cache:
            entry = self._cache[key]
            del self._cache[key]
            self._stats.size -= 1

            # Remove from table index
            if entry.table and entry.table in self._table_keys:
                self._table_keys[entry.table].discard(key)

    def _evict_lru(self) -> None:
        """Evict least recently used entry (must hold lock)."""
        if self._cache:
            oldest_key = next(iter(self._cache))
            self._remove_key(oldest_key)
            self._stats.evictions += 1

    def set(
        self,
        key: str,
        value: T,
        *,
        ttl_seconds: Optional[float] = None,
        table: Optional[str] = None,
    ) -> None:
        """Set a cache entry.

        Args:
            key: Cache key
            value: Value to cache
            ttl_seconds: TTL in seconds (None uses default)
            table: Associated table name (for invalidation)
        """
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        expires_at = time.time() + ttl if ttl else None

        with self._lock:
            # Evict if at capacity and this is a new key
            if key not in self._cache and len(self._cache) >= self.max_size:
                self._evict_lru()

            # Remove old entry if exists
            if key in self._cache:
                self._remove_key(key)

            # Add new entry
            entry = CacheEntry(
                value=value,
                created_at=time.time(),
                expires_at=expires_at,
                table=table,
            )
            self._cache[key] = entry
            self._stats.size += 1

            # Track by table
            if table:
                if table not in self._table_keys:
                    self._table_keys[table] = set()
                self._table_keys[table].add(key)

    def get(self, key: str, default: T = None) -> Optional[T]:
        """Get a cached value.

        Args:
            key: Cache key
            default: Default value if not found or expired

        Returns:
            Cached value or default
        """
        with self._lock:
            if key not in self._cache:
                self._stats.misses += 1
                return default

            entry = self._cache[key]

            if entry.is_expired:
                self._remove_key(key)
                self._stats.expirations += 1
                self._stats.misses += 1
                return default

            # Move to end (most recently used)
            self._cache.move_to_end(key)
            entry.hits += 1
            self._stats.hits += 1

            return entry.value

    def has(self, key: str) -> bool:
        """Check if a key exists and is not expired."""
        with self._lock:
            if key not in self._cache:
                return False
            entry = self._cache[key]
            if entry.is_expired:
                self._remove_key(key)
                self._stats.expirations += 1
                return False
            return True

    def invalidate(self, key: str) -> bool:
        """Invalidate a specific key.

        Returns:
            True if key was found and removed
        """
        with self._lock:
            if key in self._cache:
                self._remove_key(key)
                self._stats.invalidations += 1
                return True
            return False

    def invalidate_table(self, table: str) -> int:
        """Invalidate all entries for a table.

        Args:
            table: Table name

        Returns:
            Number of entries invalidated
        """
        with self._lock:
            if table not in self._table_keys:
                return 0

            keys = list(self._table_keys[table])
            for key in keys:
                self._remove_key(key)
                self._stats.invalidations += 1

            return len(keys)

    def invalidate_pattern(self, pattern: str) -> int:
        """Invalidate keys matching a pattern.

        Supports simple wildcards:
        - * matches any characters
        - ? matches single character

        Args:
            pattern: Pattern to match (e.g., "users:*")

        Returns:
            Number of entries invalidated
        """
        import fnmatch

        with self._lock:
            matching_keys = [key for key in self._cache.keys() if fnmatch.fnmatch(key, pattern)]
            for key in matching_keys:
                self._remove_key(key)
                self._stats.invalidations += 1

            return len(matching_keys)

    def clear(self) -> int:
        """Clear all entries.

        Returns:
            Number of entries cleared
        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._table_keys.clear()
            self._stats.size = 0
            self._stats.invalidations += count
            return count

    def get_entry(self, key: str) -> Optional[CacheEntry]:
        """Get the full cache entry (for debugging)."""
        with self._lock:
            return self._cache.get(key)

    @property
    def stats(self) -> CacheStats:
        """Get cache statistics."""
        with self._lock:
            self._stats.size = len(self._cache)
            return self._stats

    def reset_stats(self) -> None:
        """Reset statistics (except size)."""
        with self._lock:
            size = len(self._cache)
            self._stats = CacheStats(max_size=self.max_size, size=size)

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def __contains__(self, key: str) -> bool:
        return self.has(key)


# Global cache instance
_cache: Optional[QueryCache] = None


def get_cache() -> QueryCache:
    """Get or create the global cache instance."""
    global _cache
    if _cache is None:
        _cache = QueryCache()
    return _cache


def configure_cache(
    max_size: int = 1000,
    default_ttl_seconds: Optional[float] = 300,
    cleanup_interval_seconds: float = 60,
) -> QueryCache:
    """Configure and return the global cache."""
    global _cache
    _cache = QueryCache(
        max_size=max_size,
        default_ttl_seconds=default_ttl_seconds,
        cleanup_interval_seconds=cleanup_interval_seconds,
    )
    return _cache


def _make_cache_key(func: Callable, args: tuple, kwargs: dict) -> str:
    """Generate a cache key from function call."""
    key_parts = [func.__module__, func.__name__]

    # Add args
    for arg in args:
        try:
            key_parts.append(json.dumps(arg, sort_keys=True, default=str))
        except (TypeError, ValueError):
            key_parts.append(str(id(arg)))

    # Add kwargs
    for k, v in sorted(kwargs.items()):
        try:
            key_parts.append(f"{k}={json.dumps(v, sort_keys=True, default=str)}")
        except (TypeError, ValueError):
            key_parts.append(f"{k}={id(v)}")

    key_str = ":".join(key_parts)

    # Hash if too long
    if len(key_str) > 200:
        return hashlib.md5(key_str.encode()).hexdigest()
    return key_str


def cached_query(
    ttl_seconds: Optional[float] = None,
    key: Optional[str] = None,
    table: Optional[str] = None,
    cache: Optional[QueryCache] = None,
) -> Callable:
    """Decorator to cache query results.

    Args:
        ttl_seconds: Cache TTL (None uses cache default)
        key: Explicit cache key (auto-generated if None)
        table: Table name for invalidation
        cache: Specific cache instance (uses global if None)

    Usage::

        @cached_query(ttl_seconds=60, table="user")
        def get_active_users():
            return User.query().filter(F.active == True).all()

        # With explicit key
        @cached_query(key="dashboard_stats", ttl_seconds=300)
        def get_dashboard_stats():
            return {...}
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            c = cache or get_cache()
            cache_key = key or _make_cache_key(func, args, kwargs)

            # Check cache
            if c.has(cache_key):
                return c.get(cache_key)

            # Execute and cache
            result = func(*args, **kwargs)
            c.set(cache_key, result, ttl_seconds=ttl_seconds, table=table)
            return result

        # Add cache control methods
        wrapper.invalidate = lambda: (cache or get_cache()).invalidate(
            key or _make_cache_key(func, (), {})
        )
        wrapper.cache_key = key

        return wrapper

    return decorator


def async_cached_query(
    ttl_seconds: Optional[float] = None,
    key: Optional[str] = None,
    table: Optional[str] = None,
    cache: Optional[QueryCache] = None,
) -> Callable:
    """Async version of cached_query decorator."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            c = cache or get_cache()
            cache_key = key or _make_cache_key(func, args, kwargs)

            # Check cache
            if c.has(cache_key):
                return c.get(cache_key)

            # Execute and cache
            result = await func(*args, **kwargs)
            c.set(cache_key, result, ttl_seconds=ttl_seconds, table=table)
            return result

        wrapper.invalidate = lambda: (cache or get_cache()).invalidate(
            key or _make_cache_key(func, (), {})
        )
        wrapper.cache_key = key

        return wrapper

    return decorator


class CacheAwareModel:
    """Mixin to automatically invalidate cache on model changes.

    Usage::

        class User(CacheAwareModel, SQLerModel):
            name: str

            class Meta:
                cache_table = "user"

        # Now saves/deletes automatically invalidate the cache
        user.save()  # Invalidates "user" table cache
    """

    @classmethod
    def _get_cache_table(cls) -> Optional[str]:
        """Get the cache table name for this model."""
        if hasattr(cls, "Meta") and hasattr(cls.Meta, "cache_table"):
            return cls.Meta.cache_table
        if hasattr(cls, "__sqler_table__"):
            return cls.__sqler_table__
        return cls.__name__.lower()

    def save(self, *args, **kwargs):
        """Save and invalidate cache."""
        result = super().save(*args, **kwargs)
        table = self._get_cache_table()
        if table:
            get_cache().invalidate_table(table)
        return result

    async def asave(self, *args, **kwargs):
        """Async save and invalidate cache."""
        result = await super().asave(*args, **kwargs)
        table = self._get_cache_table()
        if table:
            get_cache().invalidate_table(table)
        return result

    def delete(self, *args, **kwargs):
        """Delete and invalidate cache."""
        result = super().delete(*args, **kwargs)
        table = self._get_cache_table()
        if table:
            get_cache().invalidate_table(table)
        return result

    async def adelete(self, *args, **kwargs):
        """Async delete and invalidate cache."""
        result = await super().adelete(*args, **kwargs)
        table = self._get_cache_table()
        if table:
            get_cache().invalidate_table(table)
        return result
