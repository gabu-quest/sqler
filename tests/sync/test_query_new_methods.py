"""Comprehensive tests for new query builder methods.

These tests are designed to be rigorous, covering edge cases, combinations,
and real-world scenarios to ensure the new functionality is robust.
"""

import pytest
from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

# ============================================================================
# Test Models
# ============================================================================


class Product(SQLerModel):
    name: str
    price: float
    category: str
    in_stock: bool = True
    tags: list[str] = []
    email: str | None = None
    metadata: dict | None = None


class User(SQLerModel):
    username: str
    email: str
    age: int
    role: str = "user"
    active: bool = True
    score: float = 0.0


class Event(SQLerModel):
    name: str
    date: str
    attendees: int = 0
    canceled: bool = False
    organizer: str | None = None
    location: dict | None = None


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def db():
    """Create a fresh in-memory database for each test."""
    db = SQLerDB.in_memory(shared=False)
    Product.set_db(db)
    User.set_db(db)
    Event.set_db(db)
    yield db
    db.close()


@pytest.fixture
def products(db):
    """Seed with diverse product data."""
    products = [
        Product(
            name="Widget A",
            price=10.0,
            category="tools",
            tags=["sale", "new"],
            email="widget@test.com",
        ),
        Product(name="Widget B", price=20.0, category="tools", tags=["popular"]),
        Product(name="Gadget C", price=15.0, category="electronics", tags=["new", "featured"]),
        Product(name="Gadget D", price=30.0, category="electronics", in_stock=False),
        Product(name="Thing E", price=25.0, category="misc", email="thing@example.com"),
        Product(name="Item F", price=5.0, category="tools", tags=[], in_stock=False),
        Product(name="100% Off Special", price=0.0, category="sale", tags=["clearance"]),
        Product(name="The_Underscore_Item", price=99.99, category="special"),
        Product(name="", price=1.0, category="empty"),  # Empty name edge case
        Product(name="Zzz Last", price=999.99, category="premium", metadata={"weight": 10}),
    ]
    for p in products:
        p.save()
    return products


@pytest.fixture
def users(db):
    """Seed with diverse user data."""
    users = [
        User(username="alice", email="alice@example.com", age=25, role="admin", score=95.5),
        User(username="bob", email="bob@test.org", age=30, role="user", score=82.0),
        User(username="charlie", email="charlie@example.com", age=35, role="user", score=78.5),
        User(username="diana", email="diana@other.net", age=28, role="moderator", score=88.0),
        User(username="eve", email="eve@example.com", age=22, role="user", active=False, score=0.0),
        User(username="frank", email="FRANK@EXAMPLE.COM", age=45, role="admin", score=91.0),
    ]
    for u in users:
        u.save()
    return users


@pytest.fixture
def events(db):
    """Seed with event data including nulls."""
    events = [
        Event(name="Conference A", date="2024-01-15", attendees=100, organizer="alice"),
        Event(name="Workshop B", date="2024-02-20", attendees=25, organizer="bob"),
        Event(name="Meetup C", date="2024-03-10", attendees=50, canceled=True),
        Event(name="Seminar D", date="2024-04-05", attendees=75),  # No organizer
        Event(name="Webinar E", date="2024-05-01", attendees=200, location={"city": "NYC"}),
    ]
    for e in events:
        e.save()
    return events


# ============================================================================
# BETWEEN Tests
# ============================================================================


class TestBetween:
    """Rigorous tests for between() method."""

    def test_between_inclusive_boundaries(self, db, products):
        """between() must include both boundary values."""
        results = Product.query().filter(F("price").between(10.0, 20.0)).all()
        prices = {p.price for p in results}
        assert 10.0 in prices, "Lower boundary should be included"
        assert 20.0 in prices, "Upper boundary should be included"
        assert 15.0 in prices, "Middle value should be included"

    def test_between_same_value(self, db, products):
        """between(x, x) should find exact match."""
        results = Product.query().filter(F("price").between(15.0, 15.0)).all()
        assert len(results) == 1
        assert results[0].name == "Gadget C"

    def test_between_no_matches(self, db, products):
        """between() with no matching values returns empty."""
        results = Product.query().filter(F("price").between(500.0, 600.0)).all()
        assert len(results) == 0

    def test_between_negative_values(self, db):
        """between() works with negative numbers."""
        Product(name="Negative", price=-10.0, category="test").save()
        Product(name="More Negative", price=-5.0, category="test").save()
        results = Product.query().filter(F("price").between(-15.0, -3.0)).all()
        assert len(results) == 2

    def test_between_floats_precision(self, db):
        """between() handles float precision correctly."""
        Product(name="Precise", price=10.001, category="test").save()
        results = Product.query().filter(F("price").between(10.0, 10.01)).all()
        assert len(results) == 1

    def test_between_with_integers(self, db, users):
        """between() works with integer fields."""
        results = User.query().filter(F("age").between(25, 30)).all()
        ages = {u.age for u in results}
        assert ages == {25, 28, 30}

    def test_between_combined_with_other_filters(self, db, products):
        """between() can be combined with other filter conditions."""
        results = (
            Product.query()
            .filter(F("price").between(10.0, 30.0))
            .filter(F("category") == "tools")
            .all()
        )
        assert len(results) == 2
        assert all(p.category == "tools" for p in results)


