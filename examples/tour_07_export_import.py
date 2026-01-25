# /// script
# requires-python = ">=3.12"
# dependencies = ["sqler", "marimo"]
# ///

import marimo

__generated_with = "0.18.4"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    return (mo,)


@app.cell
def _(mo):
    mo.md(r"""
    # SQLer Tour: Export & Import

    This notebook covers SQLer's data export and import capabilities for
    backup, migration, and data interchange.

    You'll learn:

    1. Export to CSV, JSON, and JSONL formats
    2. Export query results vs entire tables
    3. Import data from files
    4. Streaming large datasets
    5. Export/Import results and error handling

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
    from sqler import SQLerDB, SQLerModel
    from sqler.export import (
        ExportResult,
        ImportResult,
        export_csv_string,
        export_json_string,
    )
    from sqler.query import SQLerField as F

    db = SQLerDB.in_memory()
    print("Database connected!")
    print("\nExport formats: CSV, JSON, JSONL")
    return F, SQLerModel, db, export_csv_string, export_json_string


@app.cell
def _(SQLerModel, db):
    class User(SQLerModel):
        _table = "users"
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
    ## 2. Export to CSV String

    Export query results to CSV format. In the browser, we use string export:
    """)
    return


@app.cell
def _(User, export_csv_string):
    # Export all users to CSV
    csv_data = export_csv_string(User.query())

    print("CSV Export:")
    print(csv_data)
    return


@app.cell
def _(F, User, export_csv_string):
    # Export filtered results
    active_users = User.query().filter(F("age") > 28)
    csv_filtered = export_csv_string(active_users)

    print("CSV Export (age > 28):")
    print(csv_filtered)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 3. Export to JSON String

    Export to JSON format for APIs and web applications:
    """)
    return


@app.cell
def _(User, export_json_string):
    # Export all users to JSON
    json_data = export_json_string(User.query(), indent=2)

    print("JSON Export:")
    print(json_data)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 4. Export Specific Fields

    Control which fields are exported:
    """)
    return


@app.cell
def _(User, export_csv_string, export_json_string):
    # Export only specific fields
    csv_partial = export_csv_string(User.query(), fields=["name", "email"])
    print("CSV with selected fields:")
    print(csv_partial)

    # JSON with selected fields
    json_partial = export_json_string(User.query(), fields=["name", "age"], indent=2)
    print("\nJSON with selected fields:")
    print(json_partial)
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 5. Import from CSV

    Import data from CSV format. Let's create a new model and import data:
    """)
    return


@app.cell
def _(SQLerModel, db):
    import csv
    import io

    from sqler.export import ImportResult as ImpResult

    class Product(SQLerModel):
        _table = "products"
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
    for row in reader:
        p = Product(name=row["name"], price=float(row["price"]), category=row["category"])
        p.save()
        imported.append(p)

    print(f"Imported {len(imported)} products")
    for p in Product.query().all():
        print(f"  {p.name}: ${p.price} ({p.category})")
    return (ImpResult,)


@app.cell
def _(mo):
    mo.md(r"""
    ## 6. Import from JSON

    Import data from JSON format:
    """)
    return


@app.cell
def _(SQLerModel, db):
    import json

    class Order(SQLerModel):
        _table = "orders"
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
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 7. Export Results

    The export functions return `ExportResult` with metadata:
    """)
    return


@app.cell
def _():
    from sqler.export import export_csv, export_json

    # When exporting to files, you get an ExportResult
    # (In WASM we can't write files, so we show the concept)

    print("ExportResult fields:")
    print("  - path: output file path")
    print("  - format: 'csv', 'json', or 'jsonl'")
    print("  - count: number of records exported")
    print("  - size_bytes: file size")

    # Example (would work in native Python):
    # result = export_csv(User.query(), "/tmp/users.csv")
    # print(f"Exported {result.count} records to {result.path}")
    # print(f"File size: {result.size_bytes} bytes")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 8. Import Results and Error Handling

    Import operations return `ImportResult` with error details:
    """)
    return


@app.cell
def _(ImpResult):
    # ImportResult tracks success/failure
    print("ImportResult fields:")
    print("  - count: total records attempted")
    print("  - succeeded: records successfully imported")
    print("  - failed: records that failed")
    print("  - errors: list of error details")
    print("  - success_rate: succeeded / count")

    # Example result
    example = ImpResult(count=10, succeeded=8, failed=2, errors=[{"row": 3, "error": "invalid email"}])
    print(f"\nExample: {example.succeeded}/{example.count} succeeded ({example.success_rate:.0%})")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## 9. JSONL Format for Streaming

    JSONL (JSON Lines) is ideal for large datasets - one record per line:
    """)
    return


@app.cell
def _(User):
    # JSONL format: one JSON object per line
    # Great for streaming large datasets

    jsonl_data = []
    for user in User.query().all():
        record = {"name": user.name, "email": user.email, "age": user.age}
        jsonl_data.append(str(record).replace("'", '"'))

    print("JSONL format (one record per line):")
    for line in jsonl_data:
        print(line)

    print("\nBenefits of JSONL:")
    print("  - Stream processing (no need to load entire file)")
    print("  - Easy to append new records")
    print("  - Line-by-line error recovery")
    return


@app.cell
def _(mo):
    mo.md(r"""
    ## Summary

    SQLer export/import features:

    | Function | Description |
    |----------|-------------|
    | `export_csv(query, path)` | Export to CSV file |
    | `export_csv_string(query)` | Export to CSV string |
    | `export_json(query, path)` | Export to JSON file |
    | `export_json_string(query)` | Export to JSON string |
    | `export_jsonl(query, path)` | Export to JSONL file |
    | `import_csv(Model, path)` | Import from CSV file |
    | `import_json(Model, path)` | Import from JSON file |
    | `import_jsonl(Model, path)` | Import from JSONL file |
    | `stream_jsonl(query, path)` | Stream large datasets |

    **Key Options:**
    - `fields=[...]`: Export only specific fields
    - `indent=2`: Pretty-print JSON
    - `include_id=True`: Include model ID in export

    **Next up:** Tour 08 covers Full-Text Search!
    """)
    return


@app.cell
def _(db):
    db.close()
    print("Database closed!")
    return


if __name__ == "__main__":
    app.run()
