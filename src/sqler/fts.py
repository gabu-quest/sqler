"""Full-Text Search (FTS5) integration for SQLer.

Enables efficient text search using SQLite's FTS5 extension.

Usage::

    from sqler.fts import FTSIndex

    # Create an FTS index for a model
    class Article(SQLerModel):
        title: str
        content: str
        author: str

    # Create FTS index
    fts = FTSIndex(Article, fields=["title", "content"])
    fts.create()  # The FTS5 table, its triggers, and the rows already in the table

    # Search
    results = fts.search("python tutorial")
    results = fts.search("python OR rust", limit=10)
    results = fts.search('"exact phrase"')
    results = fts.search("C++ it's foo-bar", plain=True)  # words, not a query

    # Ranked results
    results = fts.search_ranked("machine learning", limit=20)

    # Highlights
    results = fts.search_with_highlights("python", highlight_start="<b>", highlight_end="</b>")

On an ``AsyncSQLerDB`` the same calls are awaited, on ``AsyncFTSIndex`` and
``AsyncSearchableMixin``.
"""

import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Optional, Tuple, Type, Union

from sqler.db._guard import _check_sql, _guard
from sqler.db.async_db import AsyncSQLerDB, _afetch_all
from sqler.db.sqler_db import SQLerDB
from sqler.exceptions import SchemaError, SearchQueryError
from sqler.models._compat import field_types
from sqler.models._table_names import _instance_binding
from sqler.utils import validate_field_name, validate_identifier

if TYPE_CHECKING:
    from sqler.models import SQLerModel
    from sqler.models._host import ModelHost

    # SearchableMixin reads the fields and the save, delete and from_ids of a pydantic model;
    # AsyncSearchableMixin awaits what its model's save and delete return
    _SearchHost = SQLerModel
    _AsyncSearchHost = ModelHost
else:
    _SearchHost = object
    _AsyncSearchHost = object

# Tokenizer values: alphanumeric, spaces, underscores only.
# Covers all standard FTS5 tokenizers (porter, unicode61, ascii, trigram)
# and numeric options (e.g. "unicode61 remove_diacritics 2").
_TOKENIZER_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9_ ]*")


def _check_tokenizer(tokenizer: object) -> str:
    if not isinstance(tokenizer, str) or not _TOKENIZER_PATTERN.fullmatch(tokenizer):
        raise ValueError(
            f"Invalid tokenizer: {tokenizer!r}. "
            f"Must contain only alphanumeric characters, spaces, and underscores."
        )
    return tokenizer


# What SQLite says of a MATCH string it cannot read as an FTS5 query.
_REFUSED_QUERY = ("fts5: ", "unterminated string", "no such column: ", "unknown special query: ")

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


@dataclass
class SearchResult:
    """A search result with relevance info."""

    model: "SQLerModel"
    score: float
    highlights: Optional[dict[str, str]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "_id": self.model._id,
            "score": self.score,
            "highlights": self.highlights,
        }


@dataclass
class FTSStats:
    """FTS index statistics."""

    table_name: str
    indexed_rows: int
    total_tokens: int
    fields: list[str]


def _check_search_field(name: str) -> None:
    """A search field is a column of the index table: a plain name, never a dotted path.

    A malformed name raises InvalidFieldNameError; a well-formed dotted one
    (``meta.title``) raises InvalidIdentifierError, as ``create()`` does.
    """
    validate_field_name(name)
    validate_identifier(name)



# ---- what the sync and the async index share: the statements, the checks, the results ----
#
# ``FTSIndex`` and ``AsyncFTSIndex`` each hold only their order of database calls; every
# statement they send and every rule without I/O is here, written once.

# Whether the model's table exists (``rebuild()``), and whether a database has a table of
# this index's name (the mixin's ``content_sync=False`` step in another database).
_HAS_TABLE_SQL = "SELECT name FROM sqlite_master WHERE type='table' AND name=?;"
_HAS_INDEX_TABLE_SQL = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND lower(name) = lower(?);"


def _drop_trigger_sql(name: str) -> str:
    return f"DROP TRIGGER IF EXISTS {name};"


def _check_step(table: str, layout: Optional[tuple[str, ...]]) -> Optional[tuple[str, list[Any], Any]]:
    """The check of the names a database object remembers of ``table`` (``_check_sql``), as
    the step its ``_sent`` sends; none when it remembers none."""
    return (_check_sql(table, layout), [], layout) if layout else None


def _search_error(query: str, exc: sqlite3.OperationalError, sql: str, params: list) -> Optional[SearchQueryError]:
    """The ``SearchQueryError`` of a ``MATCH`` statement SQLite refused for its query; ``None``
    when ``exc`` is about something else, which is raised as it is."""
    if not str(exc).startswith(_REFUSED_QUERY):
        return None
    return SearchQueryError(f"search query {query!r} refused by SQLite: {exc}", sql=sql, params=params)


def _is_text(annotation: Any) -> bool:
    """Whether a field of this declared type is indexed by default: ``str``, or a type made
    with ``str`` among its arguments (``Optional[str]``, ``str | None``, ``list[str]``)."""
    return annotation is str or str in getattr(annotation, "__args__", ())


def _fts_config(cls: type) -> Tuple[list[str], str]:
    """The fields and tokenizer of a searchable class: its ``FTS`` class, else every public
    field that declares text (:func:`_is_text`), on every model base (``field_types``)."""
    if hasattr(cls, "FTS"):
        fts = getattr(cls, "FTS")
        return getattr(fts, "fields", []), getattr(fts, "tokenizer", "porter unicode61")
    return [name for name, declared in field_types(cls).items() if _is_text(declared)], "porter unicode61"


def _lacks_columns(promoted: Any, columns: Any) -> bool:
    """Whether a promoted field of the class has no column in the table yet."""
    return any(c not in columns for c in promoted)


