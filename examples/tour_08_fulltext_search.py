# /// script
# requires-python = ">=3.12"
# dependencies = ["sqler", "marimo"]
# ///

import marimo

__generated_with = "0.18.4"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(r"""
    # SQLer Tour: Full-Text Search

    This notebook covers SQLer's full-text search capabilities using
    SQLite's FTS5 extension.

    You'll learn:

    1. Creating FTS indexes
    2. Basic text search
    3. Boolean queries (AND, OR, NOT)
    4. Phrase and prefix search
    5. Ranked results (BM25)
    6. Highlighted snippets

    Let's explore!
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 1. Setting Up
    """)
    return


@app.cell
def _():
    from sqler import SQLerDB, SQLerModel
    from sqler.fts import FTSIndex, FTSStats, SearchResult

    db = SQLerDB.in_memory()
    print("Database connected!")
    print("\nFTS5 features: Boolean queries, phrase search, ranking, highlights")
    return FTSIndex, SQLerModel, db


@app.cell
def _(SQLerModel, db):
    class Article(SQLerModel):
        _table = "articles"
        title: str
        content: str
        author: str

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

    print(f"Created {Article.query().count()} articles")
    return (Article,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. Creating an FTS Index

    Create an FTS5 index on specific text fields:
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
    ## 3. Basic Text Search

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
    ## 4. Boolean Queries

    Combine search terms with AND, OR, NOT:
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
    ## 5. Phrase Search

    Search for exact phrases using quotes:
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
    ## 6. Prefix Search

    Search for words starting with a prefix using *:
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
    ## 7. Ranked Results (BM25)

    `search_ranked()` returns SearchResult objects with relevance scores:
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
    ## 8. Highlighted Snippets

    Get search results with matching terms highlighted:
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
    ## 9. Index Statistics

    Get information about the FTS index:
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
    ## 10. SearchableMixin

    For convenience, use `SearchableMixin` to add search directly to models:
    """)
    return


@app.cell
def _(SQLerModel, db):
    from sqler.fts import SearchableMixin

    class BlogPost(SearchableMixin, SQLerModel):
        _table = "blog_posts"
        title: str
        body: str

        class FTS:
            fields = ["title", "body"]

    BlogPost.set_db(db)

    # Create the search index
    BlogPost.create_search_index()

    # Create and index posts
    BlogPost(title="Hello World", body="My first blog post about Python").save()
    BlogPost(title="SQLer Tips", body="Best practices for using SQLer ORM").save()

    # Search directly on model - returns model instances
    _results = BlogPost.search("python")
    print("BlogPost.search('python'):")
    for _post in _results:
        print(f"  - {_post.title}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    SQLer FTS (Full-Text Search) features:

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

    **SearchableMixin** adds `.search()`, `.search_ranked()` to models.

    **Next up:** Tour 09 covers Change Tracking!
    """)
    return


@app.cell
def _(db):
    db.close()
    print("Database closed!")
    return


if __name__ == "__main__":
    app.run()
