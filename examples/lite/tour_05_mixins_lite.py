# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo"]
# ///
"""SQLer Lite Tour: Mixins (Manual Patterns) - Works in Pyodide/WASM!"""

import marimo

__generated_with = "0.18.4"
app = marimo.App(width="medium")


@app.cell
def _():
    # --- marimo scaffolding (please ignore) ---
    import marimo as mo

    return (mo,)


@app.cell
async def _():
    # --- WASM scaffolding (please ignore) ---
    # Loads sqlite3 + sqler in Pyodide/browser environments.
    # Not needed when running locally with `marimo edit`.
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
    # SQLer Lite Tour: Mixins (Manual Patterns)

    This notebook shows how common model patterns (timestamps, soft delete, hooks)
    work under the hood using pure dataclass patterns. These run in your browser
    via WebAssembly!

    **You'll learn:**

    1. **Timestamps Pattern** - Manual `created_at`/`updated_at` fields
    2. **Soft Delete Pattern** - Manual `deleted_at` + restore methods
    3. **Hooks Pattern** - Override `save()` for before/after logic
    4. **Combined Pattern** - All three together in one model
    5. **Querying Soft-Deleted** - Filter by `deleted_at`

    Let's explore!
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    > **Lite vs Pydantic**: This tour uses `SQLerLiteModel` (dataclasses) so it runs
    > in your browser via WebAssembly. With `SQLerModel` (Pydantic), you also get:
    > - Built-in mixins: `TimestampMixin`, `SoftDeleteMixin`, `HooksMixin`, `FullMixin`
    > - `AuditMixin` and `AuditLogMixin` for user tracking and change history
    >
    > ```python
    > # Pydantic version (requires local install)
    > from sqler import SQLerModel
    > from sqler.models import TimestampMixin, SoftDeleteMixin, FullMixin
    >
    > class Post(TimestampMixin, SQLerModel):
    >     _table = "posts"
    >     title: str
    > ```
    >
    > Run locally: `uv run marimo edit examples/tour_05_mixins.py`
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 1. Setting Up

    Import SQLer Lite and create an in-memory database. We'll also import
    `datetime` for timestamp handling.
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
    from datetime import datetime, timezone
    from typing import Optional

    _sqler = importlib.import_module("sqler")
    F = _sqler.F
    SQLerDB = _sqler.SQLerDB
    SQLerLiteModel = _sqler.SQLerLiteModel

    # Create an in-memory database for this tour
    db = SQLerDB.in_memory()
    print("Connected to in-memory database!")
    return (
        F,
        Optional,
        SQLerDB,
        SQLerLiteModel,
        dataclass,
        datetime,
        db,
        timezone,
    )


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. Timestamps Pattern

    Add `created_at` and `updated_at` fields to track when records are created
    and modified. Override `save()` to set them automatically using UTC timestamps.
    """)
    return


@app.cell
def _(Optional, SQLerLiteModel, dataclass, datetime, db, timezone):
    from typing import Self

    @dataclass
    class Post(SQLerLiteModel):
        __tablename__ = "posts"

        title: str
        content: str
        created_at: Optional[str] = None
        updated_at: Optional[str] = None

        def save(self) -> Self:
            """Override save to set timestamps automatically."""
            now = datetime.now(timezone.utc).isoformat()
            if self._id is None:
                self.created_at = now
            self.updated_at = now
            return super().save()

    Post.set_db(db)
    print("Post model with timestamps ready!")
    print("\nTimestamp pattern provides:")
    print("  - created_at: Set once on creation")
    print("  - updated_at: Updated on every save")
    return (Post, Self)


@app.cell
def _(Post):
    import time

    # Create a post
    post = Post(title="Hello World", content="My first post")
    post.save()

    print(f"Created post: '{post.title}'")
    print(f"  created_at: {post.created_at}")
    print(f"  updated_at: {post.updated_at}")

    # Wait a moment and update
    time.sleep(0.1)
    post.content = "Updated content!"
    post.save()

    print("\nAfter update:")
    print(f"  created_at: {post.created_at} (unchanged)")
    print(f"  updated_at: {post.updated_at} (updated)")
    return (post, time)


@app.cell
def _(mo):
    mo.md(r"""
    ## 3. Soft Delete Pattern

    Instead of permanently deleting records, mark them as deleted by setting
    a `deleted_at` timestamp. Add helper methods for soft delete, restore,
    and hard delete operations.
    """)
    return


@app.cell
def _(Optional, SQLerLiteModel, dataclass, datetime, db, timezone):
    @dataclass
    class Document(SQLerLiteModel):
        __tablename__ = "documents"

        name: str
        content: str
        deleted_at: Optional[str] = None

        @property
        def is_deleted(self) -> bool:
            """Check if this document is soft-deleted."""
            return self.deleted_at is not None

        def soft_delete(self):
            """Mark as deleted without removing from database."""
            self.deleted_at = datetime.now(timezone.utc).isoformat()
            self.save()

        def restore(self):
            """Restore a soft-deleted document."""
            self.deleted_at = None
            self.save()

        def hard_delete(self):
            """Permanently delete the document."""
            self.delete()

    Document.set_db(db)
    print("Document model with soft delete pattern ready!")
    print("\nSoft delete pattern provides:")
    print("  - deleted_at: Optional[str] field")
    print("  - is_deleted: property (True if deleted_at is set)")
    print("  - soft_delete(): Mark as deleted")
    print("  - restore(): Undelete")
    print("  - hard_delete(): Permanently delete")
    return (Document,)


@app.cell
def _(Document, F):
    # Create some documents
    doc1 = Document(name="report.pdf", content="Annual report")
    doc1.save()
    doc2 = Document(name="notes.txt", content="Meeting notes")
    doc2.save()
    doc3 = Document(name="draft.doc", content="Draft proposal")
    doc3.save()

    print("Created 3 documents")
    print(f"Total in database: {Document.query().count()}")

    # Soft delete one document
    doc2.soft_delete()
    print(f"\nSoft deleted '{doc2.name}'")
    print(f"  is_deleted: {doc2.is_deleted}")
    print(f"  deleted_at: {doc2.deleted_at}")

    # The document still exists in the database!
    print(f"\nTotal in database: {Document.query().count()}")

    # Filter to get only active documents
    active = Document.query().filter(F("deleted_at").is_null()).all()
    print(f"Active documents: {[d.name for d in active]}")

    # Filter to get only deleted documents
    deleted = Document.query().filter(F("deleted_at").is_not_null()).all()
    print(f"Deleted documents: {[d.name for d in deleted]}")
    return active, deleted, doc1, doc2, doc3


@app.cell
def _(Document, doc2):
    # Restore a soft-deleted document
    print(f"Before restore: is_deleted = {doc2.is_deleted}")

    doc2.restore()

    print(f"After restore: is_deleted = {doc2.is_deleted}")
    print(f"deleted_at: {doc2.deleted_at}")

    # Now hard delete (permanent)
    doc2.hard_delete()
    print(f"\nAfter hard_delete: Document count = {Document.query().count()}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 4. Hooks Pattern

    Override `save()` and `delete()` to add before/after logic inline.
    This lets you normalize data, validate, log operations, etc.
    """)
    return


