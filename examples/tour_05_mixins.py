# /// script
# requires-python = ">=3.12"
# dependencies = ["sqler", "marimo"]
# ///

import marimo

__generated_with = "0.19.4"
app = marimo.App(width="medium")


@app.cell
def _():
    # --- marimo scaffolding (please ignore) ---
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(r"""
    # SQLer Tour: Mixins

    This notebook covers SQLer's built-in mixins that add common functionality
    to your models without writing boilerplate code.

    You'll learn:

    1. `TimestampMixin` - Automatic created_at/updated_at fields
    2. `SoftDeleteMixin` - Soft delete instead of permanent deletion
    3. `HooksMixin` - Lifecycle hooks (before/after save/delete)
    4. `FullMixin` - All mixins combined

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
    from datetime import datetime, timezone

    from sqler import SQLerDB, SQLerModel
    from sqler.models import FullMixin, HooksMixin, SoftDeleteMixin, TimestampMixin
    from sqler.models.mixins import AuditLogMixin, AuditMixin
    from sqler.query import SQLerField as F

    db = SQLerDB.in_memory()
    print("Database connected!")
    print("\nAvailable mixins:")
    print("  - TimestampMixin: created_at, updated_at")
    print("  - SoftDeleteMixin: soft delete with deleted_at")
    print("  - HooksMixin: before_save, after_save, before_delete, after_delete")
    print("  - FullMixin: all of the above combined")
    print("  - AuditMixin: track created_by, updated_by")
    print("  - AuditLogMixin: full change history logging")
    return (
        AuditLogMixin,
        AuditMixin,
        F,
        FullMixin,
        HooksMixin,
        SQLerModel,
        SoftDeleteMixin,
        TimestampMixin,
        datetime,
        db,
        timezone,
    )


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. TimestampMixin

    Adds `created_at` and `updated_at` fields to track when records are
    created and modified. You need to set these in your `save()` override:
    """)
    return


@app.cell
def _(SQLerModel, TimestampMixin, datetime, db, timezone):
    from typing import Self

    class Post(TimestampMixin, SQLerModel):
        _table = "posts"
        title: str
        content: str

        def save(self, *, db=None) -> Self:
            """Override save to set timestamps automatically."""
            now = datetime.now(timezone.utc)
            if self._id is None:
                self.created_at = now
            self.updated_at = now
            return super().save(db=db)

    print("Post model with TimestampMixin ready!")
    return (Post,)


@app.cell
def _(Post, db):
    import time

    # Create a post
    post = Post(title="Hello World", content="My first post")
    post.save(db=db)

    print(f"Created post: '{post.title}'")
    print(f"  created_at: {post.created_at}")
    print(f"  updated_at: {post.updated_at}")

    # Wait a moment and update
    time.sleep(0.1)
    post.content = "Updated content!"
    post.save(db=db)

    print("\nAfter update:")
    print(f"  created_at: {post.created_at} (unchanged)")
    print(f"  updated_at: {post.updated_at} (updated)")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 3. SoftDeleteMixin

    Instead of permanently deleting records, soft delete marks them as deleted
    by setting a `deleted_at` timestamp. The data remains in the database
    for audit trails or potential recovery.
    """)
    return


@app.cell
def _(SQLerModel, SoftDeleteMixin):
    class Document(SoftDeleteMixin, SQLerModel):
        _table = "documents"
        name: str
        content: str

    print("Document model with SoftDeleteMixin ready!")
    print("\nSoftDeleteMixin provides:")
    print("  - deleted_at: Optional[datetime] field")
    print("  - is_deleted: property (True if deleted_at is set)")
    print("  - soft_delete(): Mark as deleted")
    print("  - restore(): Undelete")
    print("  - hard_delete(): Permanently delete")
    return (Document,)


@app.cell
def _(Document, F, db):
    # Create some documents
    doc1 = Document(name="report.pdf", content="Annual report").save(db=db)
    doc2 = Document(name="notes.txt", content="Meeting notes").save(db=db)
    doc3 = Document(name="draft.doc", content="Draft proposal").save(db=db)

    print("Created 3 documents")
    print(f"Total in database: {Document.using(db).count()}")

    # Soft delete one document
    doc2.soft_delete(db=db)
    print(f"\nSoft deleted '{doc2.name}'")
    print(f"  is_deleted: {doc2.is_deleted}")
    print(f"  deleted_at: {doc2.deleted_at}")

    # The document still exists in the database!
    print(f"\nTotal in database: {Document.using(db).count()}")

    # Filter to get only active documents
    active = Document.using(db).filter(F("deleted_at").is_null()).all()
    print(f"Active documents: {[d.name for d in active]}")

    # Filter to get only deleted documents
    deleted = Document.using(db).filter(F("deleted_at").is_not_null()).all()
    print(f"Deleted documents: {[d.name for d in deleted]}")
    return (doc2,)


@app.cell
def _(Document, db, doc2):
    # Restore a soft-deleted document
    print(f"Before restore: is_deleted = {doc2.is_deleted}")

    doc2.restore(db=db)

    print(f"After restore: is_deleted = {doc2.is_deleted}")
    print(f"deleted_at: {doc2.deleted_at}")

    # Now hard delete (permanent)
    doc2.hard_delete(db=db)
    print(f"\nAfter hard_delete: Document count = {Document.using(db).count()}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 4. HooksMixin

    Adds lifecycle hooks that are **automatically called** by `save()` and `delete()`:
    - `before_save()` - Called before saving, return False to abort
    - `after_save()` - Called after successful save
    - `before_delete()` - Called before deleting, return False to abort
    - `after_delete()` - Called after successful delete
    """)
    return