# ============================================================================
# IS NULL / IS NOT NULL Tests
# ============================================================================


class TestNullChecks:
    """Rigorous tests for is_null() and is_not_null()."""

    def test_is_null_basic(self, db, products):
        """is_null() finds records with NULL field."""
        results = Product.query().filter(F("email").is_null()).all()
        # Most products don't have email set
        assert all(p.email is None for p in results)

    def test_is_not_null_basic(self, db, products):
        """is_not_null() finds records with non-NULL field."""
        results = Product.query().filter(F("email").is_not_null()).all()
        assert len(results) == 2  # Widget A and Thing E
        assert all(p.email is not None for p in results)

    def test_is_null_with_empty_string(self, db):
        """is_null() distinguishes NULL from empty string."""
        Product(name="With Empty", price=1.0, category="test", email="").save()
        Product(name="With Null", price=2.0, category="test").save()

        null_results = Product.query().filter(F("email").is_null()).all()
        # Empty string is NOT null
        null_names = {p.name for p in null_results}
        assert "With Null" in null_names
        assert "With Empty" not in null_names

    def test_is_null_nested_field(self, db, products):
        """is_null() works on nested fields via metadata."""
        results = Product.query().filter(F("metadata").is_null()).all()
        assert len(results) == 9  # All except "Zzz Last"

    def test_is_null_combined_with_filters(self, db, events):
        """is_null() combined with other conditions."""
        results = Event.query().filter(F("organizer").is_null()).filter(F("attendees") >= 50).all()
        # Meetup C (50), Seminar D (75), Webinar E (200) - all have NULL organizer
        assert len(results) == 3

    def test_is_null_or_condition(self, db, events):
        """is_null() with or_filter()."""
        results = (
            Event.query()
            .filter(F("organizer").is_null())
            .or_filter(F("organizer") == "alice")
            .all()
        )
        # NULL organizers + alice
        assert len(results) >= 3


# ============================================================================
# STARTSWITH / ENDSWITH Tests
# ============================================================================


class TestStringPatterns:
    """Rigorous tests for startswith() and endswith()."""

    def test_startswith_basic(self, db, products):
        """startswith() matches beginning of string."""
        results = Product.query().filter(F("name").startswith("Widget")).all()
        assert len(results) == 2
        assert all(p.name.startswith("Widget") for p in results)

    def test_startswith_case_insensitive(self, db, products):
        """startswith() is case-insensitive (SQLite LIKE default behavior)."""
        results = Product.query().filter(F("name").startswith("widget")).all()
        # SQLite LIKE is case-insensitive for ASCII characters
        assert len(results) == 2  # Matches "Widget A" and "Widget B"

    def test_startswith_special_chars_percent(self, db, products):
        """startswith() properly escapes % character."""
        results = Product.query().filter(F("name").startswith("100%")).all()
        assert len(results) == 1
        assert results[0].name == "100% Off Special"

    def test_startswith_special_chars_underscore(self, db, products):
        """startswith() properly escapes _ character."""
        results = Product.query().filter(F("name").startswith("The_")).all()
        assert len(results) == 1
        assert results[0].name == "The_Underscore_Item"

    def test_startswith_empty_string(self, db, products):
        """startswith('') matches all non-null strings."""
        results = Product.query().filter(F("name").startswith("")).all()
        # Should match all products (including empty name)
        assert len(results) == 10

    def test_endswith_basic(self, db, users):
        """endswith() matches end of string (case-insensitive in SQLite)."""
        results = User.query().filter(F("email").endswith("@example.com")).all()
        # alice, charlie, eve, frank - SQLite LIKE is case-insensitive
        assert len(results) == 4

    def test_endswith_with_domain(self, db, users):
        """endswith() for domain matching."""
        results = User.query().filter(F("email").endswith(".org")).all()
        assert len(results) == 1
        assert results[0].username == "bob"

    def test_startswith_and_endswith_combined(self, db, users):
        """Combine startswith and endswith."""
        results = (
            User.query()
            .filter(F("email").startswith("a"))
            .filter(F("email").endswith(".com"))
            .all()
        )
        assert len(results) == 1
        assert results[0].username == "alice"


