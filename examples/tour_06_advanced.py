# /// script
# requires-python = ">=3.12"
# dependencies = ["sqler", "marimo"]
# ///

import marimo

__generated_with = "0.18.4"
app = marimo.App(width="medium")


@app.cell
def _():
    # --- marimo scaffolding (please ignore) ---
    import marimo as mo

    return (mo,)


@app.cell
def _(mo):
    mo.md(r"""
    # SQLer Tour: Advanced Features

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
    ## 1. Setting Up
    """)
    return


@app.cell
def _():
    from sqler import SQLerDB, SQLerModel
    from sqler.query import SQLerField as F

    db = SQLerDB.in_memory()
    print("Database connected!")
    return F, SQLerModel, db


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. Bulk Operations

    For performance, SQLer provides bulk update and delete operations that
    work directly on the database without loading models into memory.
    """)
    return


@app.cell
def _(SQLerModel, db):
    class Product(SQLerModel):
        _table = "products"
        name: str
        category: str
        price: float
        in_stock: bool = True

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
        Product(name=name, category=cat, price=price).save(db=db)

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
def _(F, Product, db):
    # Mark all hardware as out of stock
    updated_count = Product.using(db).filter(F("category") == "hardware").update(in_stock=False)
    print(f"Marked {updated_count} hardware products as out of stock")

    # Verify
    out_of_stock = Product.using(db).filter(F("in_stock") == False).all()
    print(f"Out of stock: {[p.name for p in out_of_stock]}")
    return


@app.cell
def _(F, Product, db):
    # Update prices for a category
    Product.using(db).filter(F("category") == "electronics").update(price=49.99)

    electronics = Product.using(db).filter(F("category") == "electronics").all()
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
def _(F, Product, db):
    print(f"Products before: {Product.using(db).count()}")

    # Delete all hardware products
    deleted_count = Product.using(db).filter(F("category") == "hardware").delete_all()
    print(f"Deleted {deleted_count} hardware products")

    print(f"Products after: {Product.using(db).count()}")
    remaining = Product.using(db).all()
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
def _(SQLerModel, db):
    class User(SQLerModel):
        _table = "users"
        name: str
        email: str
        age: int
        country: str

    # Create sample users (reduced for WASM performance)
    for i in range(20):
        User(
            name=f"User{i}",
            email=f"user{i}@example.com",
            age=40 + (i % 50),
            country=["US", "UK", "JP"][i % 3],
        ).save(db=db)

    print(f"Created {User.using(db).count()} users")
    return (User,)


@app.cell
def _(User, db):
    # Create an index on the email field
    User.add_index("email", unique=True, db=db)
    print("Created unique index on 'email'")

    # Create a non-unique index on age
    User.add_index("age", db=db)
    print("Created index on 'age'")

    # Create a compound index (if supported)
    User.add_index("country", db=db)
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
def _(User, db):
    # Safe to call multiple times
    User.ensure_index("email", unique=True, db=db)
    User.ensure_index("email", unique=True, db=db)  # No error, no duplicate
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
def _(SQLerModel, db):
    class Author(SQLerModel):
        _table = "authors"
        name: str

    class Book(SQLerModel):
        _table = "books"
        title: str
        author: Author | None = None

    # Create test data
    author = Author(name="Alice").save(db=db)
    book1 = Book(title="Book One", author=author).save(db=db)
    book2 = Book(title="Book Two", author=author).save(db=db)

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
def _(Author, author, db):
    from sqler.exceptions import IntegrityError

    # Try to delete author with restrict policy
    try:
        author.delete_with_policy(on_delete="restrict", db=db)
        print("Author deleted (unexpected!)")
    except IntegrityError as _e:
        print(f"IntegrityError: Cannot delete - {_e}")

    # Author still exists
    still_exists = Author.from_id(author._id, db=db)
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
def _(Author, Book, F, db):
    # Create a new author
    bob = Author(name="Bob").save(db=db)
    book3 = Book(title="Book Three", author=bob).save(db=db)

    print(f"Before: Book '{book3.title}' has author '{book3.author.name}'")

    # Delete with set_null policy
    bob.delete_with_policy(on_delete="set_null", db=db)
    print("\nDeleted Bob with set_null policy")

    # Check the book - author should be None
    book3.refresh(db=db)
    print(f"After: Book '{book3.title}' has author: {book3.author}")

    # Verify Bob is deleted
    bob_exists = Author.using(db).filter(F("name") == "Bob").exists()
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
def _(Author, Book, db):
    # Create another author with books
    carol = Author(name="Carol").save(db=db)
    Book(title="Carol's Book 1", author=carol).save(db=db)
    Book(title="Carol's Book 2", author=carol).save(db=db)

    carol_id = carol._id
    books_before = Book.using(db).filter(Book.ref("author").field("_id") == carol_id).count()
    print(f"Created Carol with {books_before} books")
    print(f"Total books before: {Book.using(db).count()}")

    # Delete with cascade
    carol.delete_with_policy(on_delete="cascade", db=db)
    print("\nDeleted Carol with cascade policy")

    print(f"Total books after: {Book.using(db).count()}")
    carol_books_after = Book.using(db).filter(Book.ref("author").field("name") == "Carol").all()
    print(f"Carol's books remaining: {len(carol_books_after)}")
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
    result = db.adapter.execute(
        "SELECT _id, data FROM users WHERE json_extract(data, '$.age') > ? LIMIT 5", [40]
    ).fetchall()

    print("Raw SQL results (users over 40):")
    for row in result:
        user = User.from_id(row[0], db=db)
        print(f"  {user.name}, age {user.age}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. Query Debugging

    SQLer provides tools to inspect and debug your queries.
    """)
    return


@app.cell
def _(F, User, db):
    # Build a complex query
    _query = (
        User.using(db)
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
def _(F, User, db):
    # Get debug info for a query
    _query = User.using(db).filter(F("email").contains("user1"))
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
def _(F, User, db):
    # Find users in US OR UK
    results = (
        User.using(db).filter(F("country") == "US").or_filter(F("country") == "UK").limit(10).all()
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
def _(User, db):
    # Get distinct countries
    countries = User.using(db).distinct_values("country")
    print(f"Distinct countries: {countries}")

    # Get distinct ages (first 10)
    ages = User.using(db).distinct_values("age")
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
