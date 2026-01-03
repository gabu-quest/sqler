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
        # SQLer Tour: Transactions

        This notebook covers database transactions in SQLer - how to group
        multiple operations into atomic units that either all succeed or all fail.

        You'll learn:

        1. Why transactions matter
        2. Using the `db.transaction()` context manager
        3. Automatic rollback on errors
        4. Transaction-aware model saves
        5. Nested transactions (savepoints)

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
def _():
    from sqler import SQLerDB, SQLerModel
    from sqler.query import SQLerField as F

    db = SQLerDB.in_memory()
    print("Database connected!")
    return F, SQLerDB, SQLerModel, db


@app.cell
def _(SQLerModel, db):
    class Account(SQLerModel):
        _table = "accounts"
        name: str
        balance: int

    Account.set_db(db)

    # Create initial accounts
    _alice = Account(name="Alice", balance=1000).save()
    _bob = Account(name="Bob", balance=500).save()
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

        An important feature of SQLer: `model.save()` respects active transactions.
        Before this feature, saves would commit immediately, breaking rollback.

        Now saves are deferred until the transaction commits:
        """
    )
    return


@app.cell
def _(Account, db):
    # Create a new account inside a failed transaction
    _initial_count = Account.query().count()
    print(f"Initial account count: {_initial_count}")

    try:
        with db.transaction():
            # Create new account
            _charlie = Account(name="Charlie", balance=750)
            _charlie.save()
            print(f"  Created Charlie inside transaction")

            raise RuntimeError("Oops! Abort everything!")

    except RuntimeError:
        print("  Transaction aborted!")

    # Charlie should NOT exist - the transaction was rolled back
    _final_count = Account.query().count()
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
    for _acc in Account.query().all():
        print(f"  {_acc.name}: {_acc.balance}")

    with db.transaction():
        # Create new accounts
        Account(name="Diana", balance=300).save()
        Account(name="Eve", balance=450).save()

        # Update existing
        _alice = Account.query().filter(F("name") == "Alice").first()
        _alice.balance += 100
        _alice.save()

        # All committed together

    print("\nAfter batch operation:")
    for _acc in Account.query().all():
        print(f"  {_acc.name}: {_acc.balance}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 7. Nested Transactions (Savepoints)

        SQLer supports nested transactions using savepoints. Inner transactions
        can be rolled back independently:
        """
    )
    return


@app.cell
def _(Account, F, db):
    # Get starting balance
    _alice = Account.query().filter(F("name") == "Alice").first()
    _starting = _alice.balance
    print(f"Starting balance: {_starting}")

    with db.transaction():
        # Outer transaction
        _alice.balance += 50
        _alice.save()
        print(f"After outer +50: {_alice.balance}")

        try:
            with db.transaction():
                # Inner transaction (savepoint)
                _alice.refresh()
                _alice.balance += 100
                _alice.save()
                print(f"After inner +100: {_alice.balance}")

                # Inner transaction fails
                raise ValueError("Inner operation failed!")

        except ValueError:
            print("Inner transaction rolled back")

        # Outer transaction continues
        _alice.refresh()
        print(f"After inner rollback: {_alice.balance}")

        _alice.balance += 25
        _alice.save()

    # Final result
    _final = Account.query().filter(F("name") == "Alice").first()
    print(f"Final balance: {_final.balance}")
    print(f"Net change: +{_final.balance - _starting} (outer +50 and +25, inner +100 rolled back)")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 8. Checking Transaction State

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
        ## 9. Manual Commit and Rollback

        While the context manager handles this automatically, you can also
        manually control transactions through the adapter:
        """
    )
    return


@app.cell
def _(Account, F, db):
    # Manual transaction control (not recommended, but possible)
    db.adapter.begin_transaction()

    try:
        _alice = Account.query().filter(F("name") == "Alice").first()
        _alice.balance += 1000
        _alice.save()

        # Manually commit
        db.adapter.end_transaction(commit=True)
        print("Manually committed +1000 to Alice")

    except Exception:
        db.adapter.end_transaction(commit=False)
        print("Manually rolled back")

    _final = Account.query().filter(F("name") == "Alice").first()
    print(f"Alice's balance: {_final.balance}")
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
        | Nested transactions | Inner blocks use savepoints |
        | `db.adapter.in_transaction` | Check if transaction is active |

        **Best Practices:**
        - Use transactions for any multi-step data modifications
        - Keep transactions short to avoid blocking
        - Handle exceptions and let rollback happen automatically
        - Use nested transactions for partial rollback scenarios

        **Next up:** Tour 05 covers Mixins (timestamps, soft delete, hooks)!
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