@app.cell
def _(SQLerLiteModel, dataclass, db):
    @dataclass
    class User(SQLerLiteModel):
        __tablename__ = "users"

        name: str
        email: str

        def save(self):
            """Override save with before/after logic."""
            # Before save: normalize email
            original = self.email
            self.email = self.email.lower().strip()
            if original != self.email:
                print(f"  [before save] Normalized email: {original} -> {self.email}")

            # Actual save
            result = super().save()

            # After save: log
            print(f"  [after save] Saved user {self.name} (id={self._id})")

            return result

        def delete(self):
            """Override delete with before/after logic."""
            # Before delete: confirm
            print(f"  [before delete] About to delete: {self.name}")

            # Actual delete
            super().delete()

            # After delete: log
            print(f"  [after delete] Deleted user {self.name}")

    User.set_db(db)
    print("User model with hooks pattern ready!")
    print("\nHooks pattern provides:")
    print("  - Override save() for before/after save logic")
    print("  - Override delete() for before/after delete logic")
    return (User,)


@app.cell
def _(User):
    print("Creating user with messy email...")
    user = User(name="Alice", email="  ALICE@Example.COM  ")
    user.save()  # Hooks are called automatically!

    print(f"\nFinal email: {user.email}")
    return (user,)


@app.cell
def _(user):
    print("\nDeleting user...")
    user.delete()  # Delete hooks are called automatically!
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 5. Aborting Operations with Hooks

    You can also prevent operations by raising an exception in the override.
    This is useful for locked records or validation failures.
    """)
    return


@app.cell
def _(SQLerLiteModel, dataclass, db):
    @dataclass
    class ProtectedRecord(SQLerLiteModel):
        __tablename__ = "protected_records"

        data: str
        locked: bool = False

        def delete(self):
            """Prevent deletion of locked records."""
            if self.locked:
                print("  [before delete] BLOCKED: Record is locked!")
                raise RuntimeError("Cannot delete locked record")
            super().delete()

    ProtectedRecord.set_db(db)

    # Create a locked record
    record = ProtectedRecord(data="Important data", locked=True)
    record.save()
    print(f"Created locked record: {record.data}")

    # Try to delete it
    print("\nAttempting to delete locked record...")
    try:
        record.delete()  # This will be blocked
        print("Deleted (unexpected!)")
    except RuntimeError as exc:
        e = exc
        print(f"Delete blocked: {e}")

    # Verify it still exists
    still_exists = ProtectedRecord.from_id(record._id)
    print(f"Record still exists: {still_exists is not None}")
    return (ProtectedRecord, e, record, still_exists)


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. Combined Pattern: All Three Together

    Combine timestamps, soft delete, and hooks in a single model for
    maximum functionality:
    """)
    return


