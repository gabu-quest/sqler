# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo"]
# ///
"""SQLer Lite Tour: Full-Text Search - Works in Pyodide/WASM!"""

import marimo

__generated_with = "0.18.4"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo

    return (mo,)


@app.cell
async def _():
    import sys

    pyodide_sqlite3_ready = True
    sqler_ready = True
    if sys.platform == "emscripten":
        pyodide_sqlite3_ready = False
        sqler_ready = False
        try:
            import pyodide

            await pyodide.loadPackage("sqlite3")
            pyodide_sqlite3_ready = True
        except Exception:
            try:
                import js

                await js.pyodide.loadPackage("sqlite3")
                pyodide_sqlite3_ready = True
            except Exception as exc:
                print("Failed to load sqlite3 in Pyodide:", exc)

        import importlib.util as importlib_util

        if importlib_util.find_spec("sqler") is not None:
            sqler_ready = True
        else:
            try:
                import js
                import micropip

                wheel_name = "sqler-1.2026.1.6-py3-none-any.whl"
                wheel_url = str(
                    js.URL.new(f"../../{wheel_name}", js.self.location.href)
                )
                await micropip.install(wheel_url)
            except Exception as exc:
                print("Failed to install sqler wheel in Pyodide:", exc)
            else:
                if importlib_util.find_spec("sqler") is not None:
                    sqler_ready = True

    return (pyodide_sqlite3_ready, sqler_ready)


@app.cell
def _(mo):
    mo.md(r"""
    # SQLer Lite Tour: Full-Text Search

    Welcome to SQLer Lite's full-text search capabilities! This interactive notebook
    teaches you how to use SQLite's FTS5 extension with **dataclass-based models** that
    work in **Pyodide/WASM** environments.

    **What you'll learn:**
    1. Creating FTS5 indexes
    2. Basic text search
    3. Boolean queries (AND, OR, NOT)
    4. Phrase and prefix search
    5. Ranked results (BM25)
    6. Highlighted snippets
    7. Index statistics

    Let's explore!
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    > **Lite vs Pydantic**: This tour uses `SQLerLiteModel` (dataclasses) so it runs
    > in your browser via WebAssembly. With `SQLerModel` (Pydantic), you also get:
    > - `SearchableMixin` for adding `.search()` directly to model classes
    >
    > ```python
    > # Pydantic version — SearchableMixin
    > from sqler.fts import SearchableMixin
    >
    > class BlogPost(SearchableMixin, SQLerModel):
    >     _table = "blog_posts"
    >     title: str
    >     body: str
    >     class FTS:
    >         fields = ["title", "body"]
    >
    > BlogPost.create_search_index()
    > results = BlogPost.search("python")  # search directly on model
    > ```
    >
    > Run locally: `uv run marimo edit examples/tour_08_fulltext_search.py`
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 1. Setting Up

    First, we import SQLer and create an in-memory database. We'll also import
    the `FTSIndex` class for full-text search capabilities.
    """)
    return


@app.cell
def _(pyodide_sqlite3_ready, sqler_ready):
    if not pyodide_sqlite3_ready:
        raise RuntimeError(
            "sqlite3 is required in Pyodide; failed to load package 'sqlite3'."
        )
    if not sqler_ready:
        raise RuntimeError(
            "sqler is required in Pyodide; failed to install sqler wheel."
        )

    import importlib
    from dataclasses import dataclass

    _sqler = importlib.import_module("sqler")
    SQLerDB = _sqler.SQLerDB
    SQLerLiteModel = _sqler.SQLerLiteModel

    # Import FTS functionality
    _sqler_fts = importlib.import_module("sqler.fts")
    FTSIndex = _sqler_fts.FTSIndex

    # Create an in-memory database for this tour
    db = SQLerDB.in_memory()
    print("Database connected!")
    print("\nFTS5 features: Boolean queries, phrase search, ranking, highlights")
    return FTSIndex, SQLerDB, SQLerLiteModel, dataclass, db


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. Creating a Searchable Model

    Let's create an `Article` model with title, content, and author fields.
    We'll populate it with sample articles for searching.
    """)
    return


@app.cell
def _(SQLerLiteModel, dataclass, db):
    @dataclass
    class Article(SQLerLiteModel):
        __tablename__ = "articles"

        title: str
        content: str
        author: str

    # Register the model with the database
    Article.set_db(db)

    # Create sample articles
    _articles = [
        ("Python Basics", "Learn Python programming fundamentals. Variables, loops, and functions.", "Alice"),
        ("Advanced Python", "Decorators, generators, and metaclasses in Python.", "Alice"),
        ("Web Development", "Build web applications with Flask and Django frameworks.", "Bob"),
        ("Data Science", "Pandas, NumPy, and machine learning with Python.", "Carol"),
        ("JavaScript Intro", "Getting started with JavaScript for web development.", "Dave"),
        ("React Tutorial", "Building modern UIs with React and JavaScript.", "Dave"),
        ("Database Design", "SQL basics and database normalization principles.", "Eve"),
        ("SQLite Guide", "Using SQLite for embedded database applications.", "Eve"),
    ]
    for _title, _content, _author in _articles:
        Article(title=_title, content=_content, author=_author).save()

    print(f"Created {Article.count()} articles")
    return (Article,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 3. Creating an FTS Index

    Create an FTS5 index on specific text fields using `FTSIndex`.
    This creates a virtual table optimized for full-text search.
    """)
    return


