"""
sqler demo — run with: uv run python demo.py

Shows the core features in ~60 seconds of terminal output.
No external dependencies beyond sqler[pydantic].
"""

import time

from sqler import F, SQLerDB, SQLerModel, SQLerSafeModel, StaleVersionError


# ── Helpers ──────────────────────────────────────────────────────────────
def header(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}\n")


def show(label: str, value: object) -> None:
    print(f"  {label:.<40s} {value}")


# ── Models ───────────────────────────────────────────────────────────────
class User(SQLerModel):
    name: str
    age: int
    tags: list[str] = []


class Account(SQLerSafeModel):
    owner: str
    balance: int


# ── Setup ────────────────────────────────────────────────────────────────
db = SQLerDB.in_memory()
User.set_db(db)
Account.set_db(db)

# ── 1. Insert & query ───────────────────────────────────────────────────
header("1. Insert & Query")

User(name="Alice", age=30, tags=["admin", "python"]).save()
User(name="Bob", age=25, tags=["user"]).save()
User(name="Charlie", age=35, tags=["admin"]).save()
User(name="Diana", age=28, tags=["user", "python"]).save()

all_users = User.query().all()
show("Total users", len(all_users))

admins = User.query().filter(F("tags").contains("admin")).all()
show("Admins", [u.name for u in admins])

young = User.query().filter(F("age") < 30).order_by("name").all()
show("Under 30 (sorted)", [u.name for u in young])

pythonistas = User.query().filter(F("tags").contains("python")).all()
show("Python users", [u.name for u in pythonistas])

avg_age = User.query().avg("age")
show("Average age", avg_age)

# ── 2. Index speedup ────────────────────────────────────────────────────
header("2. Index Speedup")

# Insert more rows so the speedup is visible
docs = [{"name": f"user_{i}", "age": i % 60, "tags": []} for i in range(50_000)]
db.bulk_upsert("users", docs)
show("Total rows after bulk insert", User.query().count())

# Query WITHOUT index
t0 = time.perf_counter()
for _ in range(100):
    User.query().filter(F("age") == 25).all()
no_idx = (time.perf_counter() - t0) / 100

# Add index
User.add_index("age")

# Query WITH index
t0 = time.perf_counter()
for _ in range(100):
    User.query().filter(F("age") == 25).all()
with_idx = (time.perf_counter() - t0) / 100

show("Without index (avg)", f"{no_idx * 1000:.2f} ms")
show("With index (avg)", f"{with_idx * 1000:.2f} ms")
if with_idx > 0:
    show("Speedup", f"{no_idx / with_idx:.0f}x")

# ── 3. Safe models — optimistic locking ─────────────────────────────────
header("3. Safe Models — Optimistic Locking")

acc = Account(owner="Ada", balance=100).save()
show("Created", f"owner={acc.owner}, balance={acc.balance}, version={acc._version}")

acc.balance = 150
acc.save()
show("Updated", f"balance={acc.balance}, version={acc._version}")

# Simulate a stale write
stale = Account.from_id(acc._id)
acc.balance = 200
acc.save()  # bumps version on the DB side

try:
    stale.balance = 999
    stale.save()  # stale version → conflict!
except StaleVersionError:
    show("Conflict detected", "StaleVersionError raised")
    stale.refresh()
    show("After refresh", f"balance={stale.balance}, version={stale._version}")

# ── Done ─────────────────────────────────────────────────────────────────
header("Done!")
print("  Try the interactive tours:  uv run marimo edit examples/tour_01_fundamentals.py")
print("  Full docs:                  https://gabu-quest.github.io/sqler/")
print()