class _SearchIndex:
    """The definition of a search index and every statement it sends, shared by
    :class:`FTSIndex` and :class:`AsyncFTSIndex`, which add the calls."""

    # the database of the other kind, which this index refuses with ``_refusal``
    _refuses: ClassVar[type] = object
    _refusal: ClassVar[str] = ""

    def __init__(
        self,
        model_class: Type[Any],
        fields: list[str],
        *,
        index_name: Optional[str] = None,
        tokenizer: str = "porter unicode61",
        content_sync: bool = True,
        db: Any = None,
    ):
        """Create an FTS index configuration.

        Args:
            model_class: Model class to index
            fields: Fields to include in the index
            index_name: Custom index table name (default: {table}_fts)
            tokenizer: FTS5 tokenizer (default: porter unicode61)
            content_sync: ``create()`` adds triggers that update the index on every
                insert, update and delete of the table, whoever writes. With
                ``False`` the index changes only through ``index()``, ``remove()``
                and ``rebuild()``.
            db: Database holding the model's rows. Needed when the class was never
                bound with ``set_db()``; a ``db`` given to ``create()``,
                ``drop()`` or ``rebuild()`` replaces it.
        """
        # Validate all inputs that will be interpolated into SQL
        for f in fields:
            _check_search_field(f)
        if index_name is not None:
            validate_identifier(index_name)
        _check_tokenizer(tokenizer)

        self.model_class = model_class
        self.fields = fields
        self.tokenizer = tokenizer  # checked by the setter
        self.content_sync = content_sync

        self.index_table = index_name  # Set later if needed

        # The database given here, else the class binding (see _get_db)
        self._db: Any = db

    @property
    def table(self) -> str:
        """The model's table, by the one rule every model route uses.

        The table bound with ``set_db()``, else ``__tablename__``, else the plural
        of the class name (``Article`` -> ``articles``); checked before it is used.
        """
        from sqler.models._table_names import _model_table_name

        return _model_table_name(self.model_class)

    @property
    def fields(self) -> list[str]:
        """The indexed field names (a copy: changing it changes nothing)."""
        return list(self._fields)

    @fields.setter
    def fields(self, value: list[str]) -> None:
        """Set the indexed field names (checked like the constructor's ``fields``)."""
        names = list(value)
        for f in names:
            _check_search_field(f)
        self._fields = tuple(names)

    @property
    def tokenizer(self) -> str:
        """The FTS5 tokenizer."""
        return self._tokenizer

    @tokenizer.setter
    def tokenizer(self, value: str) -> None:
        """Set the tokenizer (checked like the constructor's ``tokenizer``)."""
        self._tokenizer = _check_tokenizer(value)

    @property
    def index_table(self) -> str:
        """Get the FTS index table name."""
        if self._index_table:
            return self._index_table
        return f"{self.table}_fts"

    @index_table.setter
    def index_table(self, value: Optional[str]) -> None:
        """Set the index table name (checked like the constructor's ``index_name``)."""
        if value is not None:
            validate_identifier(value)
        self._index_table = value

    def _held_db(self) -> Any:
        """The database given to this index, else the one the model class is bound to, else
        ``None``."""
        return self._db if self._db is not None else getattr(self.model_class, "_db", None)

    def _get_db(self) -> Any:
        """The database given to this index, else the one the model class is bound to.

        Raises:
            ValueError: There is neither.
            TypeError: It is a database of the other kind (``_refuses``).
        """
        db = self._held_db()
        if db is None:
            raise ValueError(
                "Model has no database bound: pass db= to the class's search call, to "
                f"{type(self).__name__} or to create()/rebuild()"
            )
        if isinstance(db, self._refuses):
            raise TypeError(self._refusal)
        return db

    # ---- create(): what it reads, what it decides, what it sends ----

    def _trigger_names(self) -> dict[str, str]:
        """The index's triggers: name -> event."""
        return {f"{self.index_table}_{suffix}": event for suffix, event in _SYNC_TRIGGERS.items()}

    def _lookup(self) -> tuple[str, list[str]]:
        """The read of what ``create()`` compares: the index table, the model's table and the
        index's triggers, by their names in lower case."""
        names = [n.lower() for n in (self.index_table, self.table, *self._trigger_names())]
        return (
            f"SELECT name, type, sql FROM sqlite_master WHERE lower(name) IN ({', '.join('?' * len(names))});",
            names,
        )

    def _found(self, rows: list) -> dict[str, tuple[str, Optional[str]]]:
        """The rows of :meth:`_lookup` by lower-case name: (type, statement). The index table,
        if found, must be this definition (SchemaError before any statement)."""
        stored = {name.lower(): (kind, sql) for name, kind, sql in rows}
        index = stored.get(self.index_table.lower())
        if index is not None:
            self._check_definition(index)
        return stored

    def _create_sql(self) -> str:
        return (
            f"CREATE VIRTUAL TABLE {self.index_table} USING fts5"
            f"({', '.join(self.fields)}, tokenize='{self.tokenizer}');"
        )

    def _stale_triggers(self, stored: dict) -> list[str]:
        """Without ``content_sync``: the triggers of this index that exist, to be dropped."""
        return [name for name in self._trigger_names() if stored.get(name.lower(), ("",))[0] == "trigger"]

    def _promoted(self) -> Any:
        return getattr(self.model_class, "__promoted__", None) or {}

    def _table_args(self, promoted: Any) -> dict[str, Any]:
        """The arguments of the ``ensure_table`` a trigger needs (it reads a promoted field
        from its column)."""
        return {"promoted": promoted or None, "checks": getattr(self.model_class, "__checks__", None)}

    def _triggers(self, columns: Any) -> dict[str, str]:
        """The statement of each trigger of this content_sync index (see :func:`_trigger_sql`),
        for a table whose promoted columns are ``columns``."""
        return _trigger_sql(self.index_table, self.table, self.fields, columns)

    @staticmethod
    def _differing(stored: dict, triggers: dict[str, str]) -> list[tuple[str, str]]:
        """The triggers that are missing or are not the statement this definition writes now."""
        return [(name, sql) for name, sql in triggers.items() if stored.get(name.lower()) != ("trigger", sql)]

    def _check_definition(self, index: tuple[str, Optional[str]]) -> None:
        """Raise SchemaError unless the stored ``index`` (type, statement) is this definition."""
        kind, sql = index
        found = _stored_definition(sql) if kind == "table" else None
        if found is None:
            raise SchemaError(
                f"{self.index_table!r} is not a search index (an FTS5 table): "
                f"give {type(self).__name__} another index_name"
            )
        columns, tokenizer, other = found
        asked = " ".join(self.tokenizer.split())
        if [c.lower() for c in columns] != [f.lower() for f in self.fields] or tokenizer != asked or other:
            extra = f" and arguments {other}" if other else ""
            raise SchemaError(
                f"search index {self.index_table!r} has fields {columns} and tokenizer "
                f"{tokenizer!r}{extra}; asked for fields {self.fields} and tokenizer {asked!r}. "
                f"To change it, call drop() then create()."
            )

    # ---- the writes of the index ----

    def _drop_sqls(self) -> list[str]:
        """``drop()``: the triggers, then the index table."""
        return [*map(_drop_trigger_sql, self._trigger_names()), f"DROP TABLE IF EXISTS {self.index_table};"]

    def _clear_sql(self) -> str:
        return f"DELETE FROM {self.index_table};"

    def _forget_sql(self) -> str:
        return f"DELETE FROM {self.index_table} WHERE rowid = ?;"

    def _fill_step(
        self, columns: Any, layout: Optional[tuple[str, ...]], rows: str, params: list[Any]
    ) -> tuple[str, list[Any], Any]:
        """The index rows of the rows of the table that ``rows`` (a ``WHERE`` term on ``t``;
        empty: every row) picks, each field read where the database object remembers it is
        stored (``columns``), with the guard of what it remembers (``layout``)."""
        table = self.table
        fill = _fill_sql(self.index_table, self.fields, columns, "t")
        terms = [term for term in (rows, layout and _guard(table, layout)) if term]
        where = f" WHERE {' AND '.join(terms)}" if terms else ""
        return f"{fill} FROM {table} AS t{where};", params, layout

    def _optimize_sql(self) -> str:
        return f"INSERT INTO {self.index_table}({self.index_table}) VALUES('optimize');"

    # ---- the reads of the index ----

    def _count_all_sql(self) -> str:
        return f"SELECT COUNT(*) FROM {self.index_table};"

    def _length_sql(self) -> str:
        """``stats()``: the length of the first field, from which ``total_tokens`` is guessed."""
        return f"SELECT SUM(LENGTH(COALESCE({self.fields[0]}, ''))) FROM {self.index_table};"

    def _stats(self, row_count: int, length: Optional[int]) -> FTSStats:
        return FTSStats(
            table_name=self.index_table,
            indexed_rows=row_count,
            total_tokens=length // 5 if length else 0,  # Rough estimate
            fields=self.fields,
        )

    def _search_sql(self) -> str:
        idx = self.index_table
        return f"SELECT rowid FROM {idx} WHERE {idx} MATCH ? LIMIT ? OFFSET ?;"

    def _ranked_sql(self) -> str:
        idx = self.index_table
        return (
            f"SELECT rowid, bm25({idx}) as score FROM {idx} WHERE {idx} MATCH ? "
            "ORDER BY score LIMIT ? OFFSET ?;"
        )

    def _highlight_sql(self, highlight_start: str, highlight_end: str) -> tuple[str, list[Any]]:
        """The statement of ``search_with_highlights()`` and its parameters before the query:
        one ``highlight()`` per field, its tags as parameters."""
        idx = self.index_table
        exprs = [f"highlight({idx}, {i}, ?, ?) as {field}_hl" for i, field in enumerate(self.fields)]
        sql = (
            f"SELECT rowid, bm25({idx}) as score, {', '.join(exprs)} FROM {idx} "
            f"WHERE {idx} MATCH ? ORDER BY score LIMIT ? OFFSET ?;"
        )
        return sql, [highlight_start, highlight_end] * len(self.fields)

    def _snippet_sql(self, field: Optional[str], max_tokens: int) -> str:
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
            raise TypeError(f"max_tokens must be an integer, got {type(max_tokens).__name__}")
        field_idx = 0 if field is None else self.fields.index(field)
        idx = self.index_table
        return (
            f"SELECT snippet({idx}, {field_idx}, ?, ?, '...', {max_tokens}) FROM {idx} "
            f"WHERE rowid = ? AND {idx} MATCH ?;"
        )

    def _count_sql(self) -> str:
        return f"SELECT COUNT(*) FROM {self.index_table} WHERE {self.index_table} MATCH ?;"

    @staticmethod
    def _match_params(query: str, plain: bool, before: Any, after: Any) -> list[Any]:
        """The parameters of a ``MATCH ?`` statement: ``before``, the query as given (or with
        ``plain`` as its words, ``_plain_query``), ``after``."""
        return [*before, _plain_query(query) if plain else query, *after]

    @staticmethod
    def _ranked(rows: list, models: list) -> list[SearchResult]:
        """The hits of ``search_ranked()`` in the index's order: a row whose model loaded."""
        loaded = {model._id: model for model in models}
        return [SearchResult(model=loaded[_id], score=score) for _id, score in rows if _id in loaded]

    def _highlights(self, rows: list) -> dict[int, dict[str, Any]]:
        """The score and the highlight of each field of each row of ``search_with_highlights()``."""
        return {
            row[0]: {"score": row[1], "highlights": {f: row[2 + i] for i, f in enumerate(self.fields)}}
            for row in rows
        }

    @staticmethod
    def _highlighted(found: dict[int, dict[str, Any]], models: list) -> list[SearchResult]:
        results = []
        for model in models:
            data = found.get(model._id, {})
            results.append(
                SearchResult(model=model, score=data.get("score", 0.0), highlights=data.get("highlights"))
            )
        results.sort(key=lambda r: r.score)
        return results


