"""The guard a statement carries so SQLite refuses it once the table has other columns than the
ones the statement was built from."""

import functools
from typing import Any


def _quoted(name: str) -> str:
    """``name`` as an identifier only: a double-quoted name SQLite cannot resolve would be read
    as a string, and the guard would hold for a column that is gone."""
    return "`" + name.replace("`", "``") + "`"


@functools.lru_cache(maxsize=512)
def _guard(table: str, names: tuple[str, ...], value: bool = False) -> str:
    """The guard a statement built from what is remembered of the validated ``table`` carries:
    a ``CASE`` whose ``ELSE`` never runs and reads the row value of ``names`` against
    ``SELECT *`` of the table. SQLite checks it each time it prepares the statement, so the
    statement is refused once the table has other columns (a name it lacks by its name,
    another count as "sub-select returns N columns"), and runs as before while it has these.

    ``IN``, not ``=``: SQLite resolves the names before it compares the counts, and a name a
    connection does not find makes it reload a schema another connection changed, so a
    connection that has not seen a column this object learned elsewhere runs the statement.

    A term of a ``WHERE``, which is 1; with ``value``, an expression to put in place of one
    ``?`` of an ``INSERT``, which is that variable."""
    row = ", ".join(_quoted(name) for name in names)
    return f"CASE WHEN 1 THEN {'?' if value else '1'} ELSE (SELECT ({row}) IN (SELECT * FROM {table}) FROM {table}) END"


def _and_guard(table: str, names: Any) -> str:
    """`` AND `` and the guard of ``names`` (:func:`_guard`), to end a ``WHERE``; empty without ``names``."""
    return f" AND {_guard(table, names)}" if names else ""


@functools.lru_cache(maxsize=512)
def _check_sql(table: str, names: tuple[str, ...]) -> str:
    """The guard of ``names`` (:func:`_guard`) as a statement of its own, which reads no row:
    sent first in a transaction, it holds for the statements after it there that are built
    from those names and carry no guard (the bulk writes, the triggers of a search index)."""
    return f"SELECT {_guard(table, names)};"
