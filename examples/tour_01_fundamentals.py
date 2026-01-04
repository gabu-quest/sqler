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
    # SQLer Tour: Fundamentals

    Welcome to SQLer! This interactive notebook will teach you the fundamentals
    of this lightweight, JSON-first micro-ORM for SQLite.

    **What you'll learn:**
    1. Creating and connecting to a database
    2. Defining models with Pydantic-style fields
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

    First, we import SQLer and create an in-memory database. SQLer uses SQLite
    under the hood, storing your model data as JSON documents.
    """)
    return


@app.cell
def _():
    from sqler import SQLerDB, SQLerModel
    from sqler.query import SQLerField as F

    # Create an in-memory database for this tour
    # (You can also use SQLerDB("/path/to/file.db") for persistent storage)
    db = SQLerDB.in_memory()
    print("Connected to in-memory database!")
    return F, SQLerModel, db


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. Defining Models

    Models in SQLer inherit from `SQLerModel` and use Pydantic-style field definitions.
    Each model automatically gets:
    - An `_id` field (auto-generated integer)
    - A `_table` class variable for the table name
    - JSON serialization/deserialization

    Let's create a simple `User` model:
    """)
    return


@app.cell
def _(SQLerModel, db):
    class User(SQLerModel):
        _table = "users"

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
    - `Model.query()` - Start a query builder chain
    - `Model.query().all()` - Fetch all records
    """)
    return


@app.cell
def _(User, alice):
    # Fetch by ID
    fetched_alice = User.from_id(alice._id)
    print(f"Fetched: {fetched_alice.name}, {fetched_alice.email}")

    # Fetch all users
    all_users = User.query().all()
    print(f"\nAll users ({len(all_users)}):")
    for u in all_users:
        print(f"  - {u.name} (age: {u.age}, active: {u.is_active})")
    return


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
    return


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
    remaining = User.query().all()
    print(f"Remaining users: {[u.name for u in remaining]}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. Querying with the Fluent API

    The real power of SQLer is its fluent query builder. Start with `Model.query()`
    and chain methods to build complex queries.

    ### Basic Filtering

    Use `.filter()` with `F()` field expressions for conditions:
    """)
    return


@app.cell
def _(F, User):
    # Add more users for querying examples
    User(name="Diana", email="diana@example.com", age=28).save()
    User(name="Eve", email="eve@corp.com", age=32).save()
    User(name="Frank", email="frank@corp.com", age=28, is_active=False).save()

    # Filter by exact match
    active_users = User.query().filter(F("is_active") == True).all()
    print(f"Active users: {[u.name for u in active_users]}")

    # Filter by age
    users_28 = User.query().filter(F("age") == 28).all()
    print(f"Users aged 28: {[u.name for u in users_28]}")
    return


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
    older_than_28 = User.query().filter(F("age") > 28).all()
    print(f"Older than 28: {[u.name for u in older_than_28]}")

    # Less than or equal
    young_or_28 = User.query().filter(F("age") <= 28).all()
    print(f"28 or younger: {[u.name for u in young_or_28]}")

    # String contains
    corp_users = User.query().filter(F("email").contains("corp")).all()
    print(f"Corporate emails: {[u.name for u in corp_users]}")
    return


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
    age_range = User.query().filter(F("age").between(27, 30)).all()
    print(f"Age 27-30: {[u.name for u in age_range]}")

    # Starts with
    a_names = User.query().filter(F("name").startswith("A")).all()
    print(f"Names starting with A: {[u.name for u in a_names]}")

    # In list
    specific_ages = User.query().filter(F("age").in_list([28, 32])).all()
    print(f"Age 28 or 32: {[u.name for u in specific_ages]}")

    # Not equal
    not_alice = User.query().filter(F("name") != "Alice").all()
    print(f"Not Alice: {[u.name for u in not_alice]}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ### Combining Conditions

    Chain `.filter()` calls to combine conditions with AND logic.
    Use `.exclude()` to negate conditions:
    """)
    return


@app.cell
def _(F, User):
    # Multiple filters (AND)
    active_and_young = User.query().filter(F("is_active") == True).filter(F("age") < 30).all()
    print(f"Active AND under 30: {[u.name for u in active_and_young]}")

    # Exclude
    not_corp = User.query().exclude(F("email").contains("corp")).all()
    print(f"Non-corporate emails: {[u.name for u in not_corp]}")
    return


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
    return


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
    total = User.query().count()
    print(f"Total users: {total}")

    # Count with filter
    active_count = User.query().filter(F("is_active") == True).count()
    print(f"Active users: {active_count}")

    # Exists check
    has_alice = User.query().filter(F("name") == "Alice").exists()
    print(f"Alice exists: {has_alice}")

    # Sum, Avg, Min, Max
    total_age = User.query().sum("age")
    avg_age = User.query().avg("age")
    min_age = User.query().min("age")
    max_age = User.query().max("age")
    print(f"Age stats - Sum: {total_age}, Avg: {avg_age:.1f}, Min: {min_age}, Max: {max_age}")
    return


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
    first_corp = User.query().filter(F("email").contains("corp")).first()
    print(f"First corp user: {first_corp.name if first_corp else 'None'}")

    # First when no match
    no_match = User.query().filter(F("name") == "Nobody").first()
    print(f"No match result: {no_match}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 10. Pagination

    For large datasets, use `.paginate()` to get results in pages.
    Note: pagination returns dictionaries, not model instances.
    """)
    return


@app.cell
def _(User):
    # Get page 1 with 2 items per page
    page1 = User.query().order_by("name").paginate(page=1, per_page=2)
    print(f"Page 1 items: {[item['name'] for item in page1.items]}")
    print(f"  Total: {page1.total}, Pages: {page1.total_pages}, Has next: {page1.has_next}")

    # Get page 2
    page2 = User.query().order_by("name").paginate(page=2, per_page=2)
    print(f"Page 2 items: {[item['name'] for item in page2.items]}")
    print(f"  Has prev: {page2.has_prev}, Has next: {page2.has_next}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 11. Distinct Values

    Get unique values for a field:
    """)
    return


@app.cell
def _(User):
    # Get distinct ages
    distinct_ages = User.query().distinct_values("age")
    print(f"Distinct ages: {sorted(distinct_ages)}")

    # Get distinct active statuses
    distinct_active = User.query().distinct_values("is_active")
    print(f"Distinct is_active values: {distinct_active}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    You've learned the fundamentals of SQLer:

    | Operation | Method |
    |-----------|--------|
    | Create | `Model(...).save()` |
    | Read one | `Model.from_id(id)` |
    | Read all | `Model.query().all()` |
    | Update | Modify + `.save()` |
    | Delete | `.delete()` |
    | Query | `Model.query().filter(F(...)).all()` |
    | Filter ops | `F("field") > value`, `.contains()`, `.between()`, etc. |
    | Aggregate | `.count()`, `.sum()`, `.avg()`, `.min()`, `.max()` |
    | Order | `.order_by("field", desc=True)` |
    | Limit | `.limit(n)`, `.first()` |
    | Paginate | `.paginate(page, per_page)` |
    | Distinct | `.distinct_values("field")` |

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
