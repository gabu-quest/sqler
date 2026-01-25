# /// script
# requires-python = ">=3.12"
# dependencies = ["sqler", "marimo"]
# ///

import marimo

__generated_with = "0.19.4"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(r"""
    # SQLer Tour: Safe Models (Optimistic Locking)

    This notebook covers Safe Models - SQLer's solution for handling concurrent
    updates without data loss. You'll learn:

    1. What optimistic locking is and why you need it
    2. Using `SQLerSafeModel` for version tracking
    3. Handling `StaleVersionError` conflicts
    4. The `refresh()` pattern for conflict recovery
    5. Configurable intent rebasing for automatic conflict resolution

    Let's dive in!
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 1. The Problem: Lost Updates

    Imagine two users editing the same record simultaneously:

    1. User A reads `balance = 100`
    2. User B reads `balance = 100`
    3. User A sets `balance = 150` and saves
    4. User B sets `balance = 80` and saves (overwrites A's change!)

    User A's update is **lost**. This is the "lost update" problem.
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. The Solution: Optimistic Locking

    Safe models track a `_version` number that increments on every save.
    When saving, SQLer checks if the version matches what you read.
    If someone else modified the record, you get a `StaleVersionError`.

    Let's set up our database and create a safe model:
    """)
    return


@app.cell
def _():
    from sqler import SQLerDB, SQLerSafeModel, StaleVersionError
    from sqler.query import SQLerField as F

    db = SQLerDB.in_memory()
    print("Database connected!")
    return SQLerSafeModel, StaleVersionError, db


@app.cell
def _(SQLerSafeModel, db):
    class Account(SQLerSafeModel):
        _table = "accounts"
        owner: str
        balance: int

    Account.set_db(db)
    print("Account model registered!")
    print("Note: SQLerSafeModel automatically tracks _version")
    return (Account,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 3. Version Tracking in Action

    Each save increments the `_version`. Let's see it work:
    """)
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
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 4. Detecting Conflicts

    When two "users" have stale versions, the second save fails.
    Let's simulate this:
    """)
    return


@app.cell
def _(Account, StaleVersionError):
    # Create a fresh account
    original = Account(owner="Bob", balance=500).save()
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
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 5. Handling Conflicts: The Refresh Pattern

    When you get a `StaleVersionError`, the recommended pattern is:
    1. Call `.refresh()` to get the latest data
    2. Re-apply your business logic
    3. Try saving again

    Let's see this pattern:
    """)
    return


@app.cell
def _(Account, StaleVersionError):
    # Create account
    account = Account(owner="Charlie", balance=1000).save()

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
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. Intent Rebasing: Automatic Conflict Resolution

    For simple numeric operations (like incrementing counters), SQLer can
    automatically resolve conflicts using "intent rebasing".

    Instead of failing, it:
    1. Detects your intended change (e.g., "+50")
    2. Gets the current value
    3. Applies your delta to the current value

    Configure this with `RebaseConfig`:
    """)
    return


@app.cell
def _(SQLerSafeModel, db):
    from sqler.models.utils import PERMISSIVE_REBASE_CONFIG, RebaseConfig

    class Counter(SQLerSafeModel):
        _table = "counters"
        name: str
        value: int = 0

        # Allow rebasing numeric fields with deltas up to ±100
        _rebase_config = PERMISSIVE_REBASE_CONFIG

    Counter.set_db(db)
    print("Counter model with rebasing enabled!")
    return Counter, RebaseConfig


