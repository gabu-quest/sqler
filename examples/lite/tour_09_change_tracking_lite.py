# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo"]
# ///
"""SQLer Lite Tour: Change Tracking - Works in Pyodide/WASM!"""

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
    # SQLer Lite Tour: Change Tracking

    Welcome! This tour explores **built-in change tracking** for dataclass-based
    SQLer models. You'll learn how to detect modified fields, compare instances,
    and efficiently manage updates.

    **What you'll learn:**
    1. Built-in dirty tracking with `is_dirty()` and `get_dirty_fields()`
    2. Inspecting field changes with snapshots
    3. Comparing two instances manually
    4. Resetting tracking after save
    5. Manual revert using snapshots

    Let's explore!
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    > **Lite vs Pydantic**: This tour uses `SQLerLiteModel` (dataclasses) so it runs
    > in your browser via WebAssembly. With `SQLerModel` (Pydantic), you also get:
    > - `TrackedModel` with full change history and timestamped `FieldChange` objects
    > - `DiffMixin` for comparing two instances
    > - `PartialUpdateMixin` with `save_partial()` for efficient updates
    >
    > ```python
    > # Pydantic version — full tracking
    > from sqler.tracking import TrackedModel, DiffMixin, PartialUpdateMixin
    >
    > class User(TrackedModel, SQLerModel):
    >     _table = "users"
    >     name: str
    >
    > user.get_change_history()  # timestamped FieldChange objects
    > user.revert_changes()      # undo all unsaved changes
    > user.save_partial()        # only UPDATE changed columns
    > ```
    >
    > Run locally: `uv run marimo edit examples/tour_09_change_tracking.py`
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 1. Setting Up

    First, import SQLer and create a database. Lite models have built-in
    tracking with `is_dirty()` and `get_dirty_fields()` methods.
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
    print("\nBuilt-in Lite tracking methods:")
    print("  - is_dirty() — Check if model has unsaved changes")
    print("  - get_dirty_fields() — Set of modified field names")
    print("  - _snapshot — Dict of original values after load/save")
    return F, SQLerDB, SQLerLiteModel, dataclass, db


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. Built-in Dirty Tracking

    Lite models track changes automatically. After saving, `is_dirty()` returns
    `False` because the model is synchronized with the database.
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

    User.set_db(db)

    # Create a user
    _user = User(name="Alice", email="alice@example.com", age=30).save()
    print(f"Created: {_user.name}, {_user.email}, age {_user.age}")
    print(f"is_dirty(): {_user.is_dirty()}")  # False - save() marks as clean
    print(f"get_dirty_fields(): {_user.get_dirty_fields()}")  # Empty set
    return (User,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 3. Detecting Changes

    Modify fields and check what changed. `is_dirty()` returns `True` when
    unsaved changes exist, and `get_dirty_fields()` returns the modified field names.

    **IMPORTANT:** These are **methods**, not properties. Always call with `()`.
    """)
    return


@app.cell
def _(User):
    # Load user
    _user = User.from_id(1)
    print(f"Loaded: {_user.name}, is_dirty(): {_user.is_dirty()}")

    # Make changes
    _user.name = "Alice Smith"
    _user.age = 31

    print("\nAfter changes:")
    print(f"  is_dirty(): {_user.is_dirty()}")
    print(f"  get_dirty_fields(): {_user.get_dirty_fields()}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 4. Manual Change Details

    Want to see old vs new values? Compare the snapshot with current state:
    """)
    return


