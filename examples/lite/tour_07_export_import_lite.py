# /// script
# requires-python = ">=3.12"
# dependencies = ["marimo"]
# ///
"""SQLer Lite Tour: Export & Import - Works in Pyodide/WASM!"""

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
    # SQLer Lite Tour: Export & Import

    This notebook covers data export and import for backup, migration, and data
    interchange using **SQLer Lite** (dataclass-based models) in the browser.

    You'll learn:

    1. Manual CSV export using Python's `csv` module
    2. Manual JSON export using Python's `json` module
    3. Import data from CSV and JSON formats
    4. Export filtered query results
    5. JSONL format for streaming data

    Let's explore!
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    > **Lite vs Pydantic**: This tour uses `SQLerLiteModel` (dataclasses) so it runs
    > in your browser via WebAssembly. With `SQLerModel` (Pydantic), you also get:
    > - `export_csv_string()` / `export_json_string()` convenience functions
    > - `export_csv()` / `export_json()` for file-based export
    > - `ImportResult` tracking with error details
    >
    > ```python
    > # Pydantic version — one-line export
    > from sqler.export import export_csv_string
    > csv_data = export_csv_string(User.query())
    > ```
    >
    > Run locally: `uv run marimo edit examples/tour_07_export_import.py`
    """)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 1. Setting Up

    We'll import SQLer and standard library modules for CSV and JSON handling:
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

    import csv
    import importlib
    import io
    import json
    from dataclasses import dataclass

    _sqler = importlib.import_module("sqler")
    F = _sqler.F
    SQLerDB = _sqler.SQLerDB
    SQLerLiteModel = _sqler.SQLerLiteModel

    # Create an in-memory database for this tour
    db = SQLerDB.in_memory()
    print("Database connected!")
    print("\nExport formats: CSV, JSON, JSONL")
    return F, SQLerDB, SQLerLiteModel, csv, dataclass, db, io, json


@app.cell
def _(SQLerLiteModel, dataclass, db):
    @dataclass
    class User(SQLerLiteModel):
        __tablename__ = "users"

        name: str
        email: str
        age: int
        active: bool = True

    User.set_db(db)

    # Create sample data
    users_data = [
        ("Alice", "alice@example.com", 30),
        ("Bob", "bob@example.com", 25),
        ("Carol", "carol@example.com", 35),
        ("Dave", "dave@example.com", 28),
        ("Eve", "eve@example.com", 32),
    ]
    for name, email, age in users_data:
        User(name=name, email=email, age=age).save()

    print(f"Created {User.query().count()} users")
    return (User,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 2. Manual CSV Export

    Since SQLer Lite doesn't have built-in CSV export (Pydantic-only), we'll use
    Python's `csv` module and `model_dump()`:
    """)
    return


@app.cell
def _(User, csv, io):
    # Get all users
    users = User.query().all()

    # Create CSV in memory
    csv_buffer = io.StringIO()
    if users:
        # Get field names from first user (exclude _id if not needed)
        fieldnames = [k for k in users[0].model_dump().keys()]
        writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames)

        writer.writeheader()
        for user in users:
            writer.writerow(user.model_dump())

    csv_data = csv_buffer.getvalue()
    print("CSV Export (all users):")
    print(csv_data)
    return csv_buffer, csv_data, fieldnames, user, users, writer


@app.cell
def _(mo):
    mo.md(r"""
    ## 3. Filtered CSV Export

    Export only records matching a query:
    """)
    return


@app.cell
def _(F, User, csv, io):
    # Export filtered results (age > 28)
    older_users = User.query().filter(F("age") > 28).all()

    csv_filtered_buffer = io.StringIO()
    if older_users:
        fieldnames_filtered = [k for k in older_users[0].model_dump().keys()]
        writer_filtered = csv.DictWriter(csv_filtered_buffer, fieldnames=fieldnames_filtered)

        writer_filtered.writeheader()
        for u in older_users:
            writer_filtered.writerow(u.model_dump())

    csv_filtered = csv_filtered_buffer.getvalue()
    print("CSV Export (age > 28):")
    print(csv_filtered)
    return csv_filtered, csv_filtered_buffer, fieldnames_filtered, older_users, u, writer_filtered


@app.cell
def _(mo):
    mo.md(r"""
    ## 4. Manual JSON Export

    Export to JSON format using `json.dumps()`:
    """)
    return


@app.cell
def _(User, json):
    # Export all users to JSON
    users_json = User.query().all()
    json_data = json.dumps([u.model_dump() for u in users_json], indent=2)

    print("JSON Export:")
    print(json_data)
    return json_data, users_json


@app.cell
def _(mo):
    mo.md(r"""
    ## 5. Export Specific Fields

    Control which fields are exported by filtering the dict:
    """)
    return


