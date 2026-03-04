# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo"]
# ///
"""SQLer Lite Tour: Advanced Features - Works in Pyodide/WASM!"""

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

                await micropip.install("sqler")
            except Exception as exc:
                print("Failed to install sqler in Pyodide:", exc)
            else:
                if importlib_util.find_spec("sqler") is not None:
                    sqler_ready = True

    return (pyodide_sqlite3_ready, sqler_ready)


@app.cell
def _(mo):
    mo.md(r"""
    # SQLer Lite Tour: Advanced Features

    This notebook covers advanced SQLer features for production use cases.

    You'll learn:

    1. Bulk operations (update, delete_all)
    2. Index management
    3. Integrity policies (restrict, set_null, cascade)
    4. Raw SQL queries
    5. Query debugging and explain plans

    Let's explore!
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    > **Lite vs Pydantic**: This tour uses `SQLerLiteModel` (dataclasses) so it runs
    > in your browser via WebAssembly. The advanced features shown here work identically
    > with both `SQLerModel` (Pydantic) and `SQLerLiteModel` (dataclasses).
    >
    > ```python
    > # Pydantic version (requires local install)
    > from sqler import SQLerModel
    >
    > class Product(SQLerModel):
    >     _table = "products"
    >     name: str
    >     price: float
    > ```
    >
    > Run locally: `uv run marimo edit examples/tour_06_advanced.py`
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 1. Setting Up
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
    _sqler_exceptions = importlib.import_module("sqler.exceptions")
    F = _sqler.F
    SQLerDB = _sqler.SQLerDB
    SQLerLiteModel = _sqler.SQLerLiteModel
    IntegrityError = _sqler_exceptions.IntegrityError

    db = SQLerDB.in_memory()
    print("Database connected!")
    return F, IntegrityError, SQLerDB, SQLerLiteModel, dataclass, db


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. Bulk Operations

    For performance, SQLer provides bulk update and delete operations that
    work directly on the database without loading models into memory.
    """)
    return


@app.cell
def _(SQLerLiteModel, dataclass, db):
    @dataclass
    class Product(SQLerLiteModel):
        __tablename__ = "products"
        name: str
        category: str
        price: float
        in_stock: bool = True

    Product.set_db(db)

    # Create sample data
    products = [
        ("Widget A", "electronics", 29.99),
        ("Widget B", "electronics", 39.99),
        ("Gadget X", "electronics", 99.99),
        ("Tool 1", "hardware", 19.99),
        ("Tool 2", "hardware", 24.99),
        ("Tool 3", "hardware", 14.99),
    ]
    for name, cat, price in products:
        Product(name=name, category=cat, price=price).save()

    print(f"Created {len(products)} products")
    return (Product,)


@app.cell
def _(mo):
    mo.md(r"""
    ### Bulk Update

    Update multiple records matching a filter with `.update()`:
    """)
    return


@app.cell
def _(F, Product):
    # Mark all hardware as out of stock
    updated_count = Product.query().filter(F("category") == "hardware").update(in_stock=False)
    print(f"Marked {updated_count} hardware products as out of stock")

    # Verify
    out_of_stock = Product.query().filter(F("in_stock") == False).all()
    print(f"Out of stock: {[p.name for p in out_of_stock]}")
    return


@app.cell
def _(F, Product):
    # Update prices for a category
    Product.query().filter(F("category") == "electronics").update(price=49.99)

    electronics = Product.query().filter(F("category") == "electronics").all()
    print("Electronics prices after bulk update:")
    for p in electronics:
        print(f"  {p.name}: ${p.price}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ### Bulk Delete

    Delete multiple records with `.delete_all()`:
    """)
    return


@app.cell
def _(F, Product):
    print(f"Products before: {Product.query().count()}")

    # Delete all hardware products
    deleted_count = Product.query().filter(F("category") == "hardware").delete_all()
    print(f"Deleted {deleted_count} hardware products")

    print(f"Products after: {Product.query().count()}")
    remaining = Product.query().all()
    print(f"Remaining: {[p.name for p in remaining]}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 3. Index Management

    Indexes speed up queries on frequently filtered fields. SQLer provides
    methods to create, check, and manage indexes.
    """)
    return