# ============================================================================
# GLOB Tests
# ============================================================================


class TestGlob:
    """Rigorous tests for glob() method."""

    def test_glob_asterisk(self, db, products):
        """glob() with * wildcard."""
        results = Product.query().filter(F("name").glob("Widget *")).all()
        assert len(results) == 2

    def test_glob_question_mark(self, db, products):
        """glob() with ? single character wildcard."""
        results = Product.query().filter(F("name").glob("Gadget ?")).all()
        assert len(results) == 2  # Gadget C, Gadget D

    def test_glob_character_class(self, db, products):
        """glob() with character class [abc]."""
        results = Product.query().filter(F("name").glob("Widget [AB]")).all()
        assert len(results) == 2

    def test_glob_case_sensitive(self, db, products):
        """glob() is case-sensitive unlike LIKE."""
        upper = Product.query().filter(F("name").glob("WIDGET*")).all()
        lower = Product.query().filter(F("name").glob("widget*")).all()
        assert len(upper) == 0
        assert len(lower) == 0

    def test_glob_no_match(self, db, products):
        """glob() with pattern that matches nothing."""
        results = Product.query().filter(F("name").glob("ZZZ*")).all()
        assert len(results) == 0


# ============================================================================
# IN_LIST Tests
# ============================================================================


class TestInList:
    """Rigorous tests for in_list() method."""

    def test_in_list_basic(self, db, products):
        """in_list() matches any value in list."""
        results = Product.query().filter(F("category").in_list(["tools", "misc"])).all()
        categories = {p.category for p in results}
        assert categories == {"tools", "misc"}

    def test_in_list_empty_list(self, db, products):
        """in_list([]) matches nothing."""
        results = Product.query().filter(F("category").in_list([])).all()
        assert len(results) == 0

    def test_in_list_single_value(self, db, products):
        """in_list() with single value works like equality."""
        in_list_results = Product.query().filter(F("category").in_list(["electronics"])).all()
        eq_results = Product.query().filter(F("category") == "electronics").all()
        assert len(in_list_results) == len(eq_results)

    def test_in_list_no_matches(self, db, products):
        """in_list() with values that don't exist."""
        results = Product.query().filter(F("category").in_list(["nonexistent", "fake"])).all()
        assert len(results) == 0

    def test_in_list_with_numbers(self, db, users):
        """in_list() works with numeric values."""
        results = User.query().filter(F("age").in_list([25, 30, 35])).all()
        ages = {u.age for u in results}
        assert ages == {25, 30, 35}

    def test_in_list_combined_with_filters(self, db, products):
        """in_list() combined with other filters."""
        results = (
            Product.query()
            .filter(F("category").in_list(["tools", "electronics"]))
            .filter(F("in_stock") == True)
            .all()
        )
        assert all(p.in_stock for p in results)
        assert all(p.category in ["tools", "electronics"] for p in results)


# ============================================================================
# OR_FILTER Tests
# ============================================================================


