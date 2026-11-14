"""Full-Text Search (FTS5) integration for SQLer.

Enables efficient text search using SQLite's FTS5 extension.

Usage::

    from sqler.fts import FTSIndex, search

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
    results = fts.search_with_highlights("python", highlight_tags=("<b>", "</b>"))
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Tuple, Type, Union

from sqler.db.async_db import AsyncSQLerDB
from sqler.exceptions import SchemaError, SearchQueryError
from sqler.utils import validate_field_name, validate_identifier

if TYPE_CHECKING:
    from sqler import SQLerDB
    from sqler.models import SQLerModel

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


class FTSIndex:
    """Full-Text Search index for a SQLer model.

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
        self._db: Optional["SQLerDB"] = db

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

    def _get_db(self) -> "SQLerDB":
        """The database given to this index, else the one the model class is bound to.

        Raises:
            ValueError: There is neither.
            TypeError: It is an ``AsyncSQLerDB``: a search index works on a ``SQLerDB``.
        """
        db = self._db if self._db is not None else getattr(self.model_class, "_db", None)
        if db is None:
            raise ValueError(
                "Model has no database bound: pass db= to FTSIndex or to create()/rebuild()"
            )
        if isinstance(db, AsyncSQLerDB):
            raise TypeError(
                "a search index works on a SQLerDB: got an AsyncSQLerDB, and the async "
                "database has no search yet"
            )
        return db

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

        # A field is a column name of the index table: a dotted (nested) field is not one.
        for f in self.fields:
            validate_identifier(f)

        if self._make(db, write=False):
            with db.transaction():
                self._make(db, write=True)

    def _trigger_names(self) -> dict[str, str]:
        """The index's triggers: name -> event."""
        return {f"{self.index_table}_{suffix}": event for suffix, event in _SYNC_TRIGGERS.items()}

    def _make(self, db: "SQLerDB", *, write: bool) -> bool:
        """What ``create()`` finds and what it does: whether anything differs from the
        definition, and with ``write`` the statements that make it so (the caller holds
        the transaction). A definition the index table does not have raises SchemaError,
        before any statement.
        """
        idx, table = self.index_table, self.table
        names = self._trigger_names()
        stored = {
            name.lower(): (kind, sql)
            for name, kind, sql in db.adapter.execute(
                "SELECT name, type, sql FROM sqlite_master WHERE lower(name) IN "
                f"({', '.join('?' * (2 + len(names)))});",
                [n.lower() for n in (idx, table, *names)],
            ).fetchall()
        }

        index = stored.get(idx.lower())
        if index is not None:
            self._check_definition(index)
        changed = index is None
        if changed and write:
            db.adapter.execute(
                f"CREATE VIRTUAL TABLE {idx} USING fts5"
                f"({', '.join(self.fields)}, tokenize='{self.tokenizer}');"
            )

        if not self.content_sync:
            for name in names:
                if stored.get(name.lower(), ("",))[0] == "trigger":
                    changed = True
                    if write:
                        db.adapter.execute(f"DROP TRIGGER IF EXISTS {name};")
            return changed

        promoted = getattr(self.model_class, "__promoted__", None) or {}
        if table.lower() not in stored or any(c not in db._columns_of(table) for c in promoted):
            # a trigger needs its table, and reads a promoted field from its column
            if not write:
                return True
            db.ensure_table(
                table, promoted=promoted or None, checks=getattr(self.model_class, "__checks__", None)
            )
        for name, sql in self._trigger_sql(db).items():
            if stored.get(name.lower()) != ("trigger", sql):
                changed = True
                if write:
                    db.adapter.execute(f"DROP TRIGGER IF EXISTS {name};")
                    db.adapter.execute(sql)
        if changed and write:
            self._fill(db)
        return changed

    def _check_definition(self, index: tuple[str, Optional[str]]) -> None:
        """Raise SchemaError unless the stored ``index`` (type, statement) is this definition."""
        kind, sql = index
        found = _stored_definition(sql) if kind == "table" else None
        if found is None:
            raise SchemaError(
                f"{self.index_table!r} is not a search index (an FTS5 table): "
                f"give FTSIndex another index_name"
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

    def _trigger_sql(self, db: "SQLerDB") -> dict[str, str]:
        """The statement of each trigger of a content_sync index, as SQLite stores it.

        Every name in it is checked: the fields, the index name and the table. The update
        trigger fires only when the row id or the stored value of an indexed field changed,
        compared so that ``1`` and ``1.0`` differ as they do in the index: the fields read
        from ``data`` as one ``json_array()``, a promoted column with ``quote()`` (a column
        may hold a blob, which JSON cannot).
        """
        idx, table = self.index_table, self.table
        promoted = db._columns_of(table)
        in_data = [f for f in self.fields if f not in promoted]
        changed = ["old._id IS NOT new._id"]
        if in_data:
            old, new = (
                "json_array(" + ", ".join(f"json_extract({row}.data, '$.{f}')" for f in in_data) + ")"
                for row in ("old", "new")
            )
            changed.append(f"{old} IS NOT {new}")
        changed += [f"quote(old.{f}) IS NOT quote(new.{f})" for f in self.fields if f in promoted]
        changed = " OR ".join(changed)
        forget = f"DELETE FROM {idx} WHERE rowid = old._id;"
        fill = f"{self._fill_sql(db, 'new')};"
        body = {
            "INSERT": f"BEGIN DELETE FROM {idx} WHERE rowid = new._id; {fill} END",
            "UPDATE": f"WHEN {changed} BEGIN {forget} {fill} END",
            "DELETE": f"BEGIN {forget} END",
        }
        return {
            name: f"CREATE TRIGGER {name} AFTER {event} ON {table} {body[event]}"
            for name, event in self._trigger_names().items()
        }

    def drop(self, db: Optional["SQLerDB"] = None) -> None:
        """Drop the FTS index and its triggers."""
        if db:
            self._db = db
        db = self._get_db()

        with db.transaction():
            for name in self._trigger_names():
                db.adapter.execute(f"DROP TRIGGER IF EXISTS {name};")
            db.adapter.execute(f"DROP TABLE IF EXISTS {self.index_table};")

    def _values(self, db: "SQLerDB", row: str) -> list[str]:
        """Each field of the stored row ``row`` where it is stored: a promoted column by its
        name, any other field from ``data`` with ``json_extract``."""
        promoted = db._columns_of(self.table)
        return [
            f"{row}.{f}" if f in promoted else f"json_extract({row}.data, '$.{f}')"
            for f in self.fields
        ]

    def _fill_sql(self, db: "SQLerDB", row: str) -> str:
        """The ``INSERT`` of the index row of the stored row ``row`` (``new`` in a trigger,
        an alias of the table in a select).

        The triggers, ``rebuild()``, ``create()`` and ``index()`` all use it, so they give
        the same text for a row.
        """
        return (
            f"INSERT INTO {self.index_table}(rowid, {', '.join(self.fields)}) "
            f"SELECT {row}._id, {', '.join(self._values(db, row))}"
        )

    def _fill(self, db: "SQLerDB") -> None:
        """Make the index hold every row of the table and nothing else."""
        db.adapter.execute(f"DELETE FROM {self.index_table};")
        db.adapter.execute(f"{self._fill_sql(db, 't')} FROM {self.table} AS t;")

    def _refill(self, _id: int) -> None:
        """Make the index row of ``_id`` what its stored row says: none when it has no row."""
        db = self._get_db()
        db.adapter.execute(f"DELETE FROM {self.index_table} WHERE rowid = ?;", [_id])
        db.adapter.execute(f"{self._fill_sql(db, 't')} FROM {self.table} AS t WHERE t._id = ?;", [_id])
        db.adapter.auto_commit()

    def rebuild(self, db: Optional["SQLerDB"] = None) -> int:
        """Rebuild the entire index from source data.

        Returns:
            Number of documents indexed
        """
        if db:
            self._db = db
        db = self._get_db()

        # Check if source table exists
        cursor = db.adapter.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?;",
            [self.table],
        )
        if cursor.fetchone() is None:
            # Source table doesn't exist yet, nothing to index
            return 0

        self._fill(db)
        db.adapter.auto_commit()

        # Get count
        cursor = db.adapter.execute(f"SELECT COUNT(*) FROM {self.index_table};")
        return cursor.fetchone()[0]

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

        db = self._get_db()
        db.adapter.execute(
            f"DELETE FROM {self.index_table} WHERE rowid = ?;",
            [_id],
        )

        db.adapter.auto_commit()

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
        sql = f"""
        SELECT rowid FROM {self.index_table}
        WHERE {self.index_table} MATCH ?
        LIMIT ? OFFSET ?;
        """
        rows = self._search_rows(sql, query, plain, after=(limit, offset))

        # Load models
        ids = [row[0] for row in rows]
        if not ids:
            return []

        # the database given to this index loads the rows (a class never bound has no
        # other); a missing source table loads none
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

        Uses a single JOIN query to fetch FTS scores and source documents
        in one round-trip, avoiding the overhead of separate FTS + lookup
        queries and the ``from_ids()`` hydration pipeline.

        Args:
            query: FTS5 query string, or with ``plain`` text to find (see ``search()``)
            limit: Maximum results
            offset: Skip first N results
            plain: Read ``query`` as words, not as a query

        Returns:
            List of SearchResult with scores (sorted by relevance)
        """
        from sqler.models._table_names import _is_missing_table

        source_table = self.table

        sql = f"""
        SELECT t._id, t.data, bm25({self.index_table}) as score
        FROM {self.index_table} f
        JOIN {source_table} t ON t._id = f.rowid
        WHERE {self.index_table} MATCH ?
        ORDER BY score
        LIMIT ? OFFSET ?;
        """

        try:
            rows = self._search_rows(sql, query, plain, after=(limit, offset))
        except sqlite3.OperationalError as exc:
            if not _is_missing_table(exc, source_table):
                raise
            return []  # the source table was never created: no rows to rank

        if not rows:
            return []

        results = []
        for row in rows:
            doc = json.loads(row[1])
            doc["_id"] = row[0]
            inst = self.model_class.model_validate(doc)
            inst._id = row[0]
            results.append(SearchResult(model=inst, score=row[2]))

        return results

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
        # Build highlight expressions for each field using ? placeholders
        highlight_exprs = []
        highlight_params: list[Any] = []
        for i, field in enumerate(self.fields):
            highlight_exprs.append(
                f"highlight({self.index_table}, {i}, ?, ?) as {field}_hl"
            )
            highlight_params.extend([highlight_start, highlight_end])

        sql = f"""
        SELECT rowid, bm25({self.index_table}) as score, {", ".join(highlight_exprs)}
        FROM {self.index_table}
        WHERE {self.index_table} MATCH ?
        ORDER BY score
        LIMIT ? OFFSET ?;
        """

        rows = self._search_rows(sql, query, plain, before=highlight_params, after=(limit, offset))

        if not rows:
            return []

        # Parse results
        id_data = {}
        for row in rows:
            row_id = row[0]
            score = row[1]
            highlights = {}
            for i, field in enumerate(self.fields):
                highlights[field] = row[2 + i]
            id_data[row_id] = {"score": score, "highlights": highlights}

        # Load models
        ids = list(id_data.keys())
        # the database given to this index loads the rows (a class never bound has no
        # other); a missing source table loads none
        if self._db is not None:
            models = self.model_class.from_ids(ids, db=self._db)
        else:
            models = self.model_class.from_ids(ids)

        # Create results
        results = []
        for model in models:
            data = id_data.get(model._id, {})
            results.append(
                SearchResult(
                    model=model,
                    score=data.get("score", 0.0),
                    highlights=data.get("highlights"),
                )
            )

        results.sort(key=lambda r: r.score)
        return results

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
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
            raise TypeError(f"max_tokens must be an integer, got {type(max_tokens).__name__}")
        field_idx = 0 if field is None else self.fields.index(field)

        sql = f"""
        SELECT snippet({self.index_table}, {field_idx}, ?, ?, '...', {max_tokens})
        FROM {self.index_table}
        WHERE rowid = ? AND {self.index_table} MATCH ?;
        """

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
        sql = f"SELECT COUNT(*) FROM {self.index_table} WHERE {self.index_table} MATCH ?;"
        return self._search_rows(sql, query, plain)[0][0]

    def _search_rows(self, sql: str, query: str, plain: bool, before=(), after=()) -> list:
        """The rows of a ``MATCH ?`` statement, its parameters ``before``, the query, ``after``.

        The query goes as given, or with ``plain`` as its words (``_plain_query``). A query
        SQLite refuses raises SearchQueryError; any other error is raised as it is.
        """
        params = [*before, _plain_query(query) if plain else query, *after]
        try:
            return self._get_db().adapter.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if not str(exc).startswith(_REFUSED_QUERY):
                raise
            raise SearchQueryError(
                f"search query {query!r} refused by SQLite: {exc}", sql=sql, params=params
            ) from exc

    def stats(self) -> FTSStats:
        """Get index statistics."""
        db = self._get_db()

        # Row count
        cursor = db.adapter.execute(f"SELECT COUNT(*) FROM {self.index_table};")
        row_count = cursor.fetchone()[0]

        # Total tokens (approximate via content length)
        total_tokens = 0
        try:
            cursor = db.adapter.execute(
                f"SELECT SUM(LENGTH(COALESCE({self.fields[0]}, ''))) FROM {self.index_table};"
            )
            result = cursor.fetchone()[0]
            total_tokens = result // 5 if result else 0  # Rough estimate
        except Exception:
            pass

        return FTSStats(
            table_name=self.index_table,
            indexed_rows=row_count,
            total_tokens=total_tokens,
            fields=self.fields,
        )

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
        sql = f"INSERT INTO {self.index_table}({self.index_table}) VALUES('optimize');"
        db.adapter.execute(sql)
        db.adapter.auto_commit()


class SearchableMixin:
    """Mixin to add search capabilities to models.

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
    """

    _fts_index: Optional[FTSIndex] = None

    @classmethod
    def _get_fts_config(cls) -> Tuple[list[str], str]:
        """Get FTS configuration from class."""
        if hasattr(cls, "FTS"):
            fields = getattr(cls.FTS, "fields", [])
            tokenizer = getattr(cls.FTS, "tokenizer", "porter unicode61")
        else:
            # Default: all string fields
            fields = []
            for name, field_info in cls.model_fields.items():
                if not name.startswith("_"):
                    # Check if it's a string field
                    annotation = field_info.annotation
                    if annotation is str or (
                        hasattr(annotation, "__origin__")
                        and str in getattr(annotation, "__args__", ())
                    ):
                        fields.append(name)
            tokenizer = "porter unicode61"
        return fields, tokenizer

    @classmethod
    def create_search_index(cls, db: Optional["SQLerDB"] = None) -> FTSIndex:
        """Create the FTS index for this model."""
        fields, tokenizer = cls._get_fts_config()
        cls._fts_index = FTSIndex(cls, fields, tokenizer=tokenizer)
        cls._fts_index.create(db)
        return cls._fts_index

    @classmethod
    def rebuild_search_index(cls) -> int:
        """Rebuild the search index from all data."""
        if cls._fts_index is None:
            cls.create_search_index()
        return cls._fts_index.rebuild()

    @classmethod
    def drop_search_index(cls) -> None:
        """Drop the search index."""
        if cls._fts_index:
            cls._fts_index.drop()
            cls._fts_index = None

    @classmethod
    def search(cls, query: str, *, limit: int = 100, offset: int = 0, plain: bool = False) -> list:
        """Search for matching documents (``plain``: see ``FTSIndex.search()``)."""
        if cls._fts_index is None:
            cls.create_search_index()
        return cls._fts_index.search(query, limit=limit, offset=offset, plain=plain)

    @classmethod
    def search_ranked(
        cls, query: str, *, limit: int = 100, offset: int = 0, plain: bool = False
    ) -> list[SearchResult]:
        """Search with relevance ranking (``plain``: see ``FTSIndex.search()``)."""
        if cls._fts_index is None:
            cls.create_search_index()
        return cls._fts_index.search_ranked(query, limit=limit, offset=offset, plain=plain)

    @classmethod
    def search_count(cls, query: str, *, plain: bool = False) -> int:
        """Count matching documents (``plain``: see ``FTSIndex.search()``)."""
        if cls._fts_index is None:
            cls.create_search_index()
        return cls._fts_index.count(query, plain=plain)

    def save(self, *args, **kwargs):
        """Save and update search index (with ``content_sync`` its triggers do)."""
        result = super().save(*args, **kwargs)
        if self._fts_index is not None and not self._fts_index.content_sync:
            self._fts_index.index(self)
        return result

    async def asave(self, *args, **kwargs):
        """Async save and update search index."""
        result = await super().asave(*args, **kwargs)
        if self._fts_index is not None:
            self._fts_index.index(self)  # Note: sync indexing
        return result

    def delete(self, *args, **kwargs):
        """Delete and remove from search index (with ``content_sync`` its triggers do)."""
        _id = self._id
        result = super().delete(*args, **kwargs)
        if self._fts_index is not None and _id is not None and not self._fts_index.content_sync:
            self._fts_index.remove(_id)
        return result

    async def adelete(self, *args, **kwargs):
        """Async delete and remove from search index."""
        _id = self._id
        result = await super().adelete(*args, **kwargs)
        if self._fts_index is not None and _id is not None:
            self._fts_index.remove(_id)
        return result
