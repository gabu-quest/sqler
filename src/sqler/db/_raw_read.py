import json
from typing import Any, Optional

# ---- a raw read (``execute_sql``): which statements it takes, and how its rows become results ----


_READ_WORDS = ("SELECT", "EXPLAIN", "PRAGMA", "WITH")

# a column is found by its name as ``sqlite3.Row`` finds it: ASCII letters in either case
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _read_only(query: str) -> None:
    """Refuse ``query`` unless it is one read-only statement (``SELECT``, ``EXPLAIN``,
    ``PRAGMA`` or ``WITH``; a semicolon only at its end)."""
    stripped = query.strip()
    first_word = stripped.split()[0].upper() if stripped else ""
    if first_word not in _READ_WORDS:
        raise ValueError("execute_sql only accepts read-only queries (SELECT/EXPLAIN/PRAGMA/WITH)")
    if ";" in stripped.rstrip(";"):
        raise ValueError("execute_sql does not allow multi-statement queries")


def _sql_results(description: Any, rows: Any) -> list[dict[str, Any]]:
    """The rows of a raw read as ``execute_sql`` returns them, read by the names of their
    columns (``description``, the cursor's): with a ``data`` column, its JSON document, with
    ``_id`` set from an ``_id`` column when there is one; with ``_id`` the only column,
    ``{"_id": id}``; else a dict of every column by its name. A name given twice holds the
    first column of that name, and a name is found in either case of its ASCII letters."""
    if not rows:
        return []
    names = [d[0] for d in description]
    folded = [n.translate(_ASCII_LOWER) for n in names]

    def at(name: str) -> Optional[int]:
        key = name.translate(_ASCII_LOWER)
        return folded.index(key) if key in folded else None

    if "data" in names:
        data, rid = at("data"), at("_id") if "_id" in names else None
        loads = json.loads
        docs = []
        for row in rows:
            doc = loads(row[data])
            if rid is not None:
                doc["_id"] = int(row[rid])
            docs.append(doc)
        return docs
    if names == ["_id"]:
        return [{"_id": int(row[0])} for row in rows]
    picks = [(name, at(name)) for name in names]
    return [{name: row[i] for name, i in picks} for row in rows]