@app.cell
def _(HooksMixin, SQLerModel):
    class User(HooksMixin, SQLerModel):
        _table = "users"
        name: str
        email: str

        def before_save(self) -> bool:
            """Normalize email before saving."""
            original = self.email
            self.email = self.email.lower().strip()
            if original != self.email:
                print(f"  [before_save] Normalized email: {original} -> {self.email}")
            return True  # Continue with save

        def after_save(self) -> None:
            """Log after successful save."""
            print(f"  [after_save] Saved user {self.name} (id={self._id})")

        def before_delete(self) -> bool:
            """Confirm deletion."""
            print(f"  [before_delete] About to delete: {self.name}")
            return True  # Continue with delete

        def after_delete(self) -> None:
            """Log after deletion."""
            print(f"  [after_delete] Deleted user {self.name}")

    print("User model with HooksMixin ready!")
    return (User,)


@app.cell
def _(User, db):
    print("Creating user with messy email...")
    user = User(name="Alice", email="  ALICE@Example.COM  ")
    user.save(db=db)  # Hooks are called automatically!

    print(f"\nFinal email: {user.email}")
    return (user,)


@app.cell
def _(db, user):
    print("\nDeleting user...")
    user.delete(db=db)  # Delete hooks are called automatically!
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 5. Aborting Operations with Hooks

    Return `False` from `before_save` or `before_delete` to abort the operation:
    """)
    return


@app.cell
def _(HooksMixin, SQLerModel, db):
    class ProtectedRecord(HooksMixin, SQLerModel):
        _table = "protected_records"
        data: str
        locked: bool = False

        def before_delete(self) -> bool:
            """Prevent deletion of locked records."""
            if self.locked:
                print("  [before_delete] BLOCKED: Record is locked!")
                return False  # Abort the delete
            return True


    # Create a locked record
    _record = ProtectedRecord(data="Important data", locked=True).save(db=db)
    print(f"Created locked record: {_record.data}")

    # Try to delete it
    print("\nAttempting to delete locked record...")
    try:
        _record.delete(db=db)  # This will be blocked
        print("Deleted (unexpected!)")
    except RuntimeError as _e:
        print(f"Delete blocked: {_e}")

    # Verify it still exists
    _still_exists = ProtectedRecord.from_id(_record._id, db=db)
    print(f"Record still exists: {_still_exists is not None}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. FullMixin: All Combined

    `FullMixin` combines all three mixins for maximum functionality:
    """)
    return


@app.cell
def _(FullMixin, SQLerModel, datetime, timezone):

    class Task(FullMixin, SQLerModel):
        _table = "tasks"
        title: str
        description: str = ""
        priority: int = 0

        def before_save(self) -> bool:
            """Set timestamps in before_save hook."""
            now = datetime.now(timezone.utc)
            if self._id is None:
                self.created_at = now
            self.updated_at = now
            print(f"  [before_save] Task: {self.title}")
            return True

        def after_save(self) -> None:
            print(f"  [after_save] Saved with id={self._id}")

    print("Task model with FullMixin ready!")
    print("\nFullMixin provides:")
    print("  - TimestampMixin: created_at, updated_at")
    print("  - SoftDeleteMixin: soft_delete(), restore(), hard_delete()")
    print("  - HooksMixin: before/after save/delete hooks")
    return (Task,)


