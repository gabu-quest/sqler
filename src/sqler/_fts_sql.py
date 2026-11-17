"""The SQL text of a search index: its triggers, the statement that fills it, how a stored
``CREATE VIRTUAL TABLE ... USING fts5(...)`` reads, and the FTS5 query of a plain text.

Nothing here sends a statement or reads a database; ``sqler.fts`` holds the classes that do.
"""

import re
from typing import Any, Optional

from sqler.exceptions import SchemaError
from sqler.utils import validate_identifier

# The triggers that keep a content_sync index equal to its table: name suffix -> event.
_SYNC_TRIGGERS = {"ai": "INSERT", "au": "UPDATE", "ad": "DELETE"}

# The arguments of a stored ``fts5(...)``: runs of anything but a comma, a quoted run whole.
_FTS5_ARG = re.compile(r"""(?:'[^']*'|"[^"]*"|`[^`]*`|\[[^\]]*\]|[^,'"`\[])+""")
_FTS5_BODY = re.compile(r"(?is)\s*CREATE\s+VIRTUAL\s+TABLE\s+\S+\s+USING\s+fts5\s*\((.*)\)\s*;?\s*")


def _unquote(word: str) -> str:
    """An FTS5 argument word without its quotes (a doubled quote inside is one)."""
    if len(word) >= 2 and word[0] + word[-1] in ("''", '""', "``", "[]"):
        return word[1:-1].replace(word[0] * 2, word[0]) if word[0] != "[" else word[1:-1]
    return word


def _stored_definition(sql: Optional[str]) -> Optional[tuple[list[str], str, list[str]]]:
    """What a stored ``CREATE VIRTUAL TABLE ... USING fts5(...)`` defines: its columns, its
    tokenizer with single spaces (``unicode61``, FTS5's default, when it names none) and
    any other argument as written. ``None`` when the statement is not an FTS5 table.

    This compares what the index is, not how its statement is spelled: the released
    version's statement, with its line breaks, defines the same index as today's.
    """
    found = _FTS5_BODY.fullmatch(sql or "")
    if found is None:
        return None
    columns, tokenizer, other = [], "unicode61", []
    for arg in (a.strip() for a in _FTS5_ARG.findall(found.group(1))):
        key, eq, value = arg.partition("=")
        if eq and key.strip().lower() == "tokenize":
            tokenizer = " ".join(_unquote(value.strip()).split())
        elif eq or len(arg.split()) != 1:
            other.append(arg)  # content=, prefix=, a column with UNINDEXED: not sqler's index
        else:
            columns.append(_unquote(arg))
    return columns, tokenizer, other


# ---- the statements of a content_sync index, from its names and its table's promoted columns ----


def _values_sql(fields: list[str], promoted: Any, row: str) -> list[str]:
    """Each field of the stored row ``row`` where it is stored: a promoted column by its
    name, any other field from ``data`` with ``json_extract``."""
    return [f"{row}.{f}" if f in promoted else f"json_extract({row}.data, '$.{f}')" for f in fields]


def _fill_sql(idx: str, fields: list[str], promoted: Any, row: str) -> str:
    """The ``INSERT`` of the index row of the stored row ``row`` (``new`` in a trigger, an
    alias of the table in a select). The triggers, ``rebuild()``, ``create()``, ``index()``
    and a promotion all use it, so they give the same text for a row."""
    return f"INSERT INTO {idx}(rowid, {', '.join(fields)}) SELECT {row}._id, {', '.join(_values_sql(fields, promoted, row))}"