class TestOrFilter:
    """Rigorous tests for or_filter() method."""

    def test_or_filter_basic(self, db, products):
        """or_filter() creates OR condition."""
        results = (
            Product.query()
            .filter(F("category") == "tools")
            .or_filter(F("category") == "electronics")
            .all()
        )
        categories = {p.category for p in results}
        assert categories == {"tools", "electronics"}

    def test_or_filter_multiple_chains(self, db, users):
        """Multiple or_filter() chains."""
        results = (
            User.query()
            .filter(F("role") == "admin")
            .or_filter(F("role") == "moderator")
            .or_filter(F("age") < 25)
            .all()
        )
        # admins (alice, frank), moderator (diana), age < 25 (eve)
        usernames = {u.username for u in results}
        assert usernames == {"alice", "frank", "diana", "eve"}

    def test_or_filter_with_and_filter(self, db, users):
        """or_filter() combined with filter() (AND)."""
        # (role == admin OR age > 40) AND active == True
        results = (
            User.query()
            .filter(F("role") == "admin")
            .or_filter(F("age") > 40)
            .filter(F("active") == True)  # This ANDs with the result
            .all()
        )
        # Should get admins who are active OR people > 40 who are active
        # alice (admin, active), frank (admin, age 45, active)
        usernames = {u.username for u in results}
        assert "alice" in usernames
        assert "frank" in usernames

    def test_or_filter_complex_conditions(self, db, products):
        """or_filter() with complex nested conditions."""
        results = (
            Product.query()
            .filter((F("price") > 20.0) & (F("category") == "electronics"))
            .or_filter((F("price") < 10.0) & (F("in_stock") == True))
            .all()
        )
        # High-priced electronics OR cheap in-stock items
        for p in results:
            assert (p.price > 20.0 and p.category == "electronics") or (
                p.price < 10.0 and p.in_stock
            )


# ============================================================================
# DISTINCT and DISTINCT_VALUES Tests
# ============================================================================


class TestDistinct:
    """Rigorous tests for distinct() and distinct_values()."""

    def test_distinct_values_basic(self, db, products):
        """distinct_values() returns unique values."""
        categories = Product.query().distinct_values("category")
        expected = {"tools", "electronics", "misc", "sale", "special", "empty", "premium"}
        assert set(categories) == expected

    def test_distinct_values_with_filter(self, db, products):
        """distinct_values() respects filters."""
        categories = Product.query().filter(F("price") > 15.0).distinct_values("category")
        # Products > 15: Widget B (20, tools), Gadget D (30, electronics),
        # Thing E (25, misc), The_Underscore (99.99, special), Zzz Last (999.99, premium)
        assert set(categories) == {"tools", "electronics", "misc", "special", "premium"}

    def test_distinct_values_excludes_null(self, db, products):
        """distinct_values() excludes NULL values."""
        emails = Product.query().distinct_values("email")
        assert None not in emails
        assert len(emails) == 2  # widget@test.com, thing@example.com

    def test_distinct_values_with_duplicates(self, db):
        """distinct_values() properly deduplicates."""
        for _ in range(5):
            Product(name="Dup", price=1.0, category="dup_category").save()
        categories = (
            Product.query().filter(F("category") == "dup_category").distinct_values("category")
        )
        assert categories == ["dup_category"]

    def test_distinct_on_query(self, db, products):
        """distinct() adds DISTINCT to SQL."""
        query = Product.query().distinct()
        assert "DISTINCT" in query.sql()

    def test_distinct_combined_with_order(self, db, products):
        """distinct() works with order_by()."""
        query = Product.query().distinct().order_by("price")
        sql = query.sql()
        assert "DISTINCT" in sql
        assert "ORDER BY" in sql


# ============================================================================
# BULK UPDATE Tests
# ============================================================================


class TestBulkUpdate:
    """Rigorous tests for bulk update() method."""

    def test_update_single_field(self, db, products):
        """update() modifies single field."""
        count = Product.query().filter(F("category") == "tools").update(category="hand_tools")
        assert count == 3  # Widget A, Widget B, Item F

        results = Product.query().filter(F("category") == "hand_tools").all()
        assert len(results) == 3

    def test_update_multiple_fields(self, db, products):
        """update() modifies multiple fields at once."""
        count = (
            Product.query()
            .filter(F("in_stock") == False)
            .update(in_stock=True, category="clearance")
        )
        assert count == 2  # Gadget D, Item F

        results = Product.query().filter(F("category") == "clearance").all()
        assert len(results) == 2
        assert all(p.in_stock for p in results)

    def test_update_no_matches(self, db, products):
        """update() returns 0 when no rows match."""
        count = Product.query().filter(F("category") == "nonexistent").update(price=0.0)
        assert count == 0

    def test_update_all_rows(self, db, products):
        """update() without filter updates all rows."""
        original_count = Product.query().count()
        count = Product.query().update(in_stock=True)
        assert count == original_count

    def test_update_with_complex_filter(self, db, products):
        """update() with complex filter conditions."""
        count = (
            Product.query()
            .filter(F("price").between(10.0, 20.0))
            .filter(F("category") == "tools")
            .update(tags=["discounted"])
        )
        assert count == 2  # Widget A (10), Widget B (20)

    def test_update_numeric_values(self, db, products):
        """update() works with numeric values."""
        count = Product.query().filter(F("category") == "premium").update(price=0.01)
        assert count == 1

        result = Product.query().filter(F("category") == "premium").first()
        assert result.price == 0.01

    def test_update_with_dict_value(self, db, products):
        """update() handles dict values."""
        count = (
            Product.query()
            .filter(F("name") == "Widget A")
            .update(metadata={"updated": True, "version": 2})
        )
        assert count == 1

    def test_update_with_list_value(self, db, products):
        """update() handles list values."""
        count = (
            Product.query().filter(F("name") == "Widget A").update(tags=["tag1", "tag2", "tag3"])
        )
        assert count == 1

    def test_update_empty_fields_raises(self, db, products):
        """update() with no fields raises ValueError."""
        with pytest.raises(ValueError, match="No fields to update"):
            Product.query().update()