class FTSIndex(_SearchIndex):
    """Full-Text Search index for a SQLer model on a ``SQLerDB`` (an ``AsyncSQLerDB`` takes
    :class:`AsyncFTSIndex`).

    Uses SQLite FTS5 for efficient text search with:
    - Boolean queries (AND, OR, NOT)
    - Phrase search ("exact phrase")
    - Prefix search (word*)
    - Relevance ranking (BM25)
    - Snippet/highlight generation

    Usage::

        # Create index
        fts = FTSIndex(Article, fields=["title", "content"])
        fts.create()

        # Add to index (automatic on model.save() if configured)
        fts.index(article)

        # Search
        results = fts.search("python")

        # Search with ranking
        results = fts.search_ranked("python tutorial")

        # Cleanup
        fts.drop()
    """

    _refuses = AsyncSQLerDB
    _refusal = (
        "a search index works on a SQLerDB: got an AsyncSQLerDB; an async database takes "
        "AsyncFTSIndex (and AsyncSearchableMixin)"
    )

    def __init__(
        self,
        model_class: Type["SQLerModel"],
        fields: list[str],
        *,
        index_name: Optional[str] = None,
        tokenizer: str = "porter unicode61",
        content_sync: bool = True,
        db: Optional["SQLerDB"] = None,
    ):
        super().__init__(
            model_class, fields, index_name=index_name, tokenizer=tokenizer, content_sync=content_sync, db=db
        )

    def create(self, db: Optional["SQLerDB"] = None) -> None:
        """Make the index what this definition says, or refuse.

        - No index table: it is created. With ``content_sync`` the model's table is
          created first when it is missing (a trigger needs its table), then the three
          triggers, and the index is filled from the rows already in the table.
        - An index table with the same fields in the same order and the same tokenizer
          (compared as definitions, not as statement text, so an index made by an older
          sqler counts) is kept.
        - An index table with other fields, another order or another tokenizer: raises
          ``SchemaError`` and changes nothing; ``drop()`` then ``create()`` changes it.
        - With ``content_sync``, a trigger that is missing or is not the one this
          definition writes now (a field promoted to a column since, an older sqler) is
          replaced, and the index is filled again from the table.
        - Without ``content_sync``, triggers of this index are dropped.

        All of it is one transaction: a ``create()`` that fails leaves everything as it
        was. A ``create()`` that finds everything as defined writes nothing.

        Args:
            db: Optional database (uses model's db if not provided)
        """
        if db:
            self._db = db
        db = self._get_db()

        if self._make(db, write=False):
            with db.transaction():
                self._make(db, write=True)

    def _make(self, db: "SQLerDB", *, write: bool) -> bool:
        """What ``create()`` finds and what it does: whether anything differs from the
        definition, and with ``write`` the statements that make it so (the caller holds
        the transaction). A definition the index table does not have raises SchemaError,
        before any statement.
        """
        stored = self._found(db.adapter.execute(*self._lookup()).fetchall())
        changed = self.index_table.lower() not in stored
        if changed and write:
            db.adapter.execute(self._create_sql())

        if not self.content_sync:
            stale = self._stale_triggers(stored)
            if write:
                for name in stale:
                    db.adapter.execute(_drop_trigger_sql(name))
            return changed or bool(stale)

        table, promoted = self.table, self._promoted()
        if table.lower() not in stored or (promoted and _lacks_columns(promoted, db._columns_of(table))):
            # a trigger needs its table, and reads a promoted field from its column
            if not write:
                return True
            db.ensure_table(table, **self._table_args(promoted))
        differing = self._differing(stored, self._trigger_sql(db))
        if write:
            for name, sql in differing:
                db.adapter.execute(_drop_trigger_sql(name))
                db.adapter.execute(sql)
        changed = changed or bool(differing)
        if changed and write:
            self._fill(db)
        return changed

    def _trigger_sql(self, db: "SQLerDB") -> dict[str, str]:
        """The statement of each trigger of this content_sync index (see :func:`_trigger_sql`),
        built from the table as it is: the database sends the check of the names it remembers
        (``_check_sql``) first, in the caller's transaction, and learns the table again if
        SQLite refuses it."""
        table = self.table
        checked = db._sent(table, lambda: _check_step(table, db._memory_of(table)[1]))
        if checked is not None:
            checked.close()
        return self._triggers(db._columns_of(table))

    def drop(self, db: Optional["SQLerDB"] = None) -> None:
        """Drop the FTS index and its triggers."""
        if db:
            self._db = db
        db = self._get_db()

        with db.transaction():
            for sql in self._drop_sqls():
                db.adapter.execute(sql)

    def _filled(self, db: "SQLerDB", rows: str, params: list[Any]) -> None:
        """Index the rows of the table that ``rows`` (a ``WHERE`` term on ``t``; empty: every
        row) picks (:meth:`_fill_step`), with the guard of what the database remembers
        (``SQLerDB._sent``)."""
        table = self.table
        db._sent(table, lambda: self._fill_step(*db._memory_of(table), rows, params))

    def _fill(self, db: "SQLerDB") -> None:
        """Make the index hold every row of the table and nothing else."""
        db.adapter.execute(self._clear_sql())
        self._filled(db, "", [])

    def _refill(self, _id: int | None, db: Optional["SQLerDB"] = None) -> None:
        """Make the index row of ``_id`` what its stored row says: none when it has no row.
        ``db`` is the database to do it in, else the index's."""
        db = db if db is not None else self._get_db()
        db.adapter.execute(self._forget_sql(), [_id])
        self._filled(db, "t._id = ?", [_id])
        db.adapter.auto_commit()

    def rebuild(self, db: Optional["SQLerDB"] = None) -> int:
        """Rebuild the entire index from source data.

        Returns:
            Number of documents indexed
        """
        if db:
            self._db = db
        db = self._get_db()

        if db.adapter.execute(_HAS_TABLE_SQL, [self.table]).fetchone() is None:
            # Source table doesn't exist yet, nothing to index
            return 0

        self._fill(db)
        db.adapter.auto_commit()
        return db.adapter.execute(self._count_all_sql()).fetchone()[0]

    def index(self, model: "SQLerModel") -> None:
        """Make the index entry of a saved row what the table holds for it.

        The row is read from the table, as the triggers and ``rebuild()`` read it. With
        ``content_sync`` the triggers already did this: calling it changes nothing.

        Args:
            model: Model instance to index
        """
        if model._id is None:
            raise ValueError("Cannot index unsaved model")
        self._refill(model._id)

    def remove(self, model_or_id: Union["SQLerModel", int]) -> None:
        """Remove a document from the index.

        With ``content_sync`` the triggers own the index: the entry goes only when its row
        is gone from the table, so the index stays equal to the table.

        Args:
            model_or_id: Model instance or _id to remove
        """
        _id = model_or_id._id if hasattr(model_or_id, "_id") else model_or_id
        if self.content_sync:
            self._refill(_id)
            return

        self._delete_entry(self._get_db(), _id)

    def _delete_entry(self, db: "SQLerDB", _id: int | None) -> None:
        db.adapter.execute(self._forget_sql(), [_id])
        db.adapter.auto_commit()

    def _follow(self, db: "SQLerDB", table: str, _id: int | None, *, deleted: bool) -> None:
        """Keep the entry of ``_id`` after its row was written to, or deleted from, ``table``
        of ``db``, in that database (so in the caller's transaction on it). A row of another
        table, or a database that has no table of this index, has no entry to keep."""
        if table != self.table:
            return
        if db is not self._get_db() and db.adapter.execute(
            _HAS_INDEX_TABLE_SQL, [self.index_table]
        ).fetchone() is None:
            return
        if deleted:
            self._delete_entry(db, _id)
        else:
            self._refill(_id, db)

    def search(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        plain: bool = False,
    ) -> list["SQLerModel"]:
        """Search the index and return matching models.

        Query syntax:
        - Simple: "python"
        - AND (default): "python tutorial"
        - OR: "python OR rust"
        - NOT: "python NOT java"
        - Phrase: '"machine learning"'
        - Prefix: "pyth*"
        - Column: "title:python"

        Args:
            query: FTS5 query string, or with ``plain`` text to find
            limit: Maximum results
            offset: Skip first N results
            plain: Read ``query`` as words, not as a query: a row matches when it has
                every word, a word being what the index's tokenizer makes of the text
                between spaces (``C++``, ``it's``, ``foo-bar`` work; no character is
                syntax). Text with no word in it matches nothing.

        Returns:
            List of matching model instances

        Raises:
            SearchQueryError: SQLite refuses ``query`` as an FTS5 query (never with
                ``plain``).

        Security note:
            The query string is passed as a SQL bound parameter, so it is
            safe from SQL injection. However, FTS5 query syntax allows
            wildcard prefix searches (``pyth*``) and very-broad disjunctions
            that can be expensive on large indexes — a DoS risk if the
            string comes directly from user input. If you expose this to
            untrusted callers, validate or truncate the query string and
            consider rate-limiting search endpoints in your application.
        """
        rows = self._search_rows(self._search_sql(), query, plain, after=(limit, offset))
        return self._models([row[0] for row in rows])

    def _models(self, ids: list[int]) -> list["SQLerModel"]:
        """The rows of ``ids`` as the class loads them by id, which is how every search gives
        its hits: the database given to this index reads them (a class never bound has no
        other); a missing source table and an id with no row give none."""
        if not ids:
            return []
        if self._db is not None:
            return self.model_class.from_ids(ids, db=self._db)
        return self.model_class.from_ids(ids)

    def search_ranked(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        plain: bool = False,
    ) -> list[SearchResult]:
        """Search with BM25 ranking scores.

        The index gives the ids and their scores; the rows are loaded as ``search()``
        loads them, so a hit is the model ``from_ids`` gives for its row.

        Args:
            query: FTS5 query string, or with ``plain`` text to find (see ``search()``)
            limit: Maximum results
            offset: Skip first N results
            plain: Read ``query`` as words, not as a query

        Returns:
            List of SearchResult with scores (sorted by relevance)
        """
        rows = self._search_rows(self._ranked_sql(), query, plain, after=(limit, offset))
        return self._ranked(rows, self._models([row[0] for row in rows]))

    def search_with_highlights(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        highlight_start: str = "<mark>",
        highlight_end: str = "</mark>",
        plain: bool = False,
    ) -> list[SearchResult]:
        """Search with highlighted snippets.

        Args:
            query: FTS5 query string, or with ``plain`` text to find (see ``search()``)
            limit: Maximum results
            offset: Skip first N results
            highlight_start: Opening tag for highlights
            highlight_end: Closing tag for highlights
            plain: Read ``query`` as words, not as a query

        Returns:
            List of SearchResult with highlights dict
        """
        sql, tags = self._highlight_sql(highlight_start, highlight_end)
        rows = self._search_rows(sql, query, plain, before=tags, after=(limit, offset))
        if not rows:
            return []
        found = self._highlights(rows)
        return self._highlighted(found, self._models(list(found)))

    def snippet(
        self,
        query: str,
        model: "SQLerModel",
        *,
        field: Optional[str] = None,
        max_tokens: int = 64,
        highlight_start: str = "<b>",
        highlight_end: str = "</b>",
        plain: bool = False,
    ) -> str:
        """Get a highlighted snippet for a specific document.

        Args:
            query: Search query
            model: Model to get snippet for
            field: Specific field (default: first field)
            max_tokens: Maximum tokens in snippet
            highlight_start: Opening highlight tag
            highlight_end: Closing highlight tag
            plain: Read ``query`` as words, not as a query (see ``search()``)

        Returns:
            Highlighted snippet string
        """
        sql = self._snippet_sql(field, max_tokens)
        rows = self._search_rows(sql, query, plain, before=(highlight_start, highlight_end, model._id))
        return rows[0][0] if rows else ""

    def count(self, query: str, *, plain: bool = False) -> int:
        """Count matching documents.

        Args:
            query: FTS5 query string, or with ``plain`` text to find (see ``search()``)
            plain: Read ``query`` as words, not as a query

        Returns:
            Number of matching documents
        """
        return self._search_rows(self._count_sql(), query, plain)[0][0]

    def _search_rows(self, sql: str, query: str, plain: bool, before: Any = (), after: Any = ()) -> list:
        """The rows of a ``MATCH ?`` statement, its parameters :meth:`_match_params`. A query
        SQLite refuses raises SearchQueryError; any other error is raised as it is."""
        params = self._match_params(query, plain, before, after)
        try:
            return self._get_db().adapter.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            refused = _search_error(query, exc, sql, params)
            if refused is None:
                raise
            raise refused from exc

    def stats(self) -> FTSStats:
        """Get index statistics."""
        db = self._get_db()
        row_count = db.adapter.execute(self._count_all_sql()).fetchone()[0]
        length = None
        try:
            length = db.adapter.execute(self._length_sql()).fetchone()[0]
        except Exception:
            pass
        return self._stats(row_count, length)

    def optimize(self, db: Optional["SQLerDB"] = None) -> None:
        """Optimize the FTS5 index by merging segments.

        Runs FTS5's ``optimize`` command which merges all index segments
        into a single optimized segment.  Unlike :meth:`rebuild`, this
        does **not** re-read source data — it only restructures the
        internal b-tree for faster queries.
        """
        if db:
            self._db = db
        db = self._get_db()
        db.adapter.execute(self._optimize_sql())
        db.adapter.auto_commit()