@app.cell
def _(Article, FTSIndex):
    # Create FTS index on title and content fields
    fts = FTSIndex(Article, fields=["title", "content"])

    # Create the FTS5 virtual table
    fts.create()
    print("FTS index created!")

    # Rebuild index from existing data
    fts.rebuild()
    print("Index populated with existing articles")
    return (fts,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 4. Basic Text Search

    Search for articles containing specific words.
    `search()` returns model instances directly:
    """)
    return


@app.cell
def _(fts):
    # Search for articles about Python - returns model instances
    _results = fts.search("python")

    print(f"Found {len(_results)} articles about 'python':")
    for _article in _results:
        print(f"  - {_article.title}")
    return


@app.cell
def _(fts):
    # Search for web development
    _web_results = fts.search("web")

    print(f"Found {len(_web_results)} articles about 'web':")
    for _article in _web_results:
        print(f"  - {_article.title}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 5. Boolean Queries

    Combine search terms with AND, OR, NOT operators for complex queries:
    """)
    return


@app.cell
def _(fts):
    # AND query (implicit - space between words)
    _results_and = fts.search("python machine")
    print("'python machine' (AND):")
    for _article in _results_and:
        print(f"  - {_article.title}")

    # OR query
    _results_or = fts.search("python OR javascript")
    print("\n'python OR javascript':")
    for _article in _results_or:
        print(f"  - {_article.title}")

    # NOT query
    _results_not = fts.search("python NOT basics")
    print("\n'python NOT basics':")
    for _article in _results_not:
        print(f"  - {_article.title}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. Phrase Search

    Search for exact phrases using double quotes:
    """)
    return


@app.cell
def _(fts):
    # Exact phrase search
    _phrase_results = fts.search('"web development"')

    print('Exact phrase "web development":')
    for _article in _phrase_results:
        print(f"  - {_article.title}: {_article.content[:50]}...")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. Prefix Search

    Search for words starting with a prefix using the `*` wildcard:
    """)
    return


@app.cell
def _(fts):
    # Prefix search - find words starting with "data"
    _prefix_results = fts.search("data*")

    print("Prefix 'data*':")
    for _article in _prefix_results:
        print(f"  - {_article.title}")

    # Prefix search for "learn"
    _learn_results = fts.search("learn*")
    print("\nPrefix 'learn*':")
    for _article in _learn_results:
        print(f"  - {_article.title}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 8. Ranked Results (BM25)

    `search_ranked()` returns SearchResult objects with relevance scores.
    Results are ranked using the BM25 algorithm:
    """)
    return


@app.cell
def _(fts):
    # Search with ranking - returns SearchResult objects
    _ranked = fts.search_ranked("python programming")

    print("Ranked results for 'python programming':")
    for _r in _ranked:
        print(f"  Score {_r.score:.4f}: {_r.model.title}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 9. Highlighted Snippets

    Get search results with matching terms highlighted.
    You can customize the highlight markers:
    """)
    return


@app.cell
def _(fts):
    # Search with highlights - returns SearchResult with highlights dict
    _highlighted = fts.search_with_highlights(
        "python", highlight_start="**", highlight_end="**"
    )

    print("Results with highlights:")
    for _r in _highlighted:
        print(f"\n{_r.model.title}:")
        if _r.highlights:
            for _field, _snippet in _r.highlights.items():
                print(f"  {_field}: {_snippet}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 10. Index Statistics

    Get information about the FTS index, including the number of indexed
    rows and total tokens:
    """)
    return


@app.cell
def _(fts):
    # Get index stats
    _stats = fts.stats()

    print("FTS Index Statistics:")
    print(f"  Table: {_stats.table_name}")
    print(f"  Indexed rows: {_stats.indexed_rows}")
    print(f"  Total tokens: {_stats.total_tokens}")
    print(f"  Fields: {_stats.fields}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 11. Note about SearchableMixin

    The Pydantic version of SQLer includes `SearchableMixin`, which adds
    search methods directly to model classes:

    ```python
    # Only works with SQLerModel (Pydantic)
    from sqler.fts import SearchableMixin

    class BlogPost(SearchableMixin, SQLerModel):
        _table = "blog_posts"
        title: str
        body: str
        class FTS:
            fields = ["title", "body"]

    BlogPost.create_search_index()
    results = BlogPost.search("python")  # search directly on model
    ```

    **With SQLerLiteModel**, use `FTSIndex` directly (as shown in this tour).
    This works identically and provides all the same functionality.
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    You've learned how to use SQLer's full-text search capabilities:

    | Function | Returns | Description |
    |----------|---------|-------------|
    | `fts.search(query)` | `list[Model]` | Basic search, returns models |
    | `fts.search_ranked(query)` | `list[SearchResult]` | With BM25 scores |
    | `fts.search_with_highlights(query)` | `list[SearchResult]` | With snippets |
    | `fts.search_count(query)` | `int` | Count matches |

    **FTS5 Query Syntax:**
    - `word1 word2`: Match both (AND)
    - `word1 OR word2`: Match either
    - `word1 NOT word2`: Match first, exclude second
    - `"exact phrase"`: Match exact phrase
    - `prefix*`: Match words starting with prefix

    **Key difference from Pydantic version:**
    - Use `FTSIndex` directly instead of `SearchableMixin`
    - All FTS functionality works identically with dataclass models
    - Use `__tablename__` instead of `_table`

    **Next up:** Tour 09 covers Change Tracking!
    """)
    return


@app.cell
def _(db):
    # Cleanup
    db.close()
    print("Database connection closed!")
    return


if __name__ == "__main__":
    app.run()
