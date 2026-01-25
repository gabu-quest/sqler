"""Tests for marimo tour notebooks.

These tests ensure that tour notebooks:
1. Execute without errors
2. Produce correct output for key examples

This prevents issues like using wrong methods (e.g., contains() for string
substring matching instead of like()) from going unnoticed.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F

# Path to examples directory
EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


# ---------------- Notebook Execution Tests ----------------


def get_tour_notebooks():
    """Get all tour notebook paths."""
    notebooks = []
    for pattern in ["tour_*.py", "ja/tour_*.py", "lite/tour_*_lite.py"]:
        notebooks.extend(EXAMPLES_DIR.glob(pattern))
    return sorted(notebooks)


@pytest.mark.parametrize("notebook_path", get_tour_notebooks(), ids=lambda p: p.name)
def test_tour_notebook_executes(notebook_path):
    """Each tour notebook should execute without errors."""
    result = subprocess.run(
        [sys.executable, str(notebook_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"Notebook {notebook_path.name} failed:\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )


# ---------------- Output Validation Tests ----------------
# These tests verify that key examples in the tour produce correct output


class TourUser(SQLerModel):
    """User model matching tour_01_fundamentals setup."""

    _table = "tour_users"
    name: str
    email: str
    age: int = 0
    is_active: bool = True


@pytest.fixture
def tour_db():
    """Create database with tour_01 user data."""
    db = SQLerDB.in_memory()
    TourUser.set_db(db)

    # Same data as tour_01_fundamentals
    TourUser(name="Alice", email="alice@example.com", age=30).save()
    TourUser(name="Bob", email="bob@example.com", age=25).save()
    # Note: Charlie is deleted in the tour, so we skip him
    TourUser(name="Diana", email="diana@example.com", age=28).save()
    TourUser(name="Eve", email="eve@corp.com", age=32).save()
    TourUser(name="Frank", email="frank@corp.com", age=28, is_active=False).save()

    yield db
    db.close()


class TestTour01FundamentalsOutput:
    """Validate key outputs from tour_01_fundamentals."""

    def test_filter_email_substring_corp(self, tour_db):
        """Email substring filter for 'corp' should find Eve and Frank.

        This test catches the bug where contains() was incorrectly used
        for string substring matching instead of like().
        """
        corp_users = TourUser.query().filter(F("email").like("%corp%")).all()
        names = sorted([u.name for u in corp_users])
        assert names == ["Eve", "Frank"], (
            f"Expected ['Eve', 'Frank'] for corp email filter, got {names}. "
            "Make sure like() is used for string substring matching, not contains()."
        )

    def test_exclude_email_substring_corp(self, tour_db):
        """Exclude corp emails should find Alice, Bob, Diana."""
        not_corp = TourUser.query().exclude(F("email").like("%corp%")).all()
        names = sorted([u.name for u in not_corp])
        assert names == ["Alice", "Bob", "Diana"]

    def test_first_corp_user(self, tour_db):
        """First corp user query should return a user (Eve or Frank)."""
        first_corp = TourUser.query().filter(F("email").like("%corp%")).first()
        assert first_corp is not None, "Should find at least one corp user"
        assert first_corp.name in ["Eve", "Frank"]

    def test_filter_age_greater_than_28(self, tour_db):
        """Users older than 28 should be Alice (30) and Eve (32)."""
        older = TourUser.query().filter(F("age") > 28).all()
        names = sorted([u.name for u in older])
        assert names == ["Alice", "Eve"]

    def test_filter_age_28_or_younger(self, tour_db):
        """Users 28 or younger should be Bob (25), Diana (28), Frank (28)."""
        young = TourUser.query().filter(F("age") <= 28).all()
        names = sorted([u.name for u in young])
        assert names == ["Bob", "Diana", "Frank"]

    def test_filter_active_users(self, tour_db):
        """Active users (is_active=True)."""
        active = TourUser.query().filter(F("is_active") == True).all()
        names = sorted([u.name for u in active])
        # Frank is not active
        assert "Frank" not in names
        assert "Alice" in names

    def test_filter_age_exact_28(self, tour_db):
        """Users aged exactly 28 should be Diana and Frank."""
        users_28 = TourUser.query().filter(F("age") == 28).all()
        names = sorted([u.name for u in users_28])
        assert names == ["Diana", "Frank"]

    def test_filter_name_startswith_a(self, tour_db):
        """Names starting with A should be Alice."""
        a_names = TourUser.query().filter(F("name").startswith("A")).all()
        names = [u.name for u in a_names]
        assert names == ["Alice"]

    def test_filter_age_between_27_30(self, tour_db):
        """Age between 27-30 inclusive: Diana (28), Frank (28), Alice (30)."""
        age_range = TourUser.query().filter(F("age").between(27, 30)).all()
        names = sorted([u.name for u in age_range])
        assert names == ["Alice", "Diana", "Frank"]

    def test_filter_age_in_list(self, tour_db):
        """Age in [28, 32]: Diana (28), Eve (32), Frank (28)."""
        specific = TourUser.query().filter(F("age").in_list([28, 32])).all()
        names = sorted([u.name for u in specific])
        assert names == ["Diana", "Eve", "Frank"]

    def test_combined_active_and_young(self, tour_db):
        """Active AND under 30: Bob (25), Diana (28)."""
        result = (
            TourUser.query()
            .filter(F("is_active") == True)
            .filter(F("age") < 30)
            .all()
        )
        names = sorted([u.name for u in result])
        assert names == ["Bob", "Diana"]

    def test_order_by_age_asc(self, tour_db):
        """Order by age ascending."""
        users = TourUser.query().order_by("age").all()
        ages = [u.age for u in users]
        assert ages == sorted(ages), "Ages should be in ascending order"

    def test_order_by_age_desc(self, tour_db):
        """Order by age descending."""
        users = TourUser.query().order_by("age", desc=True).all()
        ages = [u.age for u in users]
        assert ages == sorted(ages, reverse=True), "Ages should be in descending order"

    def test_limit_top_2_oldest(self, tour_db):
        """Top 2 oldest users."""
        top2 = TourUser.query().order_by("age", desc=True).limit(2).all()
        assert len(top2) == 2
        names = [u.name for u in top2]
        assert names[0] == "Eve"  # 32
        assert names[1] == "Alice"  # 30

    def test_aggregations(self, tour_db):
        """Test count, sum, avg, min, max."""
        assert TourUser.query().count() == 5

        total_age = TourUser.query().sum("age")
        assert total_age == 30 + 25 + 28 + 32 + 28  # 143

        min_age = TourUser.query().min("age")
        assert min_age == 25

        max_age = TourUser.query().max("age")
        assert max_age == 32

    def test_exists(self, tour_db):
        """Test exists check."""
        assert TourUser.query().filter(F("name") == "Alice").exists()
        assert not TourUser.query().filter(F("name") == "Nobody").exists()

    def test_distinct_ages(self, tour_db):
        """Distinct ages: 25, 28, 30, 32."""
        ages = TourUser.query().distinct_values("age")
        assert sorted(ages) == [25, 28, 30, 32]


# ---------------- contains() vs like() Documentation Test ----------------


def test_contains_is_for_arrays_not_strings():
    """Document that contains() is for arrays, like() is for string substrings.

    This test explicitly documents the difference to prevent future confusion.
    """
    db = SQLerDB.in_memory()

    class Product(SQLerModel):
        _table = "products"
        name: str
        tags: list[str] = []

    Product.set_db(db)

    # Product with tags array
    Product(name="Laptop", tags=["electronics", "computers", "pro"]).save()
    Product(name="Mouse", tags=["electronics", "accessories"]).save()

    # contains() works on ARRAYS - checks if array contains element
    electronics = Product.query().filter(F("tags").contains("electronics")).all()
    assert len(electronics) == 2

    pro_items = Product.query().filter(F("tags").contains("pro")).all()
    assert len(pro_items) == 1
    assert pro_items[0].name == "Laptop"

    # For STRING substring matching, use like()
    class User(SQLerModel):
        _table = "users"
        email: str

    User.set_db(db)
    User(email="alice@corp.com").save()
    User(email="bob@example.com").save()

    # CORRECT: like() for string substring
    corp_users = User.query().filter(F("email").like("%corp%")).all()
    assert len(corp_users) == 1
    assert corp_users[0].email == "alice@corp.com"

    # WRONG: contains() on string field does NOT do substring matching
    # It treats the scalar as a single-element array and checks equality
    wrong_result = User.query().filter(F("email").contains("corp")).all()
    assert len(wrong_result) == 0  # This returns empty - NOT what you want!

    db.close()