async def _sent_now(adapter: Any, sql: str, params: Optional[list[Any]] = None) -> None:
    """Send ``sql`` through the async ``adapter`` and close its cursor."""
    cur = await adapter.execute(sql, params)
    await cur.close()


class AsyncFTSIndex(_SearchIndex):
    """Async twin of :class:`FTSIndex`, for an ``AsyncSQLerDB``: every public call is a
    coroutine of the same name, arguments and result, and sends the same statements."""

    _refuses = SQLerDB
    _refusal = (
        "an AsyncFTSIndex works on an AsyncSQLerDB: got a SQLerDB; a sync database takes "
        "FTSIndex (and SearchableMixin)"
    )

    def __init__(
        self,
        model_class: Type[Any],
        fields: list[str],
        *,
        index_name: Optional[str] = None,
        tokenizer: str = "porter unicode61",
        content_sync: bool = True,
        db: Optional[AsyncSQLerDB] = None,
    ):
        super().__init__(
            model_class, fields, index_name=index_name, tokenizer=tokenizer, content_sync=content_sync, db=db
        )

    async def create(self, db: Optional[AsyncSQLerDB] = None) -> None:
        """Async twin of :meth:`FTSIndex.create`."""
        if db:
            self._db = db
        db = self._get_db()

        if await self._make(db, write=False):
            async with db.transaction():
                await self._make(db, write=True)
        else:
            await db.adapter.auto_commit()  # what it read holds no connection

    async def _make(self, db: AsyncSQLerDB, *, write: bool) -> bool:
        """Async twin of :meth:`FTSIndex._make`."""
        adapter = db.adapter
        stored = self._found(await _afetch_all(adapter, *self._lookup()))
        changed = self.index_table.lower() not in stored
        if changed and write:
            await _sent_now(adapter, self._create_sql())

        if not self.content_sync:
            stale = self._stale_triggers(stored)
            if write:
                for name in stale:
                    await _sent_now(adapter, _drop_trigger_sql(name))
            return changed or bool(stale)

        table, promoted = self.table, self._promoted()
        if table.lower() not in stored or (promoted and _lacks_columns(promoted, await db._columns_of(table))):
            if not write:
                return True
            await db.ensure_table(table, **self._table_args(promoted))
        differing = self._differing(stored, await self._trigger_sql(db))
        if write:
            for name, sql in differing:
                await _sent_now(adapter, _drop_trigger_sql(name))
                await _sent_now(adapter, sql)
        changed = changed or bool(differing)
        if changed and write:
            await self._fill(db)
        return changed

    async def _trigger_sql(self, db: AsyncSQLerDB) -> dict[str, str]:
        """Async twin of :meth:`FTSIndex._trigger_sql`."""
        table = self.table

        async def make() -> Optional[tuple[str, list[Any], Any]]:
            return _check_step(table, (await db._memory_of(table))[1])

        checked = await db._sent(table, make)
        if checked is not None:
            await checked.close()
        return self._triggers(await db._columns_of(table))

    async def drop(self, db: Optional[AsyncSQLerDB] = None) -> None:
        """Async twin of :meth:`FTSIndex.drop`."""
        if db:
            self._db = db
        db = self._get_db()

        async with db.transaction():
            for sql in self._drop_sqls():
                await _sent_now(db.adapter, sql)

    async def _filled(self, db: AsyncSQLerDB, rows: str, params: list[Any]) -> None:
        """Async twin of :meth:`FTSIndex._filled`."""
        table = self.table

        async def make() -> tuple[str, list[Any], Any]:
            return self._fill_step(*(await db._memory_of(table)), rows, params)

        await (await db._sent(table, make)).close()

    async def _fill(self, db: AsyncSQLerDB) -> None:
        await _sent_now(db.adapter, self._clear_sql())
        await self._filled(db, "", [])

    async def _refill(self, _id: int | None, db: Optional[AsyncSQLerDB] = None) -> None:
        """Async twin of :meth:`FTSIndex._refill`."""
        db = db if db is not None else self._get_db()
        await _sent_now(db.adapter, self._forget_sql(), [_id])
        await self._filled(db, "t._id = ?", [_id])
        await db.adapter.auto_commit()

    async def rebuild(self, db: Optional[AsyncSQLerDB] = None) -> int:
        """Async twin of :meth:`FTSIndex.rebuild`."""
        if db:
            self._db = db
        db = self._get_db()
        adapter = db.adapter

        if not await _afetch_all(adapter, _HAS_TABLE_SQL, [self.table]):
            await adapter.auto_commit()
            return 0

        await self._fill(db)
        await adapter.auto_commit()
        count = (await _afetch_all(adapter, self._count_all_sql(), []))[0][0]
        await adapter.auto_commit()
        return count

    async def index(self, model: Any) -> None:
        """Async twin of :meth:`FTSIndex.index`."""
        if model._id is None:
            raise ValueError("Cannot index unsaved model")
        await self._refill(model._id)

    async def remove(self, model_or_id: Any) -> None:
        """Async twin of :meth:`FTSIndex.remove`."""
        _id = model_or_id._id if hasattr(model_or_id, "_id") else model_or_id
        if self.content_sync:
            await self._refill(_id)
            return

        await self._delete_entry(self._get_db(), _id)

    async def _delete_entry(self, db: AsyncSQLerDB, _id: int | None) -> None:
        await _sent_now(db.adapter, self._forget_sql(), [_id])
        await db.adapter.auto_commit()

    async def _follow(self, db: AsyncSQLerDB, table: str, _id: int | None, *, deleted: bool) -> None:
        """Async twin of :meth:`FTSIndex._follow`."""
        if table != self.table:
            return
        if db is not self._get_db() and not await _afetch_all(
            db.adapter, _HAS_INDEX_TABLE_SQL, [self.index_table]
        ):
            await db.adapter.auto_commit()
            return
        if deleted:
            await self._delete_entry(db, _id)
        else:
            await self._refill(_id, db)

    async def search(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        plain: bool = False,
    ) -> list:
        """Async twin of :meth:`FTSIndex.search`."""
        rows = await self._search_rows(self._search_sql(), query, plain, after=(limit, offset))
        return await self._models([row[0] for row in rows])

    async def _models(self, ids: list[int]) -> list:
        """Async twin of :meth:`FTSIndex._models`."""
        if not ids:
            return []
        if self._db is not None:
            return await self.model_class.from_ids(ids, db=self._db)
        return await self.model_class.from_ids(ids)

    async def search_ranked(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        plain: bool = False,
    ) -> list[SearchResult]:
        """Async twin of :meth:`FTSIndex.search_ranked`."""
        rows = await self._search_rows(self._ranked_sql(), query, plain, after=(limit, offset))
        return self._ranked(rows, await self._models([row[0] for row in rows]))

    async def search_with_highlights(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        highlight_start: str = "<mark>",
        highlight_end: str = "</mark>",
        plain: bool = False,
    ) -> list[SearchResult]:
        """Async twin of :meth:`FTSIndex.search_with_highlights`."""
        sql, tags = self._highlight_sql(highlight_start, highlight_end)
        rows = await self._search_rows(sql, query, plain, before=tags, after=(limit, offset))
        if not rows:
            return []
        found = self._highlights(rows)
        return self._highlighted(found, await self._models(list(found)))

    async def snippet(
        self,
        query: str,
        model: Any,
        *,
        field: Optional[str] = None,
        max_tokens: int = 64,
        highlight_start: str = "<b>",
        highlight_end: str = "</b>",
        plain: bool = False,
    ) -> str:
        """Async twin of :meth:`FTSIndex.snippet`."""
        sql = self._snippet_sql(field, max_tokens)
        rows = await self._search_rows(sql, query, plain, before=(highlight_start, highlight_end, model._id))
        return rows[0][0] if rows else ""

    async def count(self, query: str, *, plain: bool = False) -> int:
        """Async twin of :meth:`FTSIndex.count`."""
        return (await self._search_rows(self._count_sql(), query, plain))[0][0]

    async def _search_rows(self, sql: str, query: str, plain: bool, before: Any = (), after: Any = ()) -> list:
        """Async twin of :meth:`FTSIndex._search_rows`; what it read holds no connection."""
        params = self._match_params(query, plain, before, after)
        adapter = self._get_db().adapter
        try:
            rows = await _afetch_all(adapter, sql, params)
        except sqlite3.OperationalError as exc:
            refused = _search_error(query, exc, sql, params)
            if refused is None:
                raise
            raise refused from exc
        await adapter.auto_commit()
        return rows

    async def stats(self) -> FTSStats:
        """Async twin of :meth:`FTSIndex.stats`."""
        adapter = self._get_db().adapter
        row_count = (await _afetch_all(adapter, self._count_all_sql(), []))[0][0]
        length = None
        try:
            length = (await _afetch_all(adapter, self._length_sql(), []))[0][0]
        except Exception:
            pass
        await adapter.auto_commit()
        return self._stats(row_count, length)

    async def optimize(self, db: Optional[AsyncSQLerDB] = None) -> None:
        """Async twin of :meth:`FTSIndex.optimize`."""
        if db:
            self._db = db
        db = self._get_db()
        await _sent_now(db.adapter, self._optimize_sql())
        await db.adapter.auto_commit()