@app.cell
def _(SQLerLiteModel, dataclass, db):
    @dataclass
    class User(SQLerLiteModel):
        __tablename__ = "users"
        name: str
        email: str
        age: int
        country: str

    User.set_db(db)

    # Create sample users (reduced for WASM performance)
    for i in range(20):
        User(
            name=f"User{i}",
            email=f"user{i}@example.com",
            age=40 + (i % 50),
            country=["US", "UK", "JP"][i % 3],
        ).save()

    print(f"Created {User.query().count()} users")
    return (User,)


@app.cell
def _(User):
    # Create an index on the email field
    User.add_index("email", unique=True)
    print("Created unique index on 'email'")

    # Create a non-unique index on age
    User.add_index("age")
    print("Created index on 'age'")

    # Create a compound index (if supported)
    User.add_index("country")
    print("Created index on 'country'")
    return


@app.cell
def _(db):
    # List all indexes on the users table
    indexes = db.adapter.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='users'"
    ).fetchall()

    print("Indexes on users table:")
    for idx in indexes:
        print(f"  {idx[0]}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ### Using ensure_index

    `ensure_index` is idempotent - it only creates the index if it doesn't exist:
    """)
    return


@app.cell
def _(User):
    # Safe to call multiple times
    User.ensure_index("email", unique=True)
    User.ensure_index("email", unique=True)  # No error, no duplicate
    print("ensure_index is safe to call multiple times")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 4. Integrity Policies

    When deleting a record that other records reference, you need to decide
    what happens. SQLer supports three policies:

    - `restrict`: Block deletion if references exist
    - `set_null`: Set referencing fields to null
    - `cascade`: Delete referencing records too
    """)
    return


@app.cell
def _(SQLerLiteModel, dataclass, db):
    @dataclass
    class Author(SQLerLiteModel):
        __tablename__ = "authors"
        name: str

    @dataclass
    class Book(SQLerLiteModel):
        __tablename__ = "books"
        title: str
        author: Author | None = None

    Author.set_db(db)
    Book.set_db(db)

    # Create test data
    author = Author(name="Alice").save()
    book1 = Book(title="Book One", author=author).save()
    book2 = Book(title="Book Two", author=author).save()

    print(f"Created author '{author.name}' with 2 books")
    return Author, Book, author


@app.cell
def _():
    return


@app.cell
def _(mo):
    mo.md(r"""
    ### Restrict Policy

    Prevents deletion if any records reference this one:
    """)
    return


@app.cell
def _(Author, IntegrityError, author):
    # Try to delete author with restrict policy
    try:
        author.delete_with_policy(on_delete="restrict")
        print("Author deleted (unexpected!)")
    except IntegrityError as _e:
        print(f"IntegrityError: Cannot delete - {_e}")

    # Author still exists
    still_exists = Author.from_id(author._id)
    print(f"Author still exists: {still_exists.name}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ### Set Null Policy

    Sets the reference to null in all referencing records:
    """)
    return


@app.cell
def _(Author, Book, F):
    # Create a new author
    bob = Author(name="Bob").save()
    book3 = Book(title="Book Three", author=bob).save()

    print(f"Before: Book '{book3.title}' has author '{book3.author.name}'")

    # Delete with set_null policy
    bob.delete_with_policy(on_delete="set_null")
    print("\nDeleted Bob with set_null policy")

    # Check the book - author should be None
    book3.refresh()
    print(f"After: Book '{book3.title}' has author: {book3.author}")

    # Verify Bob is deleted
    bob_exists = Author.query().filter(F("name") == "Bob").exists()
    print(f"Bob exists: {bob_exists}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ### Cascade Policy

    Deletes all referencing records:
    """)
    return


