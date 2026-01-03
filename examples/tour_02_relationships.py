import marimo

__generated_with = "0.18.4"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(
        r"""
        # SQLer Tour: Relationships

        This notebook covers how to define and work with relationships between models
        in SQLer. You'll learn:

        1. Defining relationships between models
        2. Saving related models
        3. Automatic hydration (loading related data)
        4. Querying across relationships
        5. Controlling hydration behavior

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
def _():
    from sqler import SQLerDB, SQLerModel
    from sqler.query import SQLerField as F

    db = SQLerDB.in_memory()
    print("Database connected!")
    return F, SQLerDB, SQLerModel, db


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
def _(SQLerModel, db):
    class Author(SQLerModel):
        _table = "authors"
        name: str
        country: str

    class Book(SQLerModel):
        _table = "books"
        title: str
        year: int
        author: Author | None = None  # Relationship to Author

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

    print(f"\nCreated books:")
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
    all_books = Book.query().all()

    print("All books with hydrated authors:")
    for _b in all_books:
        author_name = _b.author.name if _b.author else "Unknown"
        author_country = _b.author.country if _b.author else "?"
        print(f"  '{_b.title}' ({_b.year}) by {author_name} from {author_country}")
    return ()


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
def _(Book, db):
    # Look at the raw data in the database
    raw = db.adapter.execute("SELECT _id, data FROM books LIMIT 1").fetchone()
    import json
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
        ## 6. Querying Across Relationships

        SQLer provides ways to query based on related model fields using `Model.ref()`:
        """
    )
    return


@app.cell
def _(Book):
    # Find books by authors from a specific country
    usa_books = Book.query().filter(
        Book.ref("author").field("country") == "USA"
    ).all()

    print("Books by USA authors:")
    for _b in usa_books:
        print(f"  - {_b.title} by {_b.author.name}")
    return ()


@app.cell
def _(Book):
    # Find books by a specific author name
    alice_books = Book.query().filter(
        Book.ref("author").field("name") == "Alice Smith"
    ).all()

    print("Books by Alice Smith:")
    for _b in alice_books:
        print(f"  - {_b.title} ({_b.year})")
    return ()


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. Understanding Reference Storage

        Under the hood, relationships are stored as reference dictionaries.
        When you query, SQLer automatically resolves these references.
        This happens transparently - you always get fully hydrated objects.

        Note: The `.resolve(False)` option exists for advanced use cases,
        but Pydantic validation requires the full objects, so hydration
        is typically always enabled.
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 8. Updating Related Data

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
        ## 9. Multiple Relationships

        Models can have multiple relationships. Let's add a `Publisher` model:
        """
    )
    return


@app.cell
def _(Author, SQLerModel, db):
    class Publisher(SQLerModel):
        _table = "publishers"
        name: str
        location: str

    class Magazine(SQLerModel):
        _table = "magazines"
        title: str
        issue: int
        editor: Author | None = None
        publisher: Publisher | None = None

    Publisher.set_db(db)
    Magazine.set_db(db)

    # Create data
    pub = Publisher(name="TechMedia", location="San Francisco").save()

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
        ## 10. Self-Referential Relationships

        Models can even reference themselves (e.g., for tree structures):
        """
    )
    return


@app.cell
def _(SQLerModel, db):
    from typing import Optional

    class Category(SQLerModel):
        _table = "categories"
        name: str
        parent: Optional["Category"] = None

    Category.set_db(db)

    # Create a category tree
    root = Category(name="Electronics").save()
    computers = Category(name="Computers", parent=root).save()
    laptops = Category(name="Laptops", parent=computers).save()

    print("Category hierarchy:")
    print(f"  {root.name}")
    print(f"    └── {computers.name}")
    print(f"        └── {laptops.name}")

    # Verify relationships work
    fetched = Category.from_id(laptops._id)
    print(f"\nFetched '{fetched.name}', parent is '{fetched.parent.name}'")
    return Category, Optional, computers, fetched, laptops, root


@app.cell
def _(mo):
    mo.md(
        r"""
        ## Summary

        You've learned how to work with relationships in SQLer:

        | Concept | How To |
        |---------|--------|
        | Define relationship | Use related model as type hint: `author: Author \| None = None` |
        | Save related | Save child first, then parent with reference |
        | Auto hydration | Relationships are loaded automatically |
        | Query across | `Model.ref("field").field("nested_field")` |
        | Skip hydration | `.resolve(False)` for raw references |
        | Update related | Modify + save related, then `.refresh()` parent |

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