# ---- the mixins ----


def _refuse_kind(cls: type, mixin: str, twin: str, takes_async: bool) -> None:
    """Refuse a searchable class whose model base takes the other kind of database, naming
    the mixin it takes; a class with no model base yet is let through."""
    takes = getattr(cls, "_takes_db", None)
    if isinstance(takes, type) and issubclass(takes, AsyncSQLerDB) is not takes_async:
        kind = "a sync" if takes_async else "an async"
        raise TypeError(f"{cls.__name__} is {kind} model: use {twin} in place of {mixin}")


def _new_index(cls: type, kind: type, db: Any = None) -> Any:
    """An index of the class's ``FTS`` configuration (:func:`_fts_config`)."""
    fields, tokenizer = _fts_config(cls)
    return kind(cls, fields, tokenizer=tokenizer, db=db)


def _index_on(cls: Any, kind: type, db: Any) -> Any:
    """The index a search call given ``db`` works on: one of the class's index's definition,
    else of its ``FTS`` configuration, on ``db`` alone (see ``SearchableMixin._search_index``)."""
    held = cls._fts_index
    if held is None:
        return _new_index(cls, kind, db)
    return kind(
        cls,
        held.fields,
        index_name=held._index_table,
        tokenizer=held.tokenizer,
        content_sync=held.content_sync,
        db=db,
    )