@app.cell
def _(Author, Book):
    # Create another author with books
    carol = Author(name="Carol").save()
    Book(title="Carol's Book 1", author=carol).save()
    Book(title="Carol's Book 2", author=carol).save()

    _carol_id = carol._id
    _total_before = Book.query().count()
    print("Created Carol with 2 books")
    print(f"Total books before: {_total_before}")

    # Delete with cascade
    carol.delete_with_policy(on_delete="cascade")
    print("\nDeleted Carol with cascade policy")

    _total_after = Book.query().count()
    print(f"Total books after: {_total_after}")
    print(f"Carol's books removed: {_total_before - _total_after}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 5. Raw SQL Queries

    For complex queries that can't be expressed with the query builder,
    you can use raw SQL while still getting model instances:
    """)
    return


@app.cell
def _(User, db):
    # Raw SQL query
    _result = db.adapter.execute(
        "SELECT _id, data FROM users WHERE json_extract(data, '$.age') > ? LIMIT 5", [40]
    ).fetchall()

    print("Raw SQL results (users over 40):")
    for row in _result:
        _user = User.from_id(row[0])
        print(f"  {_user.name}, age {_user.age}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. Query Debugging

    SQLer provides tools to inspect and debug your queries.
    """)
    return


@app.cell
def _(F, User):
    # Build a complex query
    _query = (
        User.query()
        .filter(F("age") > 30)
        .filter(F("country") == "US")
        .order_by("age", desc=True)
        .limit(10)
    )

    # Inspect the SQL
    print("Generated SQL:")
    print(f"  {_query.sql()}")
    print(f"\nParameters: {_query.params()}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ### Debug Query

    Use `.debug()` to see the SQL and parameters:
    """)
    return


@app.cell
def _(F, User):
    # Get debug info for a query
    _query = User.query().filter(F("email").contains("user1"))
    _sql, _params = _query.debug()

    print("Debug output:")
    print(f"  SQL: {_sql}")
    print(f"  Params: {_params}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. OR Filters

    Combine filters with OR logic using `.or_filter()`:
    """)
    return


@app.cell
def _(F, User):
    # Find users in US OR UK
    results = (
        User.query().filter(F("country") == "US").or_filter(F("country") == "UK").limit(10).all()
    )

    print("Users in US or UK:")
    for u in results:
        print(f"  {u.name} ({u.country})")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 8. Distinct Queries

    Get unique values or deduplicated results:
    """)
    return


@app.cell
def _(User):
    # Get distinct countries
    countries = User.query().distinct_values("country")
    print(f"Distinct countries: {countries}")

    # Get distinct ages (first 10)
    ages = User.query().distinct_values("age")
    print(f"Distinct ages: {sorted(ages)[:10]}...")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    Advanced SQLer features for production use:

    | Feature | Method |
    |---------|--------|
    | Bulk update | `.filter(...).update(field=value)` |
    | Bulk delete | `.filter(...).delete_all()` |
    | Create index | `Model.add_index("field", unique=True)` |
    | Safe index | `Model.ensure_index("field")` |
    | Restrict delete | `.delete_with_policy(on_delete="restrict")` |
    | Set null delete | `.delete_with_policy(on_delete="set_null")` |
    | Cascade delete | `.delete_with_policy(on_delete="cascade")` |
    | Raw SQL | `db.adapter.execute(sql, params)` |
    | View SQL | `query.sql()`, `query.params()` |
    | Explain plan | `query.explain()` |
    | OR filter | `.or_filter(...)` |
    | Distinct values | `.distinct_values("field")` |

    **Congratulations!** You've completed the SQLer tour!
    """)
    return


@app.cell
def _(db):
    db.close()
    print("Database closed!")
    return


if __name__ == "__main__":
    app.run()