@app.cell
def _(Optional, SQLerLiteModel, dataclass, datetime, db, timezone):
    @dataclass
    class Task(SQLerLiteModel):
        __tablename__ = "tasks"

        title: str
        description: str = ""
        priority: int = 0
        created_at: Optional[str] = None
        updated_at: Optional[str] = None
        deleted_at: Optional[str] = None

        @property
        def is_deleted(self) -> bool:
            """Check if this task is soft-deleted."""
            return self.deleted_at is not None

        def save(self):
            """Set timestamps and log before/after save."""
            # Before save: set timestamps
            now = datetime.now(timezone.utc).isoformat()
            if self._id is None:
                self.created_at = now
            self.updated_at = now
            print(f"  [before save] Task: {self.title}")

            # Actual save
            result = super().save()

            # After save: log
            print(f"  [after save] Saved with id={self._id}")

            return result

        def soft_delete(self):
            """Mark as deleted without removing from database."""
            self.deleted_at = datetime.now(timezone.utc).isoformat()
            self.save()

        def restore(self):
            """Restore a soft-deleted task."""
            self.deleted_at = None
            self.save()

        def hard_delete(self):
            """Permanently delete the task."""
            self.delete()

    Task.set_db(db)
    print("Task model with combined pattern ready!")
    print("\nCombined pattern provides:")
    print("  - Timestamps: created_at, updated_at")
    print("  - Soft delete: soft_delete(), restore(), hard_delete()")
    print("  - Hooks: before/after logic in save()")
    return (Task,)


@app.cell
def _(Task):
    # Create a task
    task = Task(title="Learn SQLer", description="Complete all tutorials", priority=1)
    task.save()

    print(f"\nTask: {task.title}")
    print(f"  priority: {task.priority}")
    print(f"  created_at: {task.created_at}")
    print(f"  is_deleted: {task.is_deleted}")

    # Soft delete the task
    print("\nSoft deleting task...")
    task.soft_delete()
    print(f"  is_deleted: {task.is_deleted}")
    print(f"  deleted_at: {task.deleted_at}")
    return (task,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. Querying Soft-Deleted Records

    Common patterns for querying soft-deletable models:
    """)
    return


@app.cell
def _(F, Task):
    # Create more tasks for querying
    Task(title="Task A", priority=1).save()
    Task(title="Task B", priority=2).save()
    task_c = Task(title="Task C", priority=3)
    task_c.save()
    task_c.soft_delete()

    print("Query patterns for soft-deletable models:\n")

    # All records (including deleted)
    all_tasks = Task.query().all()
    print(f"All tasks: {len(all_tasks)}")

    # Only active (not deleted)
    active_tasks = Task.query().filter(F("deleted_at").is_null()).all()
    print(f"Active tasks: {[t.title for t in active_tasks]}")

    # Only deleted
    deleted_tasks = Task.query().filter(F("deleted_at").is_not_null()).all()
    print(f"Deleted tasks: {[t.title for t in deleted_tasks]}")
    return active_tasks, all_tasks, deleted_tasks, task_c


@app.cell
def _(mo):
    mo.md(r"""
    ## 8. About AuditMixin and AuditLogMixin

    The Pydantic version of SQLer includes `AuditMixin` (tracks who created/updated
    records) and `AuditLogMixin` (logs full change history). These mixins rely on
    Pydantic's model validation and aren't available in the Lite version.

    For user tracking in Lite models, you can manually add fields like:

    ```python
    @dataclass
    class Article(SQLerLiteModel):
        __tablename__ = "articles"
        title: str
        created_by: Optional[str] = None
        updated_by: Optional[str] = None

        def save(self):
            current_user = get_current_user()  # Your auth logic
            if self._id is None:
                self.created_by = current_user
            self.updated_by = current_user
            return super().save()
    ```

    For full change history, consider storing snapshots in a separate audit table.
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    Manual patterns for common model functionality:

    | Pattern | Implementation |
    |---------|----------------|
    | **Timestamps** | `created_at`/`updated_at` fields + override `save()` |
    | **Soft Delete** | `deleted_at` field + `soft_delete()`/`restore()` methods |
    | **Hooks** | Override `save()`/`delete()` with before/after logic |
    | **Combined** | All three together in one model |

    **With Pydantic's SQLerModel, you get:**
    - `TimestampMixin`, `SoftDeleteMixin`, `HooksMixin` as one-line imports
    - `FullMixin` combining all three
    - `AuditMixin` for user tracking
    - `AuditLogMixin` for change history

    **Key advantages of Pydantic mixins:**
    - Less boilerplate (1 line vs 10+ lines)
    - Consistent implementation across models
    - Type safety with datetime objects (not strings)
    - Advanced features like audit logs

    Run locally: `uv run marimo edit examples/tour_05_mixins.py`
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