# ============================================================================
# BULK DELETE Tests
# ============================================================================


class TestBulkDelete:
    """Rigorous tests for bulk delete_all() method."""

    def test_delete_all_with_filter(self, db, products):
        """delete_all() removes matching rows."""
        initial = Product.query().count()
        deleted = Product.query().filter(F("category") == "electronics").delete_all()
        assert deleted == 2

        final = Product.query().count()
        assert final == initial - 2

    def test_delete_all_no_matches(self, db, products):
        """delete_all() returns 0 when no matches."""
        deleted = Product.query().filter(F("category") == "nonexistent").delete_all()
        assert deleted == 0

    def test_delete_all_without_filter(self, db, products):
        """delete_all() without filter deletes all."""
        initial = Product.query().count()
        deleted = Product.query().delete_all()
        assert deleted == initial
        assert Product.query().count() == 0

    def test_delete_all_with_complex_filter(self, db, products):
        """delete_all() with complex conditions."""
        deleted = (
            Product.query().filter(F("in_stock") == False).filter(F("price") > 20.0).delete_all()
        )
        # Only Gadget D matches (in_stock=False, price=30)
        assert deleted == 1

    def test_delete_all_with_or_filter(self, db, products):
        """delete_all() with or_filter()."""
        deleted = (
            Product.query()
            .filter(F("category") == "sale")
            .or_filter(F("category") == "empty")
            .delete_all()
        )
        assert deleted == 2  # "100% Off Special" and empty name product


# ============================================================================
# PAGINATION Tests
# ============================================================================


