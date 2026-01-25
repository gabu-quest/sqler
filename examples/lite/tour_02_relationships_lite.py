# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo"]
# ///
"""SQLer Lite Tour: Relationships - Works in Pyodide/WASM!"""

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
                import micropip
                import js

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
    mo.md(
        r"""
        # SQLer Lite Tour: Relationships

        This notebook covers how to define and work with relationships between models
        in SQLer Lite (Pyodide/WASM compatible). You'll learn:

        1. Defining relationships between models
        2. Saving related models
        3. Automatic hydration (loading related data)
        4. Working with references

        Let's get started!
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 1. Setting Up

        We'll create a database and define two related models: `Author` and `Book`.
        A book has an author (many-to-one relationship).
        """
    )
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

    from dataclasses import dataclass
    from typing import Optional

    import importlib

    _sqler = importlib.import_module("sqler")
    F = _sqler.F
    SQLerDB = _sqler.SQLerDB
    SQLerLiteModel = _sqler.SQLerLiteModel

    db = SQLerDB.in_memory()
    print("Database connected!")
    return F, Optional, SQLerDB, SQLerLiteModel, dataclass, db


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 2. Defining Related Models

        To create a relationship, simply use one model as a type hint in another.
        SQLer stores a reference (the related model's `_id`) and automatically
        hydrates (loads) the full object when you query.
        """
    )
    return


@app.cell
def _(Optional, SQLerLiteModel, dataclass, db):
    @dataclass
    class Author(SQLerLiteModel):
        __tablename__ = "authors"
        name: str
        country: str

    @dataclass
    class Book(SQLerLiteModel):
        __tablename__ = "books"
        title: str
        year: int
        author: Optional[Author] = None  # Relationship to Author

    # Register both models
    Author.set_db(db)
    Book.set_db(db)
    print("Models registered!")
    return Author, Book


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. Creating Related Data

        When you save a model with a relationship, SQLer stores just the reference
        (the `_id` of the related model). The related model must be saved first.
        """
    )
    return


@app.cell
def _(Author, Book):
    # Create and save authors first
    alice = Author(name="Alice Smith", country="USA")
    alice.save()

    bob = Author(name="Bob Jones", country="UK")
    bob.save()

    print(f"Created authors: Alice (id={alice._id}), Bob (id={bob._id})")

    # Now create books with author relationships
    book1 = Book(title="Python Mastery", year=2023, author=alice)
    book1.save()

    book2 = Book(title="Web Development", year=2024, author=alice)
    book2.save()

    book3 = Book(title="Database Design", year=2022, author=bob)
    book3.save()

    print("\nCreated books:")
    print(f"  - {book1.title} by {book1.author.name}")
    print(f"  - {book2.title} by {book2.author.name}")
    print(f"  - {book3.title} by {book3.author.name}")
    return alice, bob, book1, book2, book3


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 4. Automatic Hydration

        When you query for books, SQLer automatically "hydrates" the relationships,
        loading the full `Author` object instead of just the ID reference.
        """
    )
    return


@app.cell
def _(Book):
    # Query all books - relationships are automatically hydrated
    all_books = Book.all()

    print("All books with hydrated authors:")
    for _b in all_books:
        author_name = _b.author.name if _b.author else "Unknown"
        author_country = _b.author.country if _b.author else "?"
        print(f"  '{_b.title}' ({_b.year}) by {author_name} from {author_country}")
    return (all_books,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 5. How References Work

        Under the hood, SQLer stores relationships as reference dictionaries with
        the table name and ID. Let's peek at the raw data.
        """
    )
    return


@app.cell
def _(db):
    import json

    # Look at the raw data in the database
    raw = db.adapter.execute("SELECT _id, data FROM books LIMIT 1").fetchone()
    data = json.loads(raw[1])
    print("Raw book data in database:")
    print(json.dumps(data, indent=2))
    return data, json, raw


