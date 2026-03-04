# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo"]
# ///
"""SQLer Lite Tour: Fundamentals - Works in Pyodide/WASM!"""

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
    # SQLer Lite Tour: Fundamentals

    Welcome to SQLer Lite! This interactive notebook teaches you the fundamentals
    of SQLer using **dataclass-based models** that work in **Pyodide/WASM** environments.

    **What you'll learn:**
    1. Creating and connecting to a database
    2. Defining models with dataclasses (no Pydantic required!)
    3. Basic CRUD operations (Create, Read, Update, Delete)
    4. Querying with the fluent API
    5. Field operations and filters

    Let's dive in!
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 1. Setting Up

    First, we import SQLer and create an in-memory database. SQLer Lite uses
    standard Python dataclasses instead of Pydantic, making it compatible with
    browser environments like Pyodide.
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
    F = _sqler.F
    SQLerDB = _sqler.SQLerDB
    SQLerLiteModel = _sqler.SQLerLiteModel

    # Create an in-memory database for this tour
    db = SQLerDB.in_memory()
    print("Connected to in-memory database!")
    pydantic_available = getattr(_sqler, "PYDANTIC_AVAILABLE", None)
    if pydantic_available is not None:
        print(f"Pydantic available: {pydantic_available}")
    get_model_backend = getattr(_sqler, "get_model_backend", None)
    if callable(get_model_backend):
        print(f"Model backend: {get_model_backend()}")
    return F, SQLerDB, SQLerLiteModel, dataclass, db


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. Defining Models

    Models in SQLer Lite use Python's `@dataclass` decorator and inherit from
    `SQLerLiteModel`. Each model automatically gets:
    - An `_id` field (auto-generated integer)
    - A `__tablename__` class variable for the table name
    - JSON serialization/deserialization

    Let's create a simple `User` model:
    """)
    return


@app.cell
def _(SQLerLiteModel, dataclass, db):
    @dataclass
    class User(SQLerLiteModel):
        __tablename__ = "users"

        name: str
        email: str
        age: int = 0
        is_active: bool = True

    # Register the model with the database (creates the table)
    User.set_db(db)
    print("User table created!")
    print(f"Table name: {User._table}")
    return (User,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 3. Creating Records (INSERT)

    Create model instances and call `.save()` to persist them to the database.
    The `_id` is auto-generated if not provided.
    """)
    return


@app.cell
def _(User):
    # Create some users
    alice = User(name="Alice", email="alice@example.com", age=30)
    bob = User(name="Bob", email="bob@example.com", age=25)
    charlie = User(name="Charlie", email="charlie@example.com", age=35, is_active=False)

    # Save them to the database
    alice.save()
    bob.save()
    charlie.save()

    print(f"Created Alice with ID: {alice._id}")
    print(f"Created Bob with ID: {bob._id}")
    print(f"Created Charlie with ID: {charlie._id}")
    return alice, bob, charlie


@app.cell
def _(mo):
    mo.md(r"""
    ## 4. Reading Records (SELECT)

    SQLer provides multiple ways to fetch records:
    - `Model.from_id(id)` - Fetch a single record by ID
    - `Model.filter(...)` - Start a filtered query
    - `Model.all()` - Fetch all records
    """)
    return


@app.cell
def _(User, alice):
    # Fetch by ID
    fetched_alice = User.from_id(alice._id)
    print(f"Fetched: {fetched_alice.name}, {fetched_alice.email}")

    # Fetch all users
    all_users = User.all()
    print(f"\nAll users ({len(all_users)}):")
    for u in all_users:
        print(f"  - {u.name} (age: {u.age}, active: {u.is_active})")
    return all_users, fetched_alice, u


@app.cell
def _(mo):
    mo.md(r"""
    ## 5. Updating Records (UPDATE)

    Modify the model's attributes and call `.save()` again. SQLer uses upsert
    semantics - if the `_id` exists, it updates; otherwise, it inserts.
    """)
    return


@app.cell
def _(User, bob):
    # Update Bob's age
    bob.age = 26
    bob.save()

    # Verify the update
    updated_bob = User.from_id(bob._id)
    print(f"Bob's new age: {updated_bob.age}")
    return (updated_bob,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. Deleting Records (DELETE)

    Call `.delete()` on a model instance to remove it from the database.
    """)
    return


@app.cell
def _(User, charlie):
    # Delete Charlie
    charlie.delete()

    # Verify deletion
    remaining = User.all()
    print(f"Remaining users: {[u.name for u in remaining]}")
    return (remaining,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. Querying with the Fluent API

    The real power of SQLer is its fluent query builder. Use `Model.filter()`
    with `F()` field expressions for conditions:
    """)
    return


@app.cell
def _(F, User):
    # Add more users for querying examples
    User(name="Diana", email="diana@example.com", age=28).save()
    User(name="Eve", email="eve@corp.com", age=32).save()
    User(name="Frank", email="frank@corp.com", age=28, is_active=False).save()

    # Filter by exact match
    active_users = User.filter(F("is_active") == True).all()
    print(f"Active users: {[u.name for u in active_users]}")

    # Filter by age
    users_28 = User.filter(F("age") == 28).all()
    print(f"Users aged 28: {[u.name for u in users_28]}")
    return active_users, users_28