@app.cell
def _(Task, db):
    # Create a task
    task = Task(title="Learn SQLer", description="Complete all tutorials", priority=1)
    task.save(db=db)

    print(f"\nTask: {task.title}")
    print(f"  priority: {task.priority}")
    print(f"  created_at: {task.created_at}")
    print(f"  is_deleted: {task.is_deleted}")

    # Soft delete the task
    print("\nSoft deleting task...")
    task.soft_delete(db=db)
    print(f"  is_deleted: {task.is_deleted}")
    print(f"  deleted_at: {task.deleted_at}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. Querying with SoftDeleteMixin

    Common patterns for querying soft-deletable models:
    """)
    return


@app.cell
def _(F, Task, db):
    # Create more tasks for querying
    Task(title="Task A", priority=1).save(db=db)
    Task(title="Task B", priority=2).save(db=db)
    task_c = Task(title="Task C", priority=3).save(db=db)
    task_c.soft_delete(db=db)

    print("Query patterns for soft-deletable models:\n")

    # All records (including deleted)
    all_tasks = Task.using(db).all()
    print(f"All tasks: {len(all_tasks)}")

    # Only active (not deleted)
    active_tasks = Task.using(db).filter(F("deleted_at").is_null()).all()
    print(f"Active tasks: {[t.title for t in active_tasks]}")

    # Only deleted
    deleted_tasks = Task.using(db).filter(F("deleted_at").is_not_null()).all()
    print(f"Deleted tasks: {[t.title for t in deleted_tasks]}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 8. AuditMixin: Track Who Changed What

    `AuditMixin` tracks which user created/updated records. Set the current
    user via `AuditMixin.set_current_user()`:
    """)
    return


@app.cell
def _(AuditMixin, SQLerModel, db):
    class Article(AuditMixin, SQLerModel):
        _table = "articles"
        title: str
        content: str


    # Set the current user (typically done in middleware)
    AuditMixin.set_current_user("alice@example.com")

    article = Article(title="Hello World", content="First post!").save(db=db)
    print(f"Created by: {article.created_by}")
    print(f"Updated by: {article.updated_by}")
    print(f"Created at: {article.created_at}")

    # Different user updates the article
    AuditMixin.set_current_user("bob@example.com")
    article.content = "Updated content"
    article.save(db=db)

    print("\nAfter update by Bob:")
    print(f"  Created by: {article.created_by} (unchanged)")
    print(f"  Updated by: {article.updated_by}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 9. AuditLogMixin: Full Change History

    `AuditLogMixin` logs every change to a separate audit table, creating
    a complete history of all modifications:
    """)
    return


@app.cell
def _(AuditLogMixin, AuditMixin, SQLerModel, db):
    class Product(AuditLogMixin, SQLerModel):
        _table = "products"
        name: str
        price: float
        in_stock: bool = True


    # Set user for audit log
    AuditMixin.set_current_user("admin@shop.com")

    # Create a product
    prod = Product(name="Widget", price=29.99).save(db=db)
    print(f"Created: {prod.name} @ ${prod.price}")

    # Make some changes
    prod.price = 24.99
    prod.save(db=db)
    print(f"Updated price to ${prod.price}")

    prod.in_stock = False
    prod.save(db=db)
    print("Marked out of stock")

    # View the audit log
    print("\n--- Audit Log ---")
    logs = prod.get_audit_log(db=db)
    for log in logs:
        print(f"{log['action']} at {log['timestamp'][:19]} by {log['user']}")
        if log["changes"]:
            for field, change in log["changes"].items():
                print(f"  {field}: {change['old']} -> {change['new']}")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    SQLer mixins add common functionality without boilerplate:

    | Mixin | Features |
    |-------|----------|
    | `TimestampMixin` | `created_at`, `updated_at` fields |
    | `SoftDeleteMixin` | `deleted_at`, `is_deleted`, `soft_delete()`, `restore()`, `hard_delete()` |
    | `HooksMixin` | `before_save()`, `after_save()`, `before_delete()`, `after_delete()` |
    | `FullMixin` | Timestamps + SoftDelete + Hooks combined |
    | `AuditMixin` | `created_by`, `updated_by` user tracking |
    | `AuditLogMixin` | Full change history with `get_audit_log()` |

    **Usage Pattern:**
    ```python
    class MyModel(SomeMixin, SQLerModel):
        _table = "my_table"
        # your fields...
    ```

    **Important:** Mixins come BEFORE `SQLerModel` in the inheritance list!

    **Next up:** Tour 06 covers Advanced Features (bulk ops, integrity policies)!
    """)
    return


@app.cell
def _(db):
    db.close()
    print("Database closed!")
    return


if __name__ == "__main__":
    app.run()
