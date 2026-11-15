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
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Generic, Optional, TypeVar, Union

from sqler.models._table_names import _unchecked_table_name
from sqler.models._write_rules import WriteRules

T = TypeVar("T")

# Every QueryCache, so a write is told to each (``_tables_written``): replaced, never changed
# in place, so a write reads it without a lock.
_caches: tuple["weakref.ref[QueryCache]", ...] = ()
# How many open transaction levels of this process wrote each table: while one has, no
# cache serves or stores an entry of the table.
_held: dict[str, int] = {}
# taken to change ``_caches`` or ``_held``
_held_lock = threading.Lock()


def _table_name(table: Union[str, type, None]) -> Optional[str]:
    """The name an entry of ``table`` is kept under: a model class stands for its table, and
    SQLite does not tell table names apart by case."""
    if table is None:
        return None
    return (table if isinstance(table, str) else _unchecked_table_name(table)).lower()


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
        # table -> how many times it was written; a result is stored only for the number
        # its function started at
        self._generations: dict[str, int] = {}
        global _caches
        with _held_lock:
            _caches = (*(ref for ref in _caches if ref() is not None), weakref.ref(self))

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
        table: Union[str, type, None] = None,
    ) -> None:
        """Set a cache entry.

        While a transaction of this process that wrote ``table`` is open, nothing is stored.

        Args:
            key: Cache key
            value: Value to cache
            ttl_seconds: TTL in seconds (None uses default)
            table: Associated table name or model class (for invalidation)
        """
        self._store(key, value, ttl_seconds, _table_name(table), None)

    def _store(
        self, key: str, value: Any, ttl_seconds: Optional[float], table: Optional[str], generation: Optional[int]
    ) -> None:
        """Store ``value``, unless ``table`` is held or was written since ``generation``
        (``None``: not checked)."""
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        expires_at = time.time() + ttl if ttl else None

        with self._lock:
            if table is not None and (
                table in _held or (generation is not None and self._generations.get(table, 0) != generation)
            ):
                return
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
            entry = self._cache.get(key)
            if entry is None or entry.table in _held:
                self._stats.misses += 1
                return default

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
            entry = self._cache.get(key)
            if entry is None or entry.table in _held:
                return False
            if entry.is_expired:
                self._remove_key(key)
                self._stats.expirations += 1
                return False
            return True

    def _lookup(self, key: str, table: Optional[str]) -> tuple[bool, Any]:
        """``(True, value)`` for an entry to serve; else ``(False, generation)``: the number
        of writes of ``table`` a result computed now is stored for, ``None`` while ``table``
        is held (the result is not stored)."""
        with self._lock:
            if table in _held:
                return False, None
            entry = self._cache.get(key)
            if entry is not None:
                if not entry.is_expired:
                    self._cache.move_to_end(key)
                    entry.hits += 1
                    self._stats.hits += 1
                    return True, entry.value
                self._remove_key(key)
                self._stats.expirations += 1
            return False, self._generations.get(table, 0)

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

    def invalidate_table(self, table: Union[str, type]) -> int:
        """Invalidate all entries for a table.

        Args:
            table: Table name or model class

        Returns:
            Number of entries invalidated
        """
        name = _table_name(table)
        with self._lock:
            return self._drop_table(name)

    def _drop_table(self, table: str) -> int:
        """Drop the entries of ``table`` (must hold lock)."""
        keys = self._table_keys.pop(table, ())
        for key in keys:
            del self._cache[key]
            self._stats.size -= 1
            self._stats.invalidations += 1
        return len(keys)

    def _written(self, tables) -> None:
        """``tables`` were written: their entries are dropped, and a result its function
        started computing before is not stored."""
        with self._lock:
            for table in tables:
                self._generations[table] = self._generations.get(table, 0) + 1
                self._drop_table(table)

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
    table: Union[str, type, None] = None,
    cache: Optional[QueryCache] = None,
) -> Callable:
    """Decorator to cache query results.

    Args:
        ttl_seconds: Cache TTL (None uses cache default)
        key: Explicit cache key (auto-generated if None)
        table: The table the function reads, or its model class: a write of a class with
            :class:`CacheAwareModel` drops the entries of its table. Without a table an
            entry ends only with its lifetime.
        cache: Specific cache instance (uses global if None)

    Usage::

        @cached_query(ttl_seconds=60, table=User)
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
            name = _table_name(table)
            found, value = c._lookup(cache_key, name)
            if found:
                return value
            result = func(*args, **kwargs)
            if value is not None:
                c._store(cache_key, result, ttl_seconds, name, value)
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
    table: Union[str, type, None] = None,
    cache: Optional[QueryCache] = None,
) -> Callable:
    """Async version of cached_query decorator."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            c = cache or get_cache()
            cache_key = key or _make_cache_key(func, args, kwargs)
            name = _table_name(table)
            found, value = c._lookup(cache_key, name)
            if found:
                return value
            result = await func(*args, **kwargs)
            if value is not None:
                c._store(cache_key, result, ttl_seconds, name, value)
            return result

        wrapper.invalidate = lambda: (cache or get_cache()).invalidate(
            key or _make_cache_key(func, (), {})
        )
        wrapper.cache_key = key

        return wrapper

    return decorator


# ----- what a write and the end of a transaction tell the caches --------------------------


def _tables_written(db, tables: set) -> None:
    """``tables`` (names as ``_table_name`` gives them) were just written through ``db``. Inside
    a transaction of ``db``'s own each is held until that transaction ends
    (``level_ended``); every cache drops their entries. ``db`` is ``None`` for a queryset built
    by hand without a database: no transaction of a database object is open around it."""
    written = db._written_level() if db is not None else None
    if written is not None:
        with _held_lock:
            for name in tables - written:
                written.add(name)
                _held[name] = _held.get(name, 0) + 1
    _tell_caches(tables)


def level_ended(written: set, above: Optional[set]) -> None:
    """A transaction level that wrote the tables ``written`` ended (any way it ends): each is
    held one level less, or from now on by ``above`` (the level a commit hands them to,
    ``None`` at the outermost or on a rollback). Nothing is dropped here: no entry of a held
    table can have been stored since the write dropped them."""
    with _held_lock:
        for name in written:
            if above is not None and name not in above:
                above.add(name)
            elif _held.get(name, 0) > 1:
                _held[name] -= 1
            else:
                _held.pop(name, None)


def _tell_caches(tables) -> None:
    for ref in _caches:
        c = ref()
        if c is not None:
            c._written(tables)


class CacheAwareModel(WriteRules):
    """Mixin to keep cached results of the model's table fresh.

    Every way of writing the model's rows (``save()``, ``save_partial()``, ``save_many()``,
    ``delete()``, ``delete_with_policy()``, a queryset's ``update()``, ``update_one()``,
    ``update_n()`` and ``delete_all()``, sync and async, and the rows a delete's policy
    changes) drops the cached entries of its table (and of ``Meta.cache_table``, where the
    class sets one); inside a transaction no entry of the table is served or stored until
    it ends.

    Usage::

        class User(CacheAwareModel, SQLerModel):
            name: str

        @cached_query(table=User)
        def users():
            return User.query().all()

        user.save()  # the next users() reads the table
    """

    @classmethod
    def _get_cache_table(cls) -> str:
        """The name of this class's cached results besides its table: ``Meta.cache_table``
        where the class sets one, else its table."""
        return getattr(getattr(cls, "Meta", None), "cache_table", None) or _unchecked_table_name(cls)

    @classmethod
    def _table_written(cls, db, table: str) -> None:
        _tables_written(db, {table.lower(), cls._get_cache_table().lower()})
        super()._table_written(db, table)
