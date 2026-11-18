"""What a database object remembers of a table: its columns, its promoted columns and its
version column, for the object as a whole and for each transaction it has open."""

from typing import TYPE_CHECKING, Any, NamedTuple, Optional, Sequence

from sqler.db._guard import _guard
from sqler.db._rows import _promoted_in
from sqler.query.query import _has_version


class _Level(NamedTuple):
    """One open transaction of a database object's own: what it learned about tables, the
    adapter's depth right after its ``begin_transaction`` (the level is the innermost one of the
    adapter's only while the adapter's depth is still this number), and the tables of cached
    results written in it (``sqler.cache``), whose results are not cached until it ends."""

    versioned: set[str]
    promoted: dict[str, list[str]]
    layouts: dict[str, tuple[str, ...]]
    depth: int
    written: set[str]


class _TableMemory:
    """What a database object remembers of its tables: the names ``SELECT *`` gives for each
    (``_layouts``), which of them have ``_version`` (``_versioned_tables``), and the promoted
    columns of each it has seen settled (``_promoted_columns``); each open level of its own
    transactions remembers what it learned in it (``_levels()``).

    The promoted columns and the version of a table hold for the names remembered beside them.
    A statement built from them carries the guard of those names (:func:`_guard`), so SQLite
    refuses it once the table has other columns; only then is the table learned again.
    The class provides ``_levels()`` and ``_adapter_depth()``.
    """

    if TYPE_CHECKING:

        def _levels(self) -> Sequence[_Level]: ...

        def _adapter_depth(self) -> int: ...

    def _init_memory(self) -> None:
        self._versioned_tables: set[str] = set()
        self._promoted_columns: dict[str, list[str]] = {}
        self._layouts: dict[str, tuple[str, ...]] = {}
        # tables seen settled whose record could not be written then: the next ensure writes it
        self._unrecorded: set[str] = set()

    def _learned(
        self,
    ) -> Optional[tuple[set[str], dict[str, list[str]], dict[str, tuple[str, ...]]]]:
        """Where a statement's lesson about a table is kept.

        The innermost open level of this object's own transactions, if the adapter's depth is
        still that level's; with no level open, the shared memory, at depth 0. Anywhere else
        the transaction is not this object's (the adapter used directly, another thread's on
        the single ``":memory:"`` connection): nowhere. That transaction holds the write lock,
        so a statement built from a table read in it is not refused for a change made since.
        """
        depth = self._adapter_depth()
        levels = self._levels()
        if levels:
            level = levels[-1]
            return (
                (level.versioned, level.promoted, level.layouts) if level.depth == depth else None
            )
        return (
            (self._versioned_tables, self._promoted_columns, self._layouts) if depth == 0 else None
        )

    def _remember(
        self, table: str, names: tuple[str, ...], *, promoted: Optional[list[str]] = None
    ) -> None:
        """Remember that ``table`` has the columns ``names`` (as ``SELECT *`` gives them), and,
        when given, its ``promoted`` columns. Promoted columns remembered for other names are
        forgotten."""
        learned = self._learned()
        if learned is not None:
            _put(learned, table, names, promoted)

    def _forget(self, table: str) -> None:
        """Forget everything remembered about ``table``, in every level and in the shared memory."""
        places = [(level.versioned, level.promoted, level.layouts) for level in self._levels()]
        for versioned, promoted, layouts in [
            *places,
            (self._versioned_tables, self._promoted_columns, self._layouts),
        ]:
            versioned.discard(table)
            promoted.pop(table, None)
            layouts.pop(table, None)

    def _merge(self, level: _Level) -> None:
        """``level`` committed: what it learned goes to the level above, or at the outermost
        level to the shared memory."""
        if not level.layouts:
            return  # it learned nothing: nothing to move
        above = self._learned()
        if above is not None:
            for table, names in level.layouts.items():
                _put(above, table, names, level.promoted.get(table))

    def _written_level(self) -> Optional[set[str]]:
        """The tables of cached results written in this object's innermost open transaction
        (``_Level.written``); ``None`` with none open. A level whose depth is above the
        adapter's has ended: a task started inside a transaction keeps that transaction's
        levels after it."""
        levels = self._levels()
        if levels and levels[-1].depth <= self._adapter_depth():
            return levels[-1].written
        return None

    def _known(self, table: str) -> Optional[tuple[tuple[str, ...], Optional[list[str]], bool]]:
        """The names, promoted columns (``None``: not checked yet) and version remembered for
        ``table`` here and now, from the innermost place that remembers its names."""
        levels = self._levels()
        for level in reversed(levels) if levels else ():
            names = level.layouts.get(table)
            if names is not None:
                return names, level.promoted.get(table), table in level.versioned
        names = self._layouts.get(table)
        return (
            None
            if names is None
            else (names, self._promoted_columns.get(table), table in self._versioned_tables)
        )

    def _is_versioned(self, table: str) -> bool:
        """Whether ``table`` is remembered to have its ``_version`` column, here and now."""
        known = self._known(table)
        return known is not None and known[2]

    def _promoted_of(self, table: str) -> Optional[list[str]]:
        """The promoted columns remembered for ``table`` here and now; ``None`` if it is not remembered."""
        known = self._known(table)
        return None if known is None else known[1]

    def _layout_of(self, table: str) -> Optional[tuple[str, ...]]:
        """The names ``SELECT *`` gives for ``table``, as remembered here and now; ``None`` if not remembered."""
        known = self._known(table)
        return None if known is None else known[0]

    def _seen(
        self, table: str, layout: tuple[str, ...], names: list[str], added: tuple[str, ...] = ()
    ) -> None:
        """Remember ``table`` as just read (``_names``), with the columns ``added`` since by
        this object: the promoted columns remembered for that layout stay, and a table with
        none is remembered as having none (an ensure's check needs no statement for it)."""
        promoted = self._promoted_of(table) if self._layout_of(table) == layout else None
        if promoted is None and not _promoted_in(names):
            promoted = []
        self._remember(table, (*layout, *added), promoted=promoted)

    def _guard_of(self, table: str, names: Optional[tuple[str, ...]]) -> str:
        """The guard (:func:`_guard`) of ``names``, those a statement on ``table`` is built
        from; empty for none. The caller passes the names it read, so the guard and what the
        statement is kept under cannot differ."""
        return "" if names is None else _guard(table, names)


def _put(place: Any, table: str, names: tuple[str, ...], promoted: Optional[list[str]]) -> None:
    """Remember in ``place`` (a level's or the shared memory's sets) that ``table`` has the
    columns ``names``, and its ``promoted`` columns when given; promoted columns remembered
    for other names are dropped."""
    versioned, known, layouts = place
    if layouts.get(table) != names:
        known.pop(table, None)
    layouts[table] = names
    (versioned.add if _has_version(names) else versioned.discard)(table)
    if promoted is not None:
        known[table] = promoted