def _trigger_sql(idx: str, table: str, fields: list[str], promoted: Any) -> dict[str, str]:
    """The statement of each trigger of a content_sync index, as SQLite stores it.

    Every name in it is checked by the caller: the fields, the index name and the table. The
    update trigger fires only when the row id or the stored value of an indexed field changed,
    compared so that ``1`` and ``1.0`` differ as they do in the index: the fields read from
    ``data`` as one ``json_array()``, a promoted column with ``quote()`` (a column may hold a
    blob, which JSON cannot).
    """
    in_data = [f for f in fields if f not in promoted]
    changed = ["old._id IS NOT new._id"]
    if in_data:
        old, new = (
            "json_array(" + ", ".join(f"json_extract({row}.data, '$.{f}')" for f in in_data) + ")"
            for row in ("old", "new")
        )
        changed.append(f"{old} IS NOT {new}")
    changed += [f"quote(old.{f}) IS NOT quote(new.{f})" for f in fields if f in promoted]
    forget = f"DELETE FROM {idx} WHERE rowid = old._id;"
    fill = f"{_fill_sql(idx, fields, promoted, 'new')};"
    body = {
        "INSERT": f"BEGIN DELETE FROM {idx} WHERE rowid = new._id; {fill} END",
        "UPDATE": f"WHEN {' OR '.join(changed)} BEGIN {forget} {fill} END",
        "DELETE": f"BEGIN {forget} END",
    }
    return {
        f"{idx}_{suffix}": f"CREATE TRIGGER {idx}_{suffix} AFTER {event} ON {table} {body[event]}"
        for suffix, event in _SYNC_TRIGGERS.items()
    }


def _follow_promotion(table: str, moved: list[str], promoted: list[str]) -> Any:
    """After the values of ``moved`` left the documents of ``table`` for their columns: each
    content_sync index on ``table`` with a field among them gets the triggers that read those
    fields from their columns, and is filled again from the table. ``promoted``: every promoted
    column of the table now.

    A run of statements the database classes send (see ``sqler.db._promotion._promotion``), in
    the transaction of the promotion: an index made before it would index NULL for a moved field.
    """
    rows = yield (
        "SELECT type, name, sql FROM sqlite_master WHERE type = 'table' OR (type = 'trigger' AND tbl_name = ?);",
        [table],
    )
    triggers = {name.lower() for kind, name, _ in rows if kind == "trigger"}
    moving = {c.lower() for c in moved}
    for kind, idx, sql in rows:
        found = _stored_definition(sql) if kind == "table" else None
        if found is None or found[2] or not {f"{idx}_{s}".lower() for s in _SYNC_TRIGGERS} <= triggers:
            continue
        fields = found[0]
        if not moving & {f.lower() for f in fields}:
            continue
        try:
            for name in (idx, *fields):
                validate_identifier(name)
        except SchemaError:
            continue  # not an index sqler made: sqler names nothing it cannot check
        for name, statement in _trigger_sql(idx, table, fields, promoted).items():
            yield f"DROP TRIGGER IF EXISTS {name};", []
            yield statement, []
        yield f"DELETE FROM {idx};", []
        yield f"{_fill_sql(idx, fields, promoted, 't')} FROM {table} AS t;", []


def _plain_query(text: str) -> str:
    """The FTS5 query that finds every word of ``text``.

    Each run of characters between spaces becomes one FTS5 string, with ``"`` doubled
    inside it (a NUL counts as a space: SQLite ends the query text there). FTS5 hands a
    string to the tokenizer as text and reads no syntax in it, so no character of
    ``text`` is an operator; strings side by side must all match. Text with no run gives
    ``""``, which matches nothing.
    """
    runs = text.replace("\x00", " ").split()
    return " ".join('"' + run.replace('"', '""') + '"' for run in runs) or '""'


# Whether the model's table exists (``rebuild()``), and whether a database has a table of
# this index's name (the mixin's ``content_sync=False`` step in another database).
_HAS_TABLE_SQL = "SELECT name FROM sqlite_master WHERE type='table' AND name=?;"
_HAS_INDEX_TABLE_SQL = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND lower(name) = lower(?);"


def _drop_trigger_sql(name: str) -> str:
    return f"DROP TRIGGER IF EXISTS {name};"
