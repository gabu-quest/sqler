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
    fts.create()  # Creates FTS5 virtual table
    fts.rebuild()  # Populates index from existing data

    # Search
    results = fts.search("python tutorial")
    results = fts.search("python OR rust", limit=10)
    results = fts.search('"exact phrase"')

    # Ranked results
    results = fts.search_ranked("machine learning", limit=20)

    # Highlights
    results = fts.search_with_highlights("python", highlight_tags=("<b>", "</b>"))
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Tuple, Type, Union

if TYPE_CHECKING:
    from sqler import SQLerDB
    from sqler.models import SQLerModel


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
    ):
        """Create an FTS index configuration.

        Args:
            model_class: Model class to index
            fields: Fields to include in the index
            index_name: Custom index table name (default: {table}_fts)
            tokenizer: FTS5 tokenizer (default: porter unicode61)
            content_sync: Keep index synced with content table
        """
        self.model_class = model_class
        self.fields = fields
        self.tokenizer = tokenizer
        self.content_sync = content_sync

        # Get table name - _table is set when set_db() is called
        self.index_table = index_name  # Set later if needed

        # Get database
        self._db: Optional["SQLerDB"] = None

    @property
    def table(self) -> str:
        """Get the table name from the model class."""
        # Try _table first (set by set_db)
        table = getattr(self.model_class, "_table", None)
        if table:
            return table
        # Fallback to lowercase class name
        return self.model_class.__name__.lower()

    @property
    def index_table(self) -> str:
        """Get the FTS index table name."""
        if self._index_table:
            return self._index_table
        return f"{self.table}_fts"

    @index_table.setter
    def index_table(self, value: Optional[str]) -> None:
        """Set the index table name."""
        self._index_table = value

    def _get_db(self) -> "SQLerDB":
        """Get the database connection."""
        if self._db is not None:
            return self._db
        db = getattr(self.model_class, "_db", None)
        if db is None:
            raise ValueError("Model has no database bound")
        return db

    def create(self, db: Optional["SQLerDB"] = None) -> None:
        """Create the FTS5 virtual table.

        Args:
            db: Optional database (uses model's db if not provided)
        """
        if db:
            self._db = db

        db = self._get_db()

        # Build column list
        columns = ", ".join(self.fields)

        # Use standalone FTS table (not external content)
        # This is more compatible with SQLer's JSON document storage
        sql = f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {self.index_table}
        USING fts5(
            {columns},
            tokenize='{self.tokenizer}'
        );
        """

        db.adapter.execute(sql)
        db.adapter.auto_commit()

    def drop(self, db: Optional["SQLerDB"] = None) -> None:
        """Drop the FTS index."""
        if db:
            self._db = db
        db = self._get_db()

        sql = f"DROP TABLE IF EXISTS {self.index_table};"
        db.adapter.execute(sql)
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

        # Clear existing index
        db.adapter.execute(f"DELETE FROM {self.index_table};")

        # Insert from source table using JSON extraction
        field_exprs = ", ".join([f"json_extract(data, '$.{f}')" for f in self.fields])

        sql = f"""
        INSERT INTO {self.index_table}(rowid, {", ".join(self.fields)})
        SELECT _id, {field_exprs}
        FROM {self.table};
        """
        cursor = db.adapter.execute(sql)
        db.adapter.auto_commit()

        # Get count
        cursor = db.adapter.execute(f"SELECT COUNT(*) FROM {self.index_table};")
        return cursor.fetchone()[0]

    def index(self, model: "SQLerModel") -> None:
        """Add or update a document in the index.

        Args:
            model: Model instance to index
        """
        if model._id is None:
            raise ValueError("Cannot index unsaved model")

        db = self._get_db()

        # Delete existing entry if present
        db.adapter.execute(
            f"DELETE FROM {self.index_table} WHERE rowid = ?;",
            [model._id],
        )

        # Insert new entry
        field_vals = [str(getattr(model, f, "") or "") for f in self.fields]
        placeholders = ", ".join(["?"] * len(self.fields))
        db.adapter.execute(
            f"INSERT INTO {self.index_table}(rowid, {', '.join(self.fields)}) VALUES (?, {placeholders});",
            [model._id] + field_vals,
        )

        db.adapter.auto_commit()

    def remove(self, model_or_id: Union["SQLerModel", int]) -> None:
        """Remove a document from the index.

        Args:
            model_or_id: Model instance or _id to remove
        """
        db = self._get_db()
        _id = model_or_id._id if hasattr(model_or_id, "_id") else model_or_id

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
            query: FTS5 query string
            limit: Maximum results
            offset: Skip first N results

        Returns:
            List of matching model instances
        """
        db = self._get_db()

        sql = f"""
        SELECT rowid FROM {self.index_table}
        WHERE {self.index_table} MATCH ?
        LIMIT ? OFFSET ?;
        """

        cursor = db.adapter.execute(sql, [query, limit, offset])
        rows = cursor.fetchall()

        # Load models
        ids = [row[0] for row in rows]
        if not ids:
            return []

        return self.model_class.from_ids(ids)

    def search_ranked(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SearchResult]:
        """Search with BM25 ranking scores.

        Args:
            query: FTS5 query string
            limit: Maximum results
            offset: Skip first N results

        Returns:
            List of SearchResult with scores
        """
        db = self._get_db()

        sql = f"""
        SELECT rowid, bm25({self.index_table}) as score
        FROM {self.index_table}
        WHERE {self.index_table} MATCH ?
        ORDER BY score
        LIMIT ? OFFSET ?;
        """

        cursor = db.adapter.execute(sql, [query, limit, offset])
        rows = cursor.fetchall()

        if not rows:
            return []

        # Load models
        id_to_score = {row[0]: row[1] for row in rows}
        ids = list(id_to_score.keys())
        models = self.model_class.from_ids(ids)

        # Create results
        results = []
        for model in models:
            results.append(
                SearchResult(
                    model=model,
                    score=id_to_score.get(model._id, 0.0),
                )
            )

        # Sort by score (lower is better for BM25)
        results.sort(key=lambda r: r.score)
        return results

    def search_with_highlights(
        self,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        highlight_start: str = "<mark>",
        highlight_end: str = "</mark>",
    ) -> list[SearchResult]:
        """Search with highlighted snippets.

        Args:
            query: FTS5 query string
            limit: Maximum results
            offset: Skip first N results
            highlight_start: Opening tag for highlights
            highlight_end: Closing tag for highlights

        Returns:
            List of SearchResult with highlights dict
        """
        db = self._get_db()

        # Build highlight expressions for each field
        highlight_exprs = []
        for i, field in enumerate(self.fields):
            highlight_exprs.append(
                f"highlight({self.index_table}, {i}, '{highlight_start}', '{highlight_end}') as {field}_hl"
            )

        sql = f"""
        SELECT rowid, bm25({self.index_table}) as score, {", ".join(highlight_exprs)}
        FROM {self.index_table}
        WHERE {self.index_table} MATCH ?
        ORDER BY score
        LIMIT ? OFFSET ?;
        """

        cursor = db.adapter.execute(sql, [query, limit, offset])
        rows = cursor.fetchall()

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
    ) -> str:
        """Get a highlighted snippet for a specific document.

        Args:
            query: Search query
            model: Model to get snippet for
            field: Specific field (default: first field)
            max_tokens: Maximum tokens in snippet
            highlight_start: Opening highlight tag
            highlight_end: Closing highlight tag

        Returns:
            Highlighted snippet string
        """
        db = self._get_db()
        field_idx = 0 if field is None else self.fields.index(field)

        sql = f"""
        SELECT snippet({self.index_table}, {field_idx}, '{highlight_start}', '{highlight_end}', '...', {max_tokens})
        FROM {self.index_table}
        WHERE rowid = ? AND {self.index_table} MATCH ?;
        """

        cursor = db.adapter.execute(sql, [model._id, query])
        row = cursor.fetchone()
        return row[0] if row else ""

    def count(self, query: str) -> int:
        """Count matching documents.

        Args:
            query: FTS5 query string

        Returns:
            Number of matching documents
        """
        db = self._get_db()
        sql = f"SELECT COUNT(*) FROM {self.index_table} WHERE {self.index_table} MATCH ?;"
        cursor = db.adapter.execute(sql, [query])
        return cursor.fetchone()[0]

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

    def optimize(self) -> None:
        """Optimize the FTS index (merge segments)."""
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
    def search(cls, query: str, *, limit: int = 100, offset: int = 0) -> list:
        """Search for matching documents."""
        if cls._fts_index is None:
            cls.create_search_index()
        return cls._fts_index.search(query, limit=limit, offset=offset)

    @classmethod
    def search_ranked(cls, query: str, *, limit: int = 100, offset: int = 0) -> list[SearchResult]:
        """Search with relevance ranking."""
        if cls._fts_index is None:
            cls.create_search_index()
        return cls._fts_index.search_ranked(query, limit=limit, offset=offset)

    @classmethod
    def search_count(cls, query: str) -> int:
        """Count matching documents."""
        if cls._fts_index is None:
            cls.create_search_index()
        return cls._fts_index.count(query)

    def save(self, *args, **kwargs):
        """Save and update search index."""
        result = super().save(*args, **kwargs)
        if self._fts_index is not None:
            self._fts_index.index(self)
        return result

    async def asave(self, *args, **kwargs):
        """Async save and update search index."""
        result = await super().asave(*args, **kwargs)
        if self._fts_index is not None:
            self._fts_index.index(self)  # Note: sync indexing
        return result

    def delete(self, *args, **kwargs):
        """Delete and remove from search index."""
        _id = self._id
        result = super().delete(*args, **kwargs)
        if self._fts_index is not None and _id is not None:
            self._fts_index.remove(_id)
        return result

    async def adelete(self, *args, **kwargs):
        """Async delete and remove from search index."""
        _id = self._id
        result = await super().adelete(*args, **kwargs)
        if self._fts_index is not None and _id is not None:
            self._fts_index.remove(_id)
        return result
