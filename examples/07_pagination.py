"""Example: Pagination with SQLer.

This example demonstrates how to use pagination for handling large result sets.
"""

from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F


class Article(SQLerModel):
    title: str
    author: str
    views: int = 0


def main():
    db = SQLerDB.in_memory()
    Article.set_db(db)

    # Create sample articles
    for i in range(25):
        Article(title=f"Article {i + 1}", author=f"Author {i % 5 + 1}", views=i * 10).save()

    print("=== Basic Pagination ===")
    # Get first page with 5 items per page
    # Note: paginate() returns dicts - use .items for the list of items
    page1 = Article.query().order_by("title").paginate(page=1, per_page=5)
    print(f"Page {page1.page} of {page1.total_pages}")
    print(f"Showing {len(page1)} of {page1.total} total items")
    for article in page1:
        # Items are dicts, access fields via dictionary syntax
        print(f"  - {article['title']}")

    print("\n=== Navigation ===")
    print(f"Has previous page: {page1.has_prev}")
    print(f"Has next page: {page1.has_next}")
    print(f"Next page number: {page1.next_page}")

    print("\n=== Pagination with Filters ===")
    # Paginate filtered results
    filtered = Article.query().filter(F("views") >= 100).paginate(page=1, per_page=5)
    print(f"Articles with 100+ views: {filtered.total} total")
    print(f"Pages needed: {filtered.total_pages}")

    print("\n=== Converting to Dict (for APIs) ===")
    # Convert to dictionary for JSON responses
    page_dict = page1.to_dict()
    print(f"Keys in response: {list(page_dict.keys())}")

    print("\n=== Iterating Through All Pages ===")
    page_num = 1
    per_page = 10
    while True:
        page = (
            Article.query().order_by("views", desc=True).paginate(page=page_num, per_page=per_page)
        )
        print(f"Page {page_num}: {len(page)} items")
        if not page.has_next:
            break
        page_num += 1

    db.close()


if __name__ == "__main__":
    main()