class SearchableMixin(_SearchHost):
    """Mixin to add search capabilities to a sync model, on every sync base (an async model
    takes :class:`AsyncSearchableMixin`).

    Usage::

        class Article(SearchableMixin, SQLerModel):
            title: str
            content: str

            class FTS:
                fields = ["title", "content"]
                tokenizer = "porter unicode61"

        # Create index
        Article.create_search_index()

        # Search
        results = Article.search("python tutorial")
        results = Article.search_ranked("machine learning")

        # Auto-index on save
        article = Article(title="Hello", content="World")
        article.save()  # Automatically indexed

    Without an ``FTS`` class every public field whose declared type holds text is indexed
    (``str``, ``Optional[str]``, ``str | None``, and a container such as ``list[str]`` as its
    JSON text).
    """

    _fts_index: Optional[FTSIndex] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        _refuse_kind(cls, "SearchableMixin", "AsyncSearchableMixin", takes_async=False)

    @classmethod
    def _get_fts_config(cls) -> Tuple[list[str], str]:
        """Get FTS configuration from class."""
        return _fts_config(cls)

    @classmethod
    def create_search_index(cls, db: Optional["SQLerDB"] = None) -> FTSIndex:
        """Create the FTS index for this model."""
        index = cls._fts_index = _new_index(cls, FTSIndex)
        index.create(db)
        return index

    @classmethod
    def _search_index(cls, db: Optional["SQLerDB"]) -> FTSIndex:
        """The index a search call of this class works on.

        Without ``db``: the class's index (``_fts_index``), created by
        ``create_search_index()`` on the class's database when there is none yet. With
        ``db``: an index of the same definition (the class's index's, else the ``FTS``
        configuration) on ``db`` alone, never created: on a ``db`` without that index a
        search raises what a search of a missing index raises. The class's index is
        neither used nor changed, so an index made on one database is not searched for
        another.
        """
        if db is None:
            index = cls._fts_index
            return index if index is not None else cls.create_search_index()
        return _index_on(cls, FTSIndex, db)

    @classmethod
    def rebuild_search_index(cls, *, db: Optional["SQLerDB"] = None) -> int:
        """Rebuild the search index from all data (of ``db`` when given, else of the
        class's database)."""
        return cls._search_index(db).rebuild()

    @classmethod
    def drop_search_index(cls, *, db: Optional["SQLerDB"] = None) -> None:
        """Drop the search index (of ``db`` when given, else the class's index).

        With ``db``, the class's index is forgotten too when it is the index of ``db``.
        """
        if db is None:
            if cls._fts_index:
                cls._fts_index.drop()
                cls._fts_index = None
            return
        cls._search_index(db).drop()
        held = cls._fts_index
        if held is not None and held._held_db() is db:
            cls._fts_index = None

    @classmethod
    def search(
        cls,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        plain: bool = False,
        db: Optional["SQLerDB"] = None,
    ) -> list:
        """Search for matching documents (``plain``: see ``FTSIndex.search()``), in ``db``
        when given, else in the class's database."""
        return cls._search_index(db).search(query, limit=limit, offset=offset, plain=plain)

    @classmethod
    def search_ranked(
        cls,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        plain: bool = False,
        db: Optional["SQLerDB"] = None,
    ) -> list[SearchResult]:
        """Search with relevance ranking (``plain``: see ``FTSIndex.search()``), in ``db``
        when given, else in the class's database."""
        return cls._search_index(db).search_ranked(query, limit=limit, offset=offset, plain=plain)

    @classmethod
    def search_count(
        cls, query: str, *, plain: bool = False, db: Optional["SQLerDB"] = None
    ) -> int:
        """Count matching documents (``plain``: see ``FTSIndex.search()``), in ``db`` when
        given, else in the class's database."""
        return cls._search_index(db).count(query, plain=plain)

    def save(self, *args, **kwargs):
        """Save and update search index (with ``content_sync`` its triggers do)."""
        result = super().save(*args, **kwargs)
        index = self._fts_index
        if index is not None and not index.content_sync:
            # a saved instance belongs to the database and table it was written to
            index._follow(*_instance_binding(self), self._id, deleted=False)
        return result

    def delete(self, *args, **kwargs):
        """Delete and remove from search index (with ``content_sync`` its triggers do)."""
        _id = self._id
        index = self._fts_index
        manual = index if index is not None and _id is not None and not index.content_sync else None
        if manual is not None:
            db, table = _instance_binding(self, kwargs.get("db"))
        result = super().delete(*args, **kwargs)
        if manual is not None:
            manual._follow(db, table, _id, deleted=True)
        return result