@app.cell
def _(Counter):
    # Create a counter
    _counter = Counter(name="page_views", value=0).save()

    # Two concurrent increments (simulating two users)
    _view1 = Counter.from_id(_counter._id)
    _view2 = Counter.from_id(_counter._id)

    print(f"Initial value: {_counter.value}")

    # First increment
    _view1.value += 1
    _view1.save()
    print(f"After view1 increment: {_view1.value}")

    # Second increment - would normally conflict, but rebasing auto-resolves it
    _view2.value += 1
    _view2.save()  # No try/except - rebasing just works!
    print(f"After view2 increment: {_view2.value} (rebased automatically)")

    # Verify both increments were applied
    _final = Counter.from_id(_counter._id)
    print(f"\nFinal value: {_final.value}")
    assert _final.value == 2, f"Expected 2, got {_final.value}"
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. Custom Rebase Configuration

    You can customize which fields allow rebasing and the maximum delta:
    """)
    return


@app.cell
def _(RebaseConfig, SQLerSafeModel, StaleVersionError, db):
    class BankAccount(SQLerSafeModel):
        _table = "bank_accounts"
        owner: str
        balance: int = 0
        overdraft_count: int = 0

        # Only allow rebasing 'overdraft_count' with small deltas
        _rebase_config = RebaseConfig(allowed_fields={"overdraft_count"}, max_delta=5)

    BankAccount.set_db(db)

    # Create account
    acct = BankAccount(owner="Dana", balance=1000, overdraft_count=0).save()

    # Two concurrent operations
    op1 = BankAccount.from_id(acct._id)
    op2 = BankAccount.from_id(acct._id)

    # op1 changes balance (not rebaseable)
    op1.balance = 900
    op1.save()
    print(f"op1: Changed balance to {op1.balance}")

    # op2 tries to change balance - will fail (not in allowed_fields)
    op2.balance = 800
    try:
        op2.save()
        print("op2: Saved (unexpected)")
    except StaleVersionError:
        print("op2: StaleVersionError for balance change (expected - not rebaseable)")

    # But overdraft_count changes ARE rebased
    op2.refresh()
    op2.overdraft_count += 1
    op2.save()
    print(f"op2: Incremented overdraft_count to {op2.overdraft_count}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 8. Disabling Rebasing

    Use `NO_REBASE_CONFIG` to disable automatic rebasing entirely:
    """)
    return


@app.cell
def _(SQLerSafeModel, StaleVersionError, db):
    from sqler.models.utils import NO_REBASE_CONFIG

    class StrictRecord(SQLerSafeModel):
        _table = "strict_records"
        data: str
        revision: int = 0

        # No rebasing - all conflicts raise errors
        _rebase_config = NO_REBASE_CONFIG

    StrictRecord.set_db(db)

    record = StrictRecord(data="original", revision=1).save()

    r1 = StrictRecord.from_id(record._id)
    r2 = StrictRecord.from_id(record._id)

    r1.revision = 2
    r1.save()

    r2.revision = 3
    try:
        r2.save()
    except StaleVersionError:
        print("StaleVersionError raised (as expected with NO_REBASE_CONFIG)")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 9. Checking Version Before Save

    You can check if your model is stale before attempting to save:
    """)
    return


@app.cell
def _(Account):
    # Create and load
    check_acc = Account(owner="Eve", balance=500).save()
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
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    Safe Models provide optimistic locking for concurrent access:

    | Feature | Description |
    |---------|-------------|
    | `SQLerSafeModel` | Base class with `_version` tracking |
    | `StaleVersionError` | Raised when saving with outdated version |
    | `.refresh()` | Reload latest data from database |
    | `_rebase_config` | Configure automatic conflict resolution |
    | `PERMISSIVE_REBASE_CONFIG` | Allow rebasing any numeric field |
    | `NO_REBASE_CONFIG` | Disable all rebasing |
    | `RebaseConfig(...)` | Custom rebase rules |

    **Best Practices:**
    - Use safe models for data that might be edited concurrently
    - Catch `StaleVersionError` and implement retry logic
    - Use rebasing for simple counters/metrics
    - Disable rebasing for critical financial data

    **Next up:** Tour 04 covers Transactions!
    """)
    return


@app.cell
def _(db):
    db.close()
    print("Database closed!")
    return


if __name__ == "__main__":
    app.run()
