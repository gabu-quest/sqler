"""Example: Aggregate Functions and Bulk Operations with SQLer.

This example demonstrates how to use aggregate functions (sum, avg, min, max)
and bulk operations (update, delete_all) for efficient data manipulation.
"""

from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F


class Sale(SQLerModel):
    product: str
    category: str
    quantity: int
    price: float
    region: str


def main():
    db = SQLerDB.in_memory()
    Sale.set_db(db)

    # Create sample sales data
    sales_data = [
        ("Widget", "electronics", 5, 29.99, "north"),
        ("Widget", "electronics", 3, 29.99, "south"),
        ("Gadget", "electronics", 10, 49.99, "north"),
        ("Tool A", "hardware", 2, 19.99, "north"),
        ("Tool B", "hardware", 8, 24.99, "south"),
        ("Tool A", "hardware", 4, 19.99, "east"),
        ("Gadget", "electronics", 6, 49.99, "east"),
        ("Widget", "electronics", 7, 29.99, "west"),
    ]
    for product, category, qty, price, region in sales_data:
        Sale(product=product, category=category, quantity=qty, price=price, region=region).save()

    print("=== Aggregate Functions ===")

    # Count all sales
    total_sales = Sale.query().count()
    print(f"Total sales records: {total_sales}")

    # Sum of quantities
    total_qty = Sale.query().sum("quantity")
    print(f"Total quantity sold: {total_qty}")

    # Average price
    avg_price = Sale.query().avg("price")
    print(f"Average price: ${avg_price:.2f}")

    # Min and max prices
    min_price = Sale.query().min("price")
    max_price = Sale.query().max("price")
    print(f"Price range: ${min_price} - ${max_price}")

    print("\n=== Aggregates with Filters ===")

    # Sum quantity for electronics only
    electronics_qty = Sale.query().filter(F("category") == "electronics").sum("quantity")
    print(f"Electronics quantity sold: {electronics_qty}")

    # Average price in north region
    north_avg = Sale.query().filter(F("region") == "north").avg("price")
    print(f"North region average price: ${north_avg:.2f}")

    # Count sales by category
    for category in ["electronics", "hardware"]:
        count = Sale.query().filter(F("category") == category).count()
        print(f"{category.capitalize()} sales: {count}")

    print("\n=== Distinct Values ===")

    # Get unique categories
    categories = Sale.query().distinct_values("category")
    print(f"Categories: {categories}")

    # Get unique regions
    regions = Sale.query().distinct_values("region")
    print(f"Regions: {regions}")

    # Get unique products in electronics
    electronics_products = Sale.query().filter(F("category") == "electronics").distinct_values("product")
    print(f"Electronics products: {electronics_products}")

    print("\n=== Bulk Update ===")

    # Increase all electronics prices by 10%
    print("Before update:")
    for sale in Sale.query().filter(F("product") == "Widget").all():
        print(f"  Widget price: ${sale.price}")

    # Note: bulk update modifies the raw JSON, not model instances
    # For percentage increase, you'd need to iterate
    # But for flat updates:
    updated = Sale.query().filter(F("region") == "south").update(region="southeast")
    print(f"\nUpdated {updated} sales to southeast region")

    # Verify update
    southeast_count = Sale.query().filter(F("region") == "southeast").count()
    print(f"Sales in southeast: {southeast_count}")

    print("\n=== Bulk Delete ===")

    print(f"Total sales before delete: {Sale.query().count()}")

    # Delete all hardware sales
    deleted = Sale.query().filter(F("category") == "hardware").delete_all()
    print(f"Deleted {deleted} hardware sales")

    print(f"Total sales after delete: {Sale.query().count()}")

    print("\n=== OR Filter ===")

    # Find sales in north OR west regions
    results = (
        Sale.query()
        .filter(F("region") == "north")
        .or_filter(F("region") == "west")
        .all()
    )
    print(f"Sales in north or west: {len(results)}")
    for sale in results:
        print(f"  {sale.product} in {sale.region}")

    print("\n=== Between Filter ===")

    # Find sales with quantity between 5 and 10
    results = Sale.query().filter(F("quantity").between(5, 10)).all()
    print(f"Sales with qty 5-10: {len(results)}")
    for sale in results:
        print(f"  {sale.product}: qty={sale.quantity}")

    print("\n=== Null Checks ===")

    # Add a sale with optional field
    class SaleWithDiscount(SQLerModel):
        product: str
        price: float
        discount: float | None = None

    SaleWithDiscount.set_db(db)
    SaleWithDiscount(product="Item A", price=100, discount=10).save()
    SaleWithDiscount(product="Item B", price=50).save()  # no discount
    SaleWithDiscount(product="Item C", price=75, discount=5).save()

    discounted = SaleWithDiscount.query().filter(F("discount").is_not_null()).all()
    print(f"Items with discount: {len(discounted)}")

    full_price = SaleWithDiscount.query().filter(F("discount").is_null()).all()
    print(f"Items at full price: {len(full_price)}")

    db.close()


if __name__ == "__main__":
    main()