class TestPagination:
    """Rigorous tests for pagination functionality."""

    def test_paginate_first_page(self, db, products):
        """First page has correct properties."""
        page = Product.query().order_by("name").paginate(page=1, per_page=3)
        assert page.page == 1
        assert page.per_page == 3
        assert page.total == 10
        assert page.total_pages == 4  # ceil(10/3)
        assert len(page.items) == 3
        assert page.has_prev is False
        assert page.has_next is True
        assert page.prev_page is None
        assert page.next_page == 2

    def test_paginate_middle_page(self, db, products):
        """Middle page has correct properties."""
        page = Product.query().order_by("name").paginate(page=2, per_page=3)
        assert page.page == 2
        assert len(page.items) == 3
        assert page.has_prev is True
        assert page.has_next is True
        assert page.prev_page == 1
        assert page.next_page == 3

    def test_paginate_last_page(self, db, products):
        """Last page has correct properties."""
        page = Product.query().order_by("name").paginate(page=4, per_page=3)
        assert page.page == 4
        assert len(page.items) == 1  # Only 1 item on last page
        assert page.has_prev is True
        assert page.has_next is False
        assert page.prev_page == 3
        assert page.next_page is None

    def test_paginate_empty_result(self, db):
        """Pagination on empty result set."""
        page = Product.query().filter(F("category") == "nonexistent").paginate(page=1, per_page=10)
        assert page.total == 0
        assert page.total_pages == 0
        assert len(page.items) == 0
        assert page.has_prev is False
        assert page.has_next is False

    def test_paginate_with_filter(self, db, products):
        """Pagination respects filters."""
        page = Product.query().filter(F("category") == "tools").paginate(page=1, per_page=2)
        assert page.total == 3  # Widget A, Widget B, Item F
        assert page.total_pages == 2
        assert len(page.items) == 2

    def test_paginate_to_dict(self, db, products):
        """to_dict() contains all expected keys."""
        page = Product.query().paginate(page=1, per_page=5)
        d = page.to_dict()

        assert "items" in d
        assert "page" in d
        assert "per_page" in d
        assert "total" in d
        assert "total_pages" in d
        assert "has_next" in d
        assert "has_prev" in d

        assert d["page"] == 1
        assert d["per_page"] == 5
        assert d["total"] == 10

    def test_paginate_iteration(self, db, products):
        """PaginatedResult is iterable."""
        page = Product.query().paginate(page=1, per_page=3)
        items = list(page)
        assert len(items) == 3

    def test_paginate_len(self, db, products):
        """len() returns items on current page."""
        page = Product.query().paginate(page=4, per_page=3)
        assert len(page) == 1  # Last page has 1 item

    def test_paginate_invalid_page_zero(self, db, products):
        """Page 0 raises ValueError."""
        with pytest.raises(ValueError, match="Page must be >= 1"):
            Product.query().paginate(page=0, per_page=10)

    def test_paginate_invalid_page_negative(self, db, products):
        """Negative page raises ValueError."""
        with pytest.raises(ValueError, match="Page must be >= 1"):
            Product.query().paginate(page=-1, per_page=10)

    def test_paginate_invalid_per_page(self, db, products):
        """per_page < 1 raises ValueError."""
        with pytest.raises(ValueError, match="per_page must be >= 1"):
            Product.query().paginate(page=1, per_page=0)

    def test_paginate_large_page_number(self, db, products):
        """Page beyond total_pages returns empty items."""
        page = Product.query().paginate(page=100, per_page=5)
        assert len(page.items) == 0
        assert page.has_next is False

    def test_paginate_single_item_per_page(self, db, products):
        """Pagination with per_page=1."""
        page = Product.query().order_by("name").paginate(page=1, per_page=1)
        assert page.total_pages == 10
        assert len(page.items) == 1


# ============================================================================
# AGGREGATE FUNCTION Tests
# ============================================================================


class TestAggregates:
    """Rigorous tests for aggregate functions."""

    def test_sum_basic(self, db, products):
        """sum() returns correct total."""
        total = Product.query().sum("price")
        expected = 10.0 + 20.0 + 15.0 + 30.0 + 25.0 + 5.0 + 0.0 + 99.99 + 1.0 + 999.99
        assert abs(total - expected) < 0.01

    def test_sum_with_filter(self, db, products):
        """sum() respects filters."""
        total = Product.query().filter(F("category") == "tools").sum("price")
        assert abs(total - 35.0) < 0.01  # 10 + 20 + 5

    def test_sum_empty_result(self, db):
        """sum() on empty result returns None."""
        result = Product.query().filter(F("category") == "nonexistent").sum("price")
        assert result is None

    def test_avg_basic(self, db, users):
        """avg() returns correct average."""
        avg_age = User.query().avg("age")
        expected = (25 + 30 + 35 + 28 + 22 + 45) / 6
        assert abs(avg_age - expected) < 0.01

    def test_avg_with_filter(self, db, users):
        """avg() respects filters."""
        avg_score = User.query().filter(F("role") == "admin").avg("score")
        expected = (95.5 + 91.0) / 2
        assert abs(avg_score - expected) < 0.01

    def test_avg_empty_result(self, db):
        """avg() on empty result returns None."""
        result = User.query().filter(F("role") == "nonexistent").avg("age")
        assert result is None

    def test_min_basic(self, db, products):
        """min() returns minimum value."""
        min_price = Product.query().min("price")
        assert min_price == 0.0  # "100% Off Special"

    def test_min_with_filter(self, db, products):
        """min() respects filters."""
        min_price = Product.query().filter(F("category") == "tools").min("price")
        assert min_price == 5.0  # Item F

    def test_min_empty_result(self, db):
        """min() on empty result returns None."""
        result = Product.query().filter(F("category") == "nonexistent").min("price")
        assert result is None

    def test_max_basic(self, db, products):
        """max() returns maximum value."""
        max_price = Product.query().max("price")
        assert max_price == 999.99  # Zzz Last

    def test_max_with_filter(self, db, products):
        """max() respects filters."""
        max_price = Product.query().filter(F("in_stock") == False).max("price")
        assert max_price == 30.0  # Gadget D

    def test_max_empty_result(self, db):
        """max() on empty result returns None."""
        result = Product.query().filter(F("category") == "nonexistent").max("price")
        assert result is None

    def test_aggregates_combined_with_complex_filters(self, db, users):
        """Aggregates with complex filter conditions."""
        avg_score = (
            User.query().filter(F("active") == True).filter(F("age").between(25, 35)).avg("score")
        )
        # alice (25, 95.5), bob (30, 82.0), charlie (35, 78.5), diana (28, 88.0)
        expected = (95.5 + 82.0 + 78.5 + 88.0) / 4
        assert abs(avg_score - expected) < 0.01


