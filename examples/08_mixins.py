"""Example: Using Mixins with SQLer.

This example demonstrates how to use the built-in mixins for timestamps,
soft delete, and lifecycle hooks.

NOTE: HooksMixin provides hook method definitions but requires you to
override save()/delete() to call them. This example shows the proper pattern.
"""

from datetime import datetime, timezone
from typing import Self

from sqler import SQLerDB, SQLerModel
from sqler.models import SoftDeleteMixin, TimestampMixin
from sqler.query import SQLerField as F


# Example 1: TimestampMixin - Provides created_at and updated_at fields
class Post(TimestampMixin, SQLerModel):
    title: str
    content: str

    def save(self) -> Self:
        """Override save to set timestamps automatically."""
        now = datetime.now(timezone.utc)
        if self._id is None:
            self.created_at = now
        self.updated_at = now
        return super().save()


# Example 2: SoftDeleteMixin - Soft delete instead of permanent deletion
class Document(SoftDeleteMixin, SQLerModel):
    name: str
    content: str


# Example 3: Custom hooks pattern - Override save() to add lifecycle hooks
class User(SQLerModel):
    name: str
    email: str

    def save(self) -> Self:
        """Save with preprocessing hooks."""
        # Pre-save hook: normalize email
        self.email = self.email.lower().strip()
        print(f"  [before_save] Normalizing email to: {self.email}")

        # Call parent save
        result = super().save()

        # Post-save hook
        print(f"  [after_save] User saved with id: {self._id}")
        return result

    def delete(self) -> None:
        """Delete with hooks."""
        print(f"  [before_delete] About to delete user: {self.name}")
        super().delete()
        print(f"  [after_delete] User deleted")


# Example 4: Combined mixins with auto-timestamps
class Task(SoftDeleteMixin, TimestampMixin, SQLerModel):
    title: str
    description: str = ""
    priority: int = 0

    def save(self) -> Self:
        """Save with automatic timestamps."""
        now = datetime.now(timezone.utc)
        if self._id is None:
            self.created_at = now
        self.updated_at = now
        print(f"  [before_save] Task: {self.title}")
        return super().save()


def main():
    db = SQLerDB.in_memory()
    Post.set_db(db)
    Document.set_db(db)
    User.set_db(db)
    Task.set_db(db)

    print("=== TimestampMixin Example ===")
    post = Post(title="Hello World", content="My first post")
    post.save()
    print(f"Post created at: {post.created_at}")
    print(f"Post updated at: {post.updated_at}")

    # Update the post - updated_at changes
    post.content = "Updated content"
    post.save()
    print(f"After update - updated at: {post.updated_at}")

    print("\n=== SoftDeleteMixin Example ===")
    doc = Document(name="Important.txt", content="Do not delete").save()
    print(f"Document created: {doc.name} (id={doc._id})")

    # Soft delete the document
    doc.soft_delete()
    print(f"Document soft deleted: is_deleted={doc.is_deleted}")
    print(f"Deleted at: {doc.deleted_at}")

    # The document still exists in the database
    all_docs = Document.query().all()
    print(f"Total documents (including deleted): {len(all_docs)}")

    # Filter out soft-deleted documents using is_null()
    active_docs = Document.query().filter(F("deleted_at").is_null()).all()
    print(f"Active documents: {len(active_docs)}")

    # Restore the document
    doc.restore()
    print(f"Document restored: is_deleted={doc.is_deleted}")

    # Hard delete permanently removes the document
    doc.hard_delete()
    print("Document hard deleted (permanent)")

    print("\n=== Custom Hooks Pattern Example ===")
    user = User(name="Alice", email="  ALICE@Example.COM  ")
    print("Creating user with messy email...")
    user.save()
    print(f"Saved user: {user.name} <{user.email}>")

    print("\nDeleting user...")
    user.delete()

    print("\n=== Combined Mixins Example ===")
    task = Task(title="Learn SQLer", description="Read the docs", priority=1)
    task.save()
    print(f"Task: {task.title}")
    print(f"Created: {task.created_at}")
    print(f"Updated: {task.updated_at}")

    # Soft delete the task
    task.soft_delete()
    print(f"Task soft deleted: {task.is_deleted}")

    db.close()


if __name__ == "__main__":
    main()