@app.cell
def _(mo):
    mo.md(
        r"""
        Notice the `author` field contains `{"_table": "authors", "_id": 1}` - this
        is the reference format SQLer uses. When you query, it automatically looks
        up this reference and replaces it with the full Author object.
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. Loading Individual Records

        When you load a book by ID, relationships are automatically resolved:
        """
    )
    return


@app.cell
def _(Book, book1):
    # Load a book by ID
    loaded_book = Book.from_id(book1._id)
    print(f"Loaded book: {loaded_book.title}")
    print(f"  Author: {loaded_book.author.name}")
    print(f"  Author's country: {loaded_book.author.country}")
    return (loaded_book,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. Updating Related Data

        When you update a related model and save it, you need to call `.refresh()`
        on the parent model to see the changes:
        """
    )
    return


@app.cell
def _(Book, alice, book1):
    print(f"Before: {book1.author.name}")

    # Update the author
    alice.name = "Alice Smith-Johnson"
    alice.save()

    # The book still has the old data in memory
    print(f"Book still shows: {book1.author.name}")

    # Refresh the book to get updated data
    book1.refresh()
    print(f"After refresh: {book1.author.name}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 8. Multiple Relationships

        Models can have multiple relationships. Let's add a `Publisher` model:
        """
    )
    return


@app.cell
def _(Author, Optional, SQLerLiteModel, dataclass, db):
    @dataclass
    class Publisher(SQLerLiteModel):
        __tablename__ = "publishers"
        name: str
        location: str

    @dataclass
    class Magazine(SQLerLiteModel):
        __tablename__ = "magazines"
        title: str
        issue: int
        editor: Optional[Author] = None
        publisher: Optional[Publisher] = None

    Publisher.set_db(db)
    Magazine.set_db(db)

    # Create data
    pub = Publisher(name="TechMedia", location="San Francisco")
    pub.save()

    # Reuse existing author as editor
    editor = Author.from_id(1)  # Alice

    mag = Magazine(title="Code Weekly", issue=42, editor=editor, publisher=pub)
    mag.save()

    print(f"Created magazine: {mag.title} #{mag.issue}")
    print(f"  Editor: {mag.editor.name}")
    print(f"  Publisher: {mag.publisher.name} ({mag.publisher.location})")
    return Magazine, Publisher, editor, mag, pub


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 9. Self-Referential Relationships

        Models can even reference themselves (e.g., for tree structures):
        """
    )
    return


@app.cell
def _(Optional, SQLerLiteModel, dataclass, db):
    @dataclass
    class Category(SQLerLiteModel):
        __tablename__ = "categories"
        name: str
        parent: Optional["Category"] = None

    Category.set_db(db)

    # Create a category tree
    root = Category(name="Electronics")
    root.save()

    computers = Category(name="Computers", parent=root)
    computers.save()

    laptops = Category(name="Laptops", parent=computers)
    laptops.save()

    print("Category hierarchy:")
    print(f"  {root.name}")
    print(f"    - {computers.name}")
    print(f"        - {laptops.name}")

    # Verify relationships work
    fetched = Category.from_id(laptops._id)
    print(f"\nFetched '{fetched.name}', parent is '{fetched.parent.name}'")
    return Category, computers, fetched, laptops, root


@app.cell
def _(mo):
    mo.md(
        r"""
        ## Summary

        You've learned how to work with relationships in SQLer Lite:

        | Concept | How To |
        |---------|--------|
        | Define relationship | Use related model as type hint: `author: Optional[Author] = None` |
        | Save related | Save child first, then parent with reference |
        | Auto hydration | Relationships are loaded automatically |
        | Update related | Modify + save related, then `.refresh()` parent |

        **Key difference from Pydantic version:**
        - Use `@dataclass` decorator on your model classes
        - Use `Optional[ModelType]` for nullable relationships
        - Works identically otherwise!

        **Next up:** Tour 03 covers Safe Models with optimistic locking!
        """
    )
    return


@app.cell
def _(db):
    db.close()
    print("Database closed!")
    return


if __name__ == "__main__":
    app.run()