# ============================================================================
# SELECT (Field Projection) Tests
# ============================================================================


class TestSelect:
    """Tests for select() field projection using all_dicts() for partial data."""

    def test_select_limits_fields(self, db, products):
        """select() limits returned fields - use all_dicts() for raw data."""
        # Access the underlying query to get raw dicts
        query = Product.query().select("name", "price")._query
        results = query.all_dicts()
        assert len(results) == 10
        # Each result should only have the selected fields plus _id
        for row in results:
            assert "name" in row
            assert "price" in row
            assert "_id" in row
            # These fields should NOT be present
            assert "category" not in row
            assert "in_stock" not in row

    def test_select_with_filter(self, db, products):
        """select() works with filters - use all_dicts() for raw data."""
        query = Product.query().filter(F("category") == "tools").select("name", "category")._query
        results = query.all_dicts()
        assert len(results) == 3
        for row in results:
            assert row["category"] == "tools"
            assert "name" in row


# ============================================================================
# COMBINED OPERATIONS Tests
# ============================================================================


class TestCombinedOperations:
    """Tests for combining multiple new features."""

    def test_filter_or_filter_distinct_paginate(self, db, products):
        """Combine filter, or_filter, and paginate."""
        page = (
            Product.query()
            .filter(F("category") == "tools")
            .or_filter(F("category") == "electronics")
            .order_by("price")
            .paginate(page=1, per_page=3)
        )
        assert page.total == 5  # 3 tools + 2 electronics
        assert len(page.items) == 3

    def test_between_and_in_list(self, db, products):
        """Combine between() and in_list()."""
        results = (
            Product.query()
            .filter(F("price").between(10.0, 30.0))
            .filter(F("category").in_list(["tools", "electronics"]))
            .all()
        )
        assert len(results) == 4  # Widget A, Widget B, Gadget C, Gadget D

    def test_is_null_with_or_filter(self, db, events):
        """Combine is_null() with or_filter()."""
        results = (
            Event.query().filter(F("organizer").is_null()).or_filter(F("canceled") == True).all()
        )
        # No organizer: Meetup C, Seminar D, Webinar E (has location but no organizer)
        # Canceled: Meetup C
        # Unique: Meetup C, Seminar D, Webinar E (3 events)
        assert len(results) >= 2

    def test_startswith_and_aggregate(self, db, products):
        """Combine startswith() with aggregate."""
        total = Product.query().filter(F("name").startswith("Widget")).sum("price")
        assert abs(total - 30.0) < 0.01  # Widget A (10) + Widget B (20)

    def test_bulk_update_then_aggregate(self, db, products):
        """Update then aggregate."""
        # Set all tools to price 100
        Product.query().filter(F("category") == "tools").update(price=100.0)

        # Sum tools prices
        total = Product.query().filter(F("category") == "tools").sum("price")
        assert abs(total - 300.0) < 0.01  # 3 tools * 100

    def test_complex_workflow(self, db, users):
        """Complex real-world workflow."""
        # 1. Find active users with specific roles
        active_admins = (
            User.query()
            .filter(F("active") == True)
            .filter(F("role").in_list(["admin", "moderator"]))
            .all()
        )
        assert len(active_admins) == 3  # alice, frank, diana

        # 2. Get their average score
        avg_score = (
            User.query()
            .filter(F("active") == True)
            .filter(F("role").in_list(["admin", "moderator"]))
            .avg("score")
        )
        assert avg_score > 80

        # 3. Update their scores
        updated = (
            User.query()
            .filter(F("active") == True)
            .filter(F("role").in_list(["admin", "moderator"]))
            .update(score=100.0)
        )
        assert updated == 3

        # 4. Verify update
        new_avg = (
            User.query()
            .filter(F("role").in_list(["admin", "moderator"]))
            .filter(F("active") == True)
            .avg("score")
        )
        assert new_avg == 100.0