@app.cell
def _(User):
    _user = User.from_id(1)

    # Modify some fields
    _user.name = "Alice Williams"
    _user.age = 32

    # Compare snapshot (original) with current values
    _snapshot = _user._snapshot
    _current = _user.model_dump()

    print("Change details (old -> new):")
    for _field in _user.get_dirty_fields():
        _old_value = _snapshot.get(_field)
        _new_value = _current.get(_field)
        print(f"  {_field}: '{_old_value}' -> '{_new_value}'")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 5. Manual Diff Between Instances

    To compare two different instances, compare their `model_dump()` outputs:
    """)
    return


@app.cell
def _(SQLerLiteModel, dataclass, db):
    @dataclass
    class Product(SQLerLiteModel):
        __tablename__ = "products"

        name: str
        price: float
        stock: int

    Product.set_db(db)

    # Create two products
    prod1 = Product(name="Widget", price=29.99, stock=100).save()
    prod2 = Product(name="Widget", price=24.99, stock=85).save()

    print(f"Product 1: name={prod1.name}, price={prod1.price}, stock={prod1.stock}")
    print(f"Product 2: name={prod2.name}, price={prod2.price}, stock={prod2.stock}")

    # Manual diff using model_dump()
    dump1 = prod1.model_dump()
    dump2 = prod2.model_dump()
    diff = {k: (dump1[k], dump2[k]) for k in dump1 if dump1[k] != dump2[k]}

    print("\nDifferences:")
    for field, (val1, val2) in diff.items():
        print(f"  {field}: {val1} vs {val2}")
    return (Product,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. Saving Resets Tracking

    When you call `.save()`, the snapshot is updated and `is_dirty()` returns `False`:
    """)
    return


@app.cell
def _(User):
    _user = User.from_id(1)

    # Modify and check
    _user.age = 33
    print(f"Before save: is_dirty() = {_user.is_dirty()}")
    print(f"  dirty fields: {_user.get_dirty_fields()}")

    # Save and check again
    _user.save()
    print(f"\nAfter save: is_dirty() = {_user.is_dirty()}")
    print(f"  dirty fields: {_user.get_dirty_fields()}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. Manual Revert

    Lite models don't have a built-in `revert_changes()` method, but you can
    manually restore from the snapshot:
    """)
    return


@app.cell
def _(User):
    _user = User.from_id(1)
    _original_name = _user.name
    print(f"Original name: {_original_name}")

    # Modify
    _user.name = "TEMPORARY NAME"
    _user.age = 999
    print(f"\nChanged to: name={_user.name}, age={_user.age}")
    print(f"is_dirty(): {_user.is_dirty()}")

    # Manual revert — restore all fields from snapshot
    if _user._snapshot:
        for _field, _value in _user._snapshot.items():
            setattr(_user, _field, _value)

    print("\nAfter manual revert:")
    print(f"  name: {_user.name}")
    print(f"  age: {_user.age}")
    print(f"  is_dirty(): {_user.is_dirty()}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 8. Revert Single Field

    You can also revert just one field while keeping other changes:
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
    print(f"dirty fields: {_user.get_dirty_fields()}")

    # Revert only the name
    if _user._snapshot and "name" in _user._snapshot:
        _user.name = _user._snapshot["name"]

    print("\nAfter reverting 'name':")
    print(f"  name: {_user.name}")
    print(f"  age: {_user.age} (still changed)")
    print(f"  dirty fields: {_user.get_dirty_fields()}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    SQLer Lite models provide **built-in change tracking**:

    | Feature | Description |
    |---------|-------------|
    | `is_dirty()` | Returns `True` if unsaved changes exist |
    | `get_dirty_fields()` | Returns set of modified field names |
    | `_snapshot` | Dict of original values (after load/save) |
    | Manual diff | Compare `_snapshot` with `model_dump()` for change details |
    | Manual revert | Restore fields from `_snapshot` |
    | `.save()` resets | After save, `is_dirty()` returns `False` |

    **Lite tracking is simple but effective:**
    - No external dependencies (Pydantic-free)
    - Detect what changed before saving
    - Build custom logic around change detection
    - Perfect for lightweight applications

    **Pydantic version offers more:**
    - `TrackedModel` with full timestamped change history
    - `revert_changes()` and `revert_field()` built-in
    - `DiffMixin` for easy instance comparison
    - `PartialUpdateMixin` with `save_partial()` for efficient DB writes

    Run locally: `uv run marimo edit examples/tour_09_change_tracking.py`

    **Next up:** Tour 10 covers Database Operations!
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