@app.cell
def _(mo):
    mo.md(r"""
    ### Field Operations with F()

    The `F()` helper lets you reference fields and apply various operations:
    """)
    return


@app.cell
def _(F, User):
    # Greater than
    older_than_28 = User.filter(F("age") > 28).all()
    print(f"Older than 28: {[u.name for u in older_than_28]}")

    # Less than or equal
    young_or_28 = User.filter(F("age") <= 28).all()
    print(f"28 or younger: {[u.name for u in young_or_28]}")

    # String substring match (using LIKE)
    corp_users = User.filter(F("email").like("%corp%")).all()
    print(f"Corporate emails: {[u.name for u in corp_users]}")
    return corp_users, older_than_28, young_or_28


@app.cell
def _(mo):
    mo.md(r"""
    ### More Field Operations

    SQLer supports a rich set of field operations:
    """)
    return


@app.cell
def _(F, User):
    # Between (inclusive)
    age_range = User.filter(F("age").between(27, 30)).all()
    print(f"Age 27-30: {[u.name for u in age_range]}")

    # Starts with
    a_names = User.filter(F("name").startswith("A")).all()
    print(f"Names starting with A: {[u.name for u in a_names]}")

    # In list
    specific_ages = User.filter(F("age").in_list([28, 32])).all()
    print(f"Age 28 or 32: {[u.name for u in specific_ages]}")

    # Not equal
    not_alice = User.filter(F("name") != "Alice").all()
    print(f"Not Alice: {[u.name for u in not_alice]}")
    return a_names, age_range, not_alice, specific_ages


@app.cell
def _(mo):
    mo.md(r"""
    ### Combining Conditions

    Chain `.filter()` calls to combine conditions with AND logic:
    """)
    return


@app.cell
def _(F, User):
    # Multiple filters (AND)
    active_and_young = User.filter(F("is_active") == True).filter(F("age") < 30).all()
    print(f"Active AND under 30: {[u.name for u in active_and_young]}")
    return (active_and_young,)


@app.cell
def _(mo):
    mo.md(r"""
    ### Ordering and Limiting

    Control the order and number of results:
    """)
    return


@app.cell
def _(User):
    # Order by age (ascending - default)
    by_age_asc = User.query().order_by("age").all()
    print(f"By age (asc): {[(u.name, u.age) for u in by_age_asc]}")

    # Order by age (descending)
    by_age_desc = User.query().order_by("age", desc=True).all()
    print(f"By age (desc): {[(u.name, u.age) for u in by_age_desc]}")

    # Limit results
    top_2_oldest = User.query().order_by("age", desc=True).limit(2).all()
    print(f"Top 2 oldest: {[(u.name, u.age) for u in top_2_oldest]}")
    return by_age_asc, by_age_desc, top_2_oldest


@app.cell
def _(mo):
    mo.md(r"""
    ## 8. Aggregations

    SQLer provides aggregation methods for common calculations:
    """)
    return


@app.cell
def _(F, User):
    # Count
    total = User.count()
    print(f"Total users: {total}")

    # Count with filter
    active_count = User.filter(F("is_active") == True).count()
    print(f"Active users: {active_count}")
    return active_count, total


@app.cell
def _(mo):
    mo.md(r"""
    ## 9. First

    Use `.first()` to get the first matching record (or `None` if none match):
    """)
    return


@app.cell
def _(F, User):
    # First (safe - returns None if not found)
    oldest = User.query().order_by("age", desc=True).first()
    print(f"Oldest user: {oldest.name if oldest else 'None'}")

    # First with filter
    first_corp = User.filter(F("email").like("%corp%")).first()
    print(f"First corp user: {first_corp.name if first_corp else 'None'}")

    # First when no match
    no_match = User.filter(F("name") == "Nobody").first()
    print(f"No match result: {no_match}")
    return first_corp, no_match, oldest


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    You've learned the fundamentals of SQLer Lite:

    | Operation | Method |
    |-----------|--------|
    | Create | `Model(...).save()` |
    | Read one | `Model.from_id(id)` |
    | Read all | `Model.all()` |
    | Update | Modify + `.save()` |
    | Delete | `.delete()` |
    | Query | `Model.filter(F(...)).all()` |
    | Filter ops | `F("field") > value`, `.contains()`, `.between()`, etc. |
    | Count | `.count()` |
    | Order | `.order_by("field", desc=True)` |
    | Limit | `.limit(n)`, `.first()` |

    **Key difference from Pydantic version:**
    - Use `@dataclass` decorator on your model classes
    - Inherit from `SQLerLiteModel` instead of `SQLerModel`
    - Use `__tablename__` instead of `_table`

    **Next up:** Tour 02 covers relationships between models!
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
