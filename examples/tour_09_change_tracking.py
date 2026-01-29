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
    # SQLer Tour: Change Tracking

    This notebook covers SQLer's change tracking features for monitoring
    field modifications and optimizing database writes.

    You'll learn:

    1. TrackedModel for dirty checking
    2. Detecting changed fields
    3. Viewing change history
    4. Reverting changes
    5. DiffMixin for comparing instances

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
    from sqler.tracking import DiffMixin, PartialUpdateMixin, TrackedModel

    db = SQLerDB.in_memory()
    print("Database connected!")
    print("\nChange tracking features:")
    print("  - is_dirty: Check if model has unsaved changes")
    print("  - changed_fields: Set of modified field names")
    print("  - get_changes(): Dict of (old, new) values")
    print("  - revert_changes(): Undo all changes")
    return DiffMixin, PartialUpdateMixin, SQLerModel, TrackedModel, db


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. TrackedModel Basics

    `TrackedModel` is a mixin that tracks changes to fields.
    Use it with `SQLerModel`:
    """)
    return


@app.cell
def _(SQLerModel, TrackedModel, db):
    class User(TrackedModel, SQLerModel):
        _table = "users"
        name: str
        email: str
        age: int

    User.set_db(db)

    # Create a user
    _user = User(name="Alice", email="alice@example.com", age=30).save()
    print(f"Created: {_user.name}, {_user.email}, age {_user.age}")
    print(f"is_dirty: {_user.is_dirty}")  # False - save() marks as clean
    return (User,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 3. Detecting Changes

    Modify fields and check what changed:
    """)
    return


@app.cell
def _(User):
    # Load user
    _user = User.from_id(1)
    print(f"Loaded: {_user.name}, is_dirty: {_user.is_dirty}")

    # Make changes
    _user.name = "Alice Smith"
    _user.age = 31

    print("\nAfter changes:")
    print(f"  is_dirty: {_user.is_dirty}")
    print(f"  changed_fields: {_user.changed_fields}")

    # Get details of changes (old, new) tuples
    _changes = _user.get_changes()
    print("\nChange details:")
    for _field, (_old, _new) in _changes.items():
        print(f"  {_field}: '{_old}' -> '{_new}'")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 4. Change History

    Track the full history of changes with timestamps:
    """)
    return


@app.cell
def _(User):
    import time

    _user = User.from_id(1)

    # Make several changes
    _user.age = 32
    time.sleep(0.01)
    _user.age = 33
    time.sleep(0.01)
    _user.name = "Alice Williams"

    print("Change history (with timestamps):")
    for _change in _user.get_change_history():
        print(f"  {_change.field}: {_change.old_value} -> {_change.new_value}")
        print(f"    at {_change.changed_at.strftime('%H:%M:%S.%f')}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 5. Reverting Changes

    Discard unsaved changes and restore original values:
    """)
    return


@app.cell
def _(User):
    _user = User.from_id(1)
    _original_name = _user.name
    print(f"Original name: {_original_name}")

    _user.name = "TEMPORARY NAME"
    print(f"Changed to: {_user.name}")
    print(f"is_dirty: {_user.is_dirty}")

    # Revert all changes
    _user.revert_changes()
    print("\nAfter revert_changes():")
    print(f"  name: {_user.name}")
    print(f"  is_dirty: {_user.is_dirty}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. Reverting Specific Fields

    Revert just one field while keeping other changes:
    """)
    return


@app.cell
def _(User):
    _user = User.from_id(1)
    print(f"Original: name={_user.name}, age={_user.age}")

    # Change both fields
    _user.name = "Bob"
    _user.age = 99
    print(f"Changed: name={_user.name}, age={_user.age}")
    print(f"changed_fields: {_user.changed_fields}")

    # Revert only the name
    _user.revert_field("name")
    print("\nAfter revert_field('name'):")
    print(f"  name: {_user.name}")
    print(f"  age: {_user.age} (still changed)")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. DiffMixin for Comparing Instances

    `DiffMixin` lets you compare two model instances:
    """)
    return


@app.cell
def _(DiffMixin, SQLerModel, db):
    class Product(DiffMixin, SQLerModel):
        _table = "products"
        name: str
        price: float
        stock: int

    Product.set_db(db)

    # Create two products to compare
    _prod1 = Product(name="Widget", price=29.99, stock=100).save()
    _prod2 = Product(name="Widget", price=24.99, stock=85).save()

    print(f"Product 1: name={_prod1.name}, price={_prod1.price}, stock={_prod1.stock}")
    print(f"Product 2: name={_prod2.name}, price={_prod2.price}, stock={_prod2.stock}")

    # Compare them
    _diff = _prod1.diff(_prod2)
    print("\nDifferences:")
    for _field, (_val1, _val2) in _diff.items():
        print(f"  {_field}: {_val1} vs {_val2}")
    return (Product,)


@app.cell
def _(Product):
    # Check equality
    _prod3 = Product(name="Gadget", price=49.99, stock=50).save()
    _prod4 = Product(name="Gadget", price=49.99, stock=50).save()

    print(f"prod3 equals prod4: {_prod3.is_equal(_prod4)}")

    # Change one
    _prod4.stock = 45
    _prod4.save()

    print(f"After changing stock: {_prod3.is_equal(_prod4)}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 8. PartialUpdateMixin

    Combine with TrackedModel for efficient partial updates:
    """)
    return


@app.cell
def _(PartialUpdateMixin, SQLerModel, TrackedModel, db):
    class Config(PartialUpdateMixin, TrackedModel, SQLerModel):
        _table = "configs"
        key: str
        value: str
        description: str = ""

    Config.set_db(db)

    _config = Config(key="theme", value="dark", description="UI theme setting").save()
    print(f"Created: {_config.key}={_config.value}")

    # Modify only one field
    _config.value = "light"
    print(f"\nChanged: value={_config.value}")
    print(f"changed_fields: {_config.changed_fields}")

    # save_partial() only updates changed columns
    _config.save_partial()
    print("Called save_partial() - only 'value' was sent to DB")

    _reloaded = Config.from_id(_config._id)
    print(f"Reloaded: value={_reloaded.value}, description={_reloaded.description}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    SQLer change tracking features:

    | Feature | Description |
    |---------|-------------|
    | `TrackedModel` | Mixin that tracks field changes |
    | `.is_dirty` | True if unsaved changes exist |
    | `.changed_fields` | Set of modified field names |
    | `.get_changes()` | Dict of {field: (old, new)} |
    | `.get_change_history()` | List of FieldChange with timestamps |
    | `.revert_changes()` | Discard all unsaved changes |
    | `.revert_field(name)` | Discard changes to one field |
    | `.mark_clean()` | Reset tracker (done by save()) |
    | `DiffMixin` | Compare two instances |
    | `.diff(other)` | Get differences between instances |
    | `.is_equal(other)` | Check if instances are equal |
    | `PartialUpdateMixin` | Efficient partial saves |
    | `.save_partial()` | Update only changed columns |

    **Benefits:**
    - Detect what changed before saving
    - Optimize DB writes (partial updates)
    - Easy rollback of unsaved changes
    - Compare model states

    **Next up:** Tour 10 covers Database Operations!
    """)
    return


@app.cell
def _(db):
    db.close()
    print("Database closed!")
    return


if __name__ == "__main__":
    app.run()