class AsyncSearchableMixin(_AsyncSearchHost):
    """Async twin of :class:`SearchableMixin`, for every async base: every search call is a
    coroutine of the same name, arguments and result, on an ``AsyncSQLerDB``
    (:class:`AsyncFTSIndex`).

    Usage::

        class Article(AsyncSearchableMixin, AsyncSQLerModel):
            title: str

        await Article.create_search_index(db)
        await Article(title="Hello").save(db=db)
        hits = await Article.search("hello", db=db)
    """

    _fts_index: Optional[AsyncFTSIndex] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        _refuse_kind(cls, "AsyncSearchableMixin", "SearchableMixin", takes_async=True)

    @classmethod
    def _get_fts_config(cls) -> Tuple[list[str], str]:
        """Get FTS configuration from class."""
        return _fts_config(cls)

    @classmethod
    async def create_search_index(cls, db: Optional[AsyncSQLerDB] = None) -> AsyncFTSIndex:
        """Async twin of :meth:`SearchableMixin.create_search_index`."""
        index = cls._fts_index = _new_index(cls, AsyncFTSIndex)
        await index.create(db)
        return index

    @classmethod
    async def _search_index(cls, db: Optional[AsyncSQLerDB]) -> AsyncFTSIndex:
        """Async twin of :meth:`SearchableMixin._search_index`."""
        if db is None:
            index = cls._fts_index
            return index if index is not None else await cls.create_search_index()
        return _index_on(cls, AsyncFTSIndex, db)

    @classmethod
    async def rebuild_search_index(cls, *, db: Optional[AsyncSQLerDB] = None) -> int:
        """Async twin of :meth:`SearchableMixin.rebuild_search_index`."""
        return await (await cls._search_index(db)).rebuild()

    @classmethod
    async def drop_search_index(cls, *, db: Optional[AsyncSQLerDB] = None) -> None:
        """Async twin of :meth:`SearchableMixin.drop_search_index`."""
        if db is None:
            if cls._fts_index:
                await cls._fts_index.drop()
                cls._fts_index = None
            return
        await (await cls._search_index(db)).drop()
        held = cls._fts_index
        if held is not None and held._held_db() is db:
            cls._fts_index = None

    @classmethod
    async def search(
        cls,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        plain: bool = False,
        db: Optional[AsyncSQLerDB] = None,
    ) -> list:
        """Async twin of :meth:`SearchableMixin.search`."""
        return await (await cls._search_index(db)).search(query, limit=limit, offset=offset, plain=plain)

    @classmethod
    async def search_ranked(
        cls,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        plain: bool = False,
        db: Optional[AsyncSQLerDB] = None,
    ) -> list[SearchResult]:
        """Async twin of :meth:`SearchableMixin.search_ranked`."""
        index = await cls._search_index(db)
        return await index.search_ranked(query, limit=limit, offset=offset, plain=plain)

    @classmethod
    async def search_count(
        cls, query: str, *, plain: bool = False, db: Optional[AsyncSQLerDB] = None
    ) -> int:
        """Async twin of :meth:`SearchableMixin.search_count`."""
        return await (await cls._search_index(db)).count(query, plain=plain)

    async def save(self, *args, **kwargs):
        """Save and update search index (with ``content_sync`` its triggers do), once the
        save has run."""
        result = await super().save(*args, **kwargs)
        index = self._fts_index
        if index is not None and not index.content_sync:
            await index._follow(*_instance_binding(self), self._id, deleted=False)
        return result

    async def delete(self, *args, **kwargs):
        """Delete and remove from search index (with ``content_sync`` its triggers do), once
        the delete has run."""
        _id = self._id
        index = self._fts_index
        manual = index if index is not None and _id is not None and not index.content_sync else None
        if manual is not None:
            db, table = _instance_binding(self, kwargs.get("db"))
        result = await super().delete(*args, **kwargs)
        if manual is not None:
            await manual._follow(db, table, _id, deleted=True)
        return result
