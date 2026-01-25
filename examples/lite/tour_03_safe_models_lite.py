# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo"]
# ///
"""SQLer Lite Tour: Safe Models - Works in Pyodide/WASM!"""

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
        # SQLer Lite Tour: Safe Models (Optimistic Locking)

        This notebook covers Safe Models - SQLer's solution for handling concurrent
        updates without data loss. Works in Pyodide/WASM!

        You'll learn:

        1. What optimistic locking is and why you need it
        2. Using `SQLerLiteSafeModel` for version tracking
        3. Handling `StaleVersionError` conflicts
        4. The `refresh()` pattern for conflict recovery

        Let's dive in!
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 1. The Problem: Lost Updates

        Imagine two users editing the same record simultaneously:

        1. User A reads `balance = 100`
        2. User B reads `balance = 100`
        3. User A sets `balance = 150` and saves
        4. User B sets `balance = 80` and saves (overwrites A's change!)

        User A's update is **lost**. This is the "lost update" problem.
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 2. The Solution: Optimistic Locking

        Safe models track a `_version` number that increments on every save.
        When saving, SQLer checks if the version matches what you read.
        If someone else modified the record, you get a `StaleVersionError`.

        Let's set up our database and create a safe model:
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

    import importlib

    _sqler = importlib.import_module("sqler")
    F = _sqler.F
    SQLerDB = _sqler.SQLerDB
    SQLerLiteSafeModel = _sqler.SQLerLiteSafeModel
    StaleVersionError = _sqler.StaleVersionError

    db = SQLerDB.in_memory()
    print("Database connected!")
    return F, SQLerDB, SQLerLiteSafeModel, StaleVersionError, dataclass, db


@app.cell
def _(SQLerLiteSafeModel, dataclass, db):
    @dataclass
    class Account(SQLerLiteSafeModel):
        __tablename__ = "accounts"
        owner: str
        balance: int

    Account.set_db(db)
    print("Account model registered!")
    print("Note: SQLerLiteSafeModel automatically tracks _version")
    return (Account,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. Version Tracking in Action

        Each save increments the `_version`. Let's see it work:
        """
    )
    return


@app.cell
def _(Account):
    # Create a new account
    acc = Account(owner="Alice", balance=100)
    print(f"Before save: _version = {acc._version}")

    acc.save()
    print(f"After first save: _version = {acc._version}")

    acc.balance = 150
    acc.save()
    print(f"After second save: _version = {acc._version}")

    acc.balance = 200
    acc.save()
    print(f"After third save: _version = {acc._version}")
    return (acc,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 4. Detecting Conflicts

        When two "users" have stale versions, the second save fails.
        Let's simulate this:
        """
    )
    return


@app.cell
def _(Account, StaleVersionError):
    # Create a fresh account
    original = Account(owner="Bob", balance=500)
    original.save()
    print(f"Created account: balance={original.balance}, version={original._version}")

    # Simulate two users loading the same record
    user_a = Account.from_id(original._id)
    user_b = Account.from_id(original._id)

    print(f"\nUser A loaded: balance={user_a.balance}, version={user_a._version}")
    print(f"User B loaded: balance={user_b.balance}, version={user_b._version}")

    # User A makes a change and saves successfully
    user_a.balance = 600
    user_a.save()
    print(f"\nUser A saved: balance={user_a.balance}, version={user_a._version}")

    # User B tries to save with stale version
    user_b.balance = 400
    try:
        user_b.save()
        print("User B saved successfully (unexpected!)")
    except StaleVersionError as _e:
        print("\nUser B got StaleVersionError!")
        print(f"  Message: {_e}")
    return original, user_a, user_b


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 5. Handling Conflicts: The Refresh Pattern

        When you get a `StaleVersionError`, the recommended pattern is:
        1. Call `.refresh()` to get the latest data
        2. Re-apply your business logic
        3. Try saving again

        Let's see this pattern:
        """
    )
    return


@app.cell
def _(Account, StaleVersionError):
    # Create account
    account = Account(owner="Charlie", balance=1000)
    account.save()

    # Two concurrent "sessions"
    session1 = Account.from_id(account._id)
    session2 = Account.from_id(account._id)

    # Session 1 adds 100
    session1.balance += 100
    session1.save()
    print(f"Session 1: Added 100, new balance = {session1.balance}")

    # Session 2 tries to add 50 (has stale data)
    session2.balance += 50
    try:
        session2.save()
    except StaleVersionError:
        print(f"\nSession 2: Conflict! My balance was {session2.balance - 50}")

        # Refresh and re-apply the logic
        session2.refresh()
        print(f"Session 2: After refresh, balance = {session2.balance}")

        session2.balance += 50  # Re-apply our change
        session2.save()
        print(f"Session 2: Now added 50, new balance = {session2.balance}")

    # Final result: both changes were applied!
    final_account = Account.from_id(account._id)
    print(f"\nFinal balance: {final_account.balance} (started at 1000, added 100 + 50)")
    return account, final_account, session1, session2


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. Checking Version Before Save

        You can check if your model is stale before attempting to save:
        """
    )
    return


@app.cell
def _(Account):
    # Create and load
    check_acc = Account(owner="Eve", balance=500)
    check_acc.save()
    loaded = Account.from_id(check_acc._id)

    # Someone else modifies it
    check_acc.balance = 600
    check_acc.save()

    # Check the version
    print(f"Loaded version: {loaded._version}")
    print(f"Current DB version: {Account.from_id(loaded._id)._version}")

    # You can compare before saving
    current = Account.from_id(loaded._id)
    if loaded._version != current._version:
        print("Warning: Record has been modified since you loaded it!")
    return check_acc, current, loaded


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. Why Use Safe Models?

        Use `SQLerLiteSafeModel` when:

        - Multiple users/processes might edit the same record
        - You need audit trails of changes
        - Data integrity is critical
        - You're building collaborative features

        Use regular `SQLerLiteModel` when:

        - Records are only edited by one user at a time
        - You don't need version tracking
        - Simplicity is more important than conflict detection
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## Summary

        Safe Models provide optimistic locking for concurrent access:

        | Feature | Description |
        |---------|-------------|
        | `SQLerLiteSafeModel` | Base class with `_version` tracking |
        | `StaleVersionError` | Raised when saving with outdated version |
        | `.refresh()` | Reload latest data from database |

        **Best Practices:**
        - Use safe models for data that might be edited concurrently
        - Catch `StaleVersionError` and implement retry logic
        - Keep the refresh-modify-save cycle short

        **Key difference from Pydantic version:**
        - Use `@dataclass` decorator on your model classes
        - Inherit from `SQLerLiteSafeModel` instead of `SQLerSafeModel`
        - Works identically otherwise!

        **Next up:** Tour 04 covers Transactions!
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
