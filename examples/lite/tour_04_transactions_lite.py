# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo"]
# ///
"""SQLer Lite Tour: Transactions - Works in Pyodide/WASM!"""

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
    mo.md(
        r"""
        # SQLer Lite Tour: Transactions

        This notebook covers database transactions in SQLer - how to group
        multiple operations into atomic units that either all succeed or all fail.
        Works in Pyodide/WASM!

        You'll learn:

        1. Why transactions matter
        2. Using the `db.transaction()` context manager
        3. Automatic rollback on errors
        4. Transaction-aware model saves

        Let's get started!
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 1. Why Transactions Matter

        Imagine transferring money between accounts:

        1. Deduct $100 from Account A
        2. Add $100 to Account B

        What if step 2 fails after step 1 succeeds? You've lost $100!

        Transactions ensure **atomicity**: either ALL operations succeed,
        or NONE of them do. If anything fails, everything is rolled back.
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 2. Setting Up
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

    import importlib
    from dataclasses import dataclass

    _sqler = importlib.import_module("sqler")
    F = _sqler.F
    SQLerDB = _sqler.SQLerDB
    SQLerLiteModel = _sqler.SQLerLiteModel

    db = SQLerDB.in_memory()
    print("Database connected!")
    return F, SQLerDB, SQLerLiteModel, dataclass, db


@app.cell
def _(SQLerLiteModel, dataclass, db):
    @dataclass
    class Account(SQLerLiteModel):
        __tablename__ = "accounts"
        name: str
        balance: int

    Account.set_db(db)

    # Create initial accounts
    _alice = Account(name="Alice", balance=1000)
    _alice.save()
    _bob = Account(name="Bob", balance=500)
    _bob.save()
    print(f"Created: Alice ({_alice.balance}), Bob ({_bob.balance})")
    return (Account,)


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. Basic Transaction Usage

        Use `db.transaction()` as a context manager. All operations inside
        the block are part of the transaction:
        """
    )
    return


@app.cell
def _(Account, db):
    # Successful transaction
    with db.transaction():
        # Transfer 200 from Alice to Bob
        _a = Account.from_id(1)
        _b = Account.from_id(2)

        _a.balance -= 200
        _a.save()

        _b.balance += 200
        _b.save()

    # Verify the transfer
    _alice_after = Account.from_id(1)
    _bob_after = Account.from_id(2)
    print(f"After transfer: Alice ({_alice_after.balance}), Bob ({_bob_after.balance})")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 4. Automatic Rollback on Error

        If an exception occurs inside a transaction, all changes are rolled back:
        """
    )
    return


@app.cell
def _(Account, db):
    # Check balances before
    _before_alice = Account.from_id(1)
    _before_bob = Account.from_id(2)
    print(f"Before: Alice ({_before_alice.balance}), Bob ({_before_bob.balance})")

    try:
        with db.transaction():
            _a = Account.from_id(1)
            _b = Account.from_id(2)

            # Deduct from Alice
            _a.balance -= 300
            _a.save()
            print(f"  Deducted from Alice: {_a.balance}")

            # Simulate an error before crediting Bob
            raise ValueError("Network error! Transaction failed!")

            # This line never executes
            _b.balance += 300
            _b.save()

    except ValueError as _e:
        print(f"  Error caught: {_e}")

    # Verify rollback - balances should be unchanged
    _after_alice = Account.from_id(1)
    _after_bob = Account.from_id(2)
    print(f"After: Alice ({_after_alice.balance}), Bob ({_after_bob.balance})")
    print("Transaction was rolled back - no money lost!")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 5. Transaction-Aware Saves

        `model.save()` respects active transactions. Saves are deferred until
        the transaction commits, enabling proper rollback:
        """
    )
    return


@app.cell
def _(Account, db):
    # Create a new account inside a failed transaction
    _initial_count = Account.count()
    print(f"Initial account count: {_initial_count}")

    try:
        with db.transaction():
            # Create new account
            _charlie = Account(name="Charlie", balance=750)
            _charlie.save()
            print("  Created Charlie inside transaction")

            raise RuntimeError("Oops! Abort everything!")

    except RuntimeError:
        print("  Transaction aborted!")

    # Charlie should NOT exist - the transaction was rolled back
    _final_count = Account.count()
    print(f"Final account count: {_final_count}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. Multiple Operations in a Transaction

        Batch multiple creates, updates, and deletes in one atomic operation:
        """
    )
    return


@app.cell
def _(Account, F, db):
    print("Before batch operation:")
    for _acc in Account.all():
        print(f"  {_acc.name}: {_acc.balance}")

    with db.transaction():
        # Create new accounts
        Account(name="Diana", balance=300).save()
        Account(name="Eve", balance=450).save()

        # Update existing
        _alice = Account.filter(F("name") == "Alice").first()
        _alice.balance += 100
        _alice.save()

        # All committed together

    print("\nAfter batch operation:")
    for _acc in Account.all():
        print(f"  {_acc.name}: {_acc.balance}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. Checking Transaction State

        You can check if a transaction is active:
        """
    )
    return


@app.cell
def _(db):
    print(f"Outside transaction - in_transaction: {db.adapter.in_transaction}")

    with db.transaction():
        print(f"Inside transaction - in_transaction: {db.adapter.in_transaction}")

    print(f"After transaction - in_transaction: {db.adapter.in_transaction}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## Summary

        Transactions ensure data integrity for multi-step operations:

        | Feature | Description |
        |---------|-------------|
        | `with db.transaction():` | Context manager for atomic operations |
        | Automatic rollback | On any exception, all changes are undone |
        | Transaction-aware saves | `model.save()` respects active transactions |
        | `db.adapter.in_transaction` | Check if transaction is active |

        **Best Practices:**
        - Use transactions for any multi-step data modifications
        - Keep transactions short to avoid blocking
        - Handle exceptions and let rollback happen automatically

        **Key difference from Pydantic version:**
        - Use `@dataclass` decorator on your model classes
        - Inherit from `SQLerLiteModel` instead of `SQLerModel`
        - Transaction behavior is identical!

        **Congratulations!** You've completed the SQLer Lite core tours!
        These notebooks work in Pyodide/WASM browser environments.
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
