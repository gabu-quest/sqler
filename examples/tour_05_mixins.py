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
        # SQLer Tour: Mixins

        This notebook covers SQLer's built-in mixins that add common functionality
        to your models without writing boilerplate code.

        You'll learn:

        1. `TimestampMixin` - Automatic created_at/updated_at fields
        2. `SoftDeleteMixin` - Soft delete instead of permanent deletion
        3. `HooksMixin` - Lifecycle hooks (before/after save/delete)
        4. `FullMixin` - All mixins combined

        Let's explore!
        """
    )
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 1. Setting Up
        """
    )
    return


@app.cell
def _():
    from datetime import datetime, timezone
    from sqler import SQLerDB, SQLerModel
    from sqler.models import TimestampMixin, SoftDeleteMixin, HooksMixin, FullMixin
    from sqler.query import SQLerField as F

    db = SQLerDB.in_memory()
    print("Database connected!")
    print("\nAvailable mixins:")
    print("  - TimestampMixin: created_at, updated_at")
    print("  - SoftDeleteMixin: soft delete with deleted_at")
    print("  - HooksMixin: before_save, after_save, before_delete, after_delete")
    print("  - FullMixin: all of the above combined")
    return (
        F,
        FullMixin,
        HooksMixin,
        SoftDeleteMixin,
        SQLerDB,
        SQLerModel,
        TimestampMixin,
        datetime,
        db,
        timezone,
    )


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 2. TimestampMixin

        Adds `created_at` and `updated_at` fields to track when records are
        created and modified. You need to set these in your `save()` override:
        """
    )
    return


@app.cell
def _(SQLerModel, TimestampMixin, datetime, db, timezone):
    from typing import Self

    class Post(TimestampMixin, SQLerModel):
        _table = "posts"
        title: str
        content: str

        def save(self) -> Self:
            """Override save to set timestamps automatically."""
            now = datetime.now(timezone.utc)
            if self._id is None:
                self.created_at = now
            self.updated_at = now
            return super().save()

    Post.set_db(db)
    print("Post model with TimestampMixin ready!")
    return Post, Self


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

    print(f"\nAfter update:")
    print(f"  created_at: {post.created_at} (unchanged)")
    print(f"  updated_at: {post.updated_at} (updated)")
    return post, time


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 3. SoftDeleteMixin

        Instead of permanently deleting records, soft delete marks them as deleted
        by setting a `deleted_at` timestamp. The data remains in the database
        for audit trails or potential recovery.
        """
    )
    return


@app.cell
def _(SQLerModel, SoftDeleteMixin, db):
    class Document(SoftDeleteMixin, SQLerModel):
        _table = "documents"
        name: str
        content: str

    Document.set_db(db)
    print("Document model with SoftDeleteMixin ready!")
    print("\nSoftDeleteMixin provides:")
    print("  - deleted_at: Optional[datetime] field")
    print("  - is_deleted: property (True if deleted_at is set)")
    print("  - soft_delete(): Mark as deleted")
    print("  - restore(): Undelete")
    print("  - hard_delete(): Permanently delete")
    return (Document,)


@app.cell
def _(Document, F):
    # Create some documents
    doc1 = Document(name="report.pdf", content="Annual report").save()
    doc2 = Document(name="notes.txt", content="Meeting notes").save()
    doc3 = Document(name="draft.doc", content="Draft proposal").save()

    print(f"Created 3 documents")
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
    mo.md(
        r"""
        ## 4. HooksMixin

        Adds lifecycle hooks that are **automatically called** by `save()` and `delete()`:
        - `before_save()` - Called before saving, return False to abort
        - `after_save()` - Called after successful save
        - `before_delete()` - Called before deleting, return False to abort
        - `after_delete()` - Called after successful delete
        """
    )
    return


@app.cell
def _(HooksMixin, SQLerModel, db):
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

    User.set_db(db)
    print("User model with HooksMixin ready!")
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
    mo.md(
        r"""
        ## 5. Aborting Operations with Hooks

        Return `False` from `before_save` or `before_delete` to abort the operation:
        """
    )
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
                print(f"  [before_delete] BLOCKED: Record is locked!")
                return False  # Abort the delete
            return True

    ProtectedRecord.set_db(db)

    # Create a locked record
    _record = ProtectedRecord(data="Important data", locked=True).save()
    print(f"Created locked record: {_record.data}")

    # Try to delete it
    print("\nAttempting to delete locked record...")
    try:
        _record.delete()  # This will be blocked
        print("Deleted (unexpected!)")
    except RuntimeError as _e:
        print(f"Delete blocked: {_e}")

    # Verify it still exists
    _still_exists = ProtectedRecord.from_id(_record._id)
    print(f"Record still exists: {_still_exists is not None}")
    return


@app.cell
def _(mo):
    mo.md(
        r"""
        ## 6. FullMixin: All Combined

        `FullMixin` combines all three mixins for maximum functionality:
        """
    )
    return


@app.cell
def _(FullMixin, SQLerModel, datetime, db, timezone):
    from typing import Self as SelfType

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

    Task.set_db(db)
    print("Task model with FullMixin ready!")
    print("\nFullMixin provides:")
    print("  - TimestampMixin: created_at, updated_at")
    print("  - SoftDeleteMixin: soft_delete(), restore(), hard_delete()")
    print("  - HooksMixin: before/after save/delete hooks")
    return SelfType, Task


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
    mo.md(
        r"""
        ## 7. Querying with SoftDeleteMixin

        Common patterns for querying soft-deletable models:
        """
    )
    return


@app.cell
def _(F, Task):
    # Create more tasks for querying
    Task(title="Task A", priority=1).save()
    Task(title="Task B", priority=2).save()
    task_c = Task(title="Task C", priority=3).save()
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
    mo.md(
        r"""
        ## Summary

        SQLer mixins add common functionality without boilerplate:

        | Mixin | Features |
        |-------|----------|
        | `TimestampMixin` | `created_at`, `updated_at` fields |
        | `SoftDeleteMixin` | `deleted_at`, `is_deleted`, `soft_delete()`, `restore()`, `hard_delete()` |
        | `HooksMixin` | `before_save()`, `after_save()`, `before_delete()`, `after_delete()` |
        | `FullMixin` | All of the above combined |

        **Usage Pattern:**
        ```python
        class MyModel(SomeMixin, SQLerModel):
            _table = "my_table"
            # your fields...
        ```

        **Important:** Mixins come BEFORE `SQLerModel` in the inheritance list!

        **Next up:** Tour 06 covers Advanced Features (bulk ops, integrity policies)!
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