@app.cell
def _(User, csv, io, json):
    # CSV with selected fields (name and email only)
    users_partial = User.query().all()

    csv_partial_buffer = io.StringIO()
    if users_partial:
        selected_fields = ["name", "email"]
        writer_partial = csv.DictWriter(csv_partial_buffer, fieldnames=selected_fields)

        writer_partial.writeheader()
        for u_p in users_partial:
            row = {k: v for k, v in u_p.model_dump().items() if k in selected_fields}
            writer_partial.writerow(row)

    csv_partial = csv_partial_buffer.getvalue()
    print("CSV with selected fields:")
    print(csv_partial)

    # JSON with selected fields (name and age only)
    json_partial = json.dumps(
        [{k: v for k, v in u_p.model_dump().items() if k in ["name", "age"]}
         for u_p in users_partial],
        indent=2
    )
    print("\nJSON with selected fields:")
    print(json_partial)
    return csv_partial, csv_partial_buffer, json_partial, row, selected_fields, u_p, users_partial, writer_partial


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. Import from CSV

    Parse CSV data and create model instances:
    """)
    return


@app.cell
def _(SQLerLiteModel, csv, dataclass, db, io):
    @dataclass
    class Product(SQLerLiteModel):
        __tablename__ = "products"

        name: str
        price: float
        category: str

    Product.set_db(db)

    # Simulate CSV data (in real usage, this would come from a file)
    csv_content = """name,price,category
Widget,29.99,electronics
Gadget,49.99,electronics
Tool,19.99,hardware
Book,14.99,media"""

    # Parse CSV and import
    reader = csv.DictReader(io.StringIO(csv_content))
    imported = []
    for row_p in reader:
        p = Product(name=row_p["name"], price=float(row_p["price"]), category=row_p["category"])
        p.save()
        imported.append(p)

    print(f"Imported {len(imported)} products")
    for p in Product.query().all():
        print(f"  {p.name}: ${p.price} ({p.category})")
    return Product, csv_content, imported, p, reader, row_p


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. Import from JSON

    Parse JSON data and create model instances:
    """)
    return


@app.cell
def _(SQLerLiteModel, dataclass, db, json):
    @dataclass
    class Order(SQLerLiteModel):
        __tablename__ = "orders"

        customer: str
        total: float
        status: str = "pending"

    Order.set_db(db)

    # Simulate JSON data
    json_content = """[
        {"customer": "Alice", "total": 99.99, "status": "shipped"},
        {"customer": "Bob", "total": 149.50, "status": "pending"},
        {"customer": "Carol", "total": 75.00, "status": "delivered"}
    ]"""

    # Parse and import
    orders_data = json.loads(json_content)
    for order_dict in orders_data:
        Order(**order_dict).save()

    print(f"Imported {Order.query().count()} orders")
    for o in Order.query().all():
        print(f"  {o.customer}: ${o.total} ({o.status})")
    return Order, json_content, o, order_dict, orders_data


@app.cell
def _(mo):
    mo.md(r"""
    ## 8. JSONL Format for Streaming

    JSONL (JSON Lines) is ideal for large datasets - one record per line:
    """)
    return


@app.cell
def _(User, json):
    # JSONL format: one JSON object per line
    # Great for streaming large datasets

    jsonl_lines = []
    for user_j in User.query().all():
        # Each line is a separate JSON object
        jsonl_lines.append(json.dumps(user_j.model_dump()))

    jsonl_data = "\n".join(jsonl_lines)

    print("JSONL format (one record per line):")
    print(jsonl_data)

    print("\n\nBenefits of JSONL:")
    print("  - Stream processing (no need to load entire file)")
    print("  - Easy to append new records")
    print("  - Line-by-line error recovery")
    return jsonl_data, jsonl_lines, user_j


@app.cell
def _(mo):
    mo.md(r"""
    ## 9. Parsing JSONL

    Import from JSONL format line by line:
    """)
    return


@app.cell
def _(SQLerLiteModel, dataclass, db, json):
    @dataclass
    class Event(SQLerLiteModel):
        __tablename__ = "events"

        event_type: str
        timestamp: str
        user_id: int

    Event.set_db(db)

    # Simulate JSONL data
    jsonl_import = """{"event_type": "login", "timestamp": "2024-01-01T10:00:00", "user_id": 1}
{"event_type": "view", "timestamp": "2024-01-01T10:05:00", "user_id": 1}
{"event_type": "logout", "timestamp": "2024-01-01T10:30:00", "user_id": 1}"""

    # Parse JSONL line by line
    for line in jsonl_import.strip().split("\n"):
        event_dict = json.loads(line)
        Event(**event_dict).save()

    print(f"Imported {Event.query().count()} events")
    for e in Event.query().all():
        print(f"  {e.event_type} at {e.timestamp} (user {e.user_id})")
    return Event, e, event_dict, jsonl_import, line


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    SQLer Lite export/import patterns:

    | Operation | Approach |
    |-----------|----------|
    | **CSV Export** | `csv.DictWriter()` + `model.model_dump()` |
    | **JSON Export** | `json.dumps([m.model_dump() for m in queryset])` |
    | **JSONL Export** | `json.dumps(m.model_dump())` per line |
    | **CSV Import** | `csv.DictReader()` + `Model(**row)` |
    | **JSON Import** | `json.loads()` + `Model(**dict)` |
    | **JSONL Import** | Parse line by line with `json.loads()` |

    **Key differences from Pydantic version:**
    - Manual export using stdlib `csv` and `json` modules
    - No `export_csv_string()` / `export_json_string()` shortcuts
    - No `ImportResult` tracking (but same import patterns)
    - Full control over field selection via dict filtering

    **Shared patterns:**
    - Both use `model_dump()` to get dict representation
    - Import logic is identical (parse and instantiate)
    - Query filtering works the same way

    For convenience functions and file-based export, use the Pydantic version locally!
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
