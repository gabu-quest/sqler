"""Tests for GROUP BY / HAVING on SQLerQuery and SQLerQuerySet."""

from sqler import SQLerDB, SQLerModel
from sqler.query import SQLerField as F


class Employee(SQLerModel):
    name: str
    department: str
    role: str
    salary: float


def setup_db():
    db = SQLerDB.in_memory(shared=False)
    Employee.set_db(db)
    return db


def seed_employees():
    Employee.save_many([
        Employee(name="Alice", department="eng", role="senior", salary=150000),
        Employee(name="Bob", department="eng", role="junior", salary=90000),
        Employee(name="Charlie", department="eng", role="senior", salary=140000),
        Employee(name="Diana", department="sales", role="lead", salary=120000),
        Employee(name="Eve", department="sales", role="junior", salary=80000),
        Employee(name="Frank", department="hr", role="lead", salary=110000),
    ])


class TestGroupByOnQuery:
    def test_group_by_count(self):
        db = setup_db()
        try:
            seed_employees()
            q = db.query("employees")
            result = q.group_by("department").count()
            assert isinstance(result, list)
            by_dept = {r["department"]: r["count"] for r in result}
            assert by_dept == {"eng": 3, "sales": 2, "hr": 1}
        finally:
            db.close()

    def test_group_by_sum(self):
        db = setup_db()
        try:
            seed_employees()
            q = db.query("employees")
            result = q.group_by("department").sum("salary")
            by_dept = {r["department"]: r["sum"] for r in result}
            assert by_dept["eng"] == 380000.0
            assert by_dept["sales"] == 200000.0
            assert by_dept["hr"] == 110000.0
        finally:
            db.close()

    def test_group_by_avg(self):
        db = setup_db()
        try:
            seed_employees()
            q = db.query("employees")
            result = q.group_by("department").avg("salary")
            by_dept = {r["department"]: r["avg"] for r in result}
            assert abs(by_dept["eng"] - 126666.67) < 1
            assert by_dept["sales"] == 100000.0
            assert by_dept["hr"] == 110000.0
        finally:
            db.close()

    def test_group_by_min(self):
        db = setup_db()
        try:
            seed_employees()
            q = db.query("employees")
            result = q.group_by("department").min("salary")
            by_dept = {r["department"]: r["min"] for r in result}
            assert by_dept["eng"] == 90000
            assert by_dept["sales"] == 80000
            assert by_dept["hr"] == 110000
        finally:
            db.close()

    def test_group_by_max(self):
        db = setup_db()
        try:
            seed_employees()
            q = db.query("employees")
            result = q.group_by("department").max("salary")
            by_dept = {r["department"]: r["max"] for r in result}
            assert by_dept["eng"] == 150000
            assert by_dept["sales"] == 120000
            assert by_dept["hr"] == 110000
        finally:
            db.close()

    def test_group_by_multiple_fields(self):
        db = setup_db()
        try:
            seed_employees()
            q = db.query("employees")
            result = q.group_by("department", "role").count()
            assert isinstance(result, list)
            # eng has 2 senior, 1 junior
            lookup = {(r["department"], r["role"]): r["count"] for r in result}
            assert lookup[("eng", "senior")] == 2
            assert lookup[("eng", "junior")] == 1
            assert lookup[("sales", "lead")] == 1
            assert lookup[("sales", "junior")] == 1
            assert lookup[("hr", "lead")] == 1
        finally:
            db.close()

    def test_group_by_with_filter(self):
        db = setup_db()
        try:
            seed_employees()
            q = db.query("employees")
            result = q.filter(F("salary") >= 100000).group_by("department").count()
            by_dept = {r["department"]: r["count"] for r in result}
            assert by_dept == {"eng": 2, "sales": 1, "hr": 1}
        finally:
            db.close()

    def test_group_by_with_having(self):
        db = setup_db()
        try:
            seed_employees()
            q = db.query("employees")
            result = q.group_by("department").having(F("_count") > 1).count()
            by_dept = {r["department"]: r["count"] for r in result}
            assert "hr" not in by_dept  # hr has only 1 employee
            assert by_dept["eng"] == 3
            assert by_dept["sales"] == 2
        finally:
            db.close()

    def test_group_by_empty_table(self):
        db = setup_db()
        try:
            q = db.query("employees")
            result = q.group_by("department").count()
            assert result == []
        finally:
            db.close()

    def test_scalar_aggregates_still_work(self):
        """Ensure non-grouped aggregates still return scalars."""
        db = setup_db()
        try:
            seed_employees()
            q = db.query("employees")
            assert q.count() == 6
            assert q.sum("salary") == 690000.0
            assert q.min("salary") == 80000
            assert q.max("salary") == 150000
        finally:
            db.close()


class TestGroupByOnQuerySet:
    def test_group_by_on_queryset(self):
        db = setup_db()
        try:
            seed_employees()
            result = Employee.query().group_by("department").count()
            assert isinstance(result, list)
            by_dept = {r["department"]: r["count"] for r in result}
            assert by_dept == {"eng": 3, "sales": 2, "hr": 1}
        finally:
            db.close()

    def test_group_by_sum_on_queryset(self):
        db = setup_db()
        try:
            seed_employees()
            result = Employee.query().group_by("department").sum("salary")
            by_dept = {r["department"]: r["sum"] for r in result}
            assert by_dept["eng"] == 380000.0
        finally:
            db.close()

    def test_group_by_with_filter_on_queryset(self):
        db = setup_db()
        try:
            seed_employees()
            result = (
                Employee.query()
                .filter(F("role") == "senior")
                .group_by("department")
                .count()
            )
            by_dept = {r["department"]: r["count"] for r in result}
            assert by_dept == {"eng": 2}
        finally:
            db.close()

    def test_group_by_having_on_queryset(self):
        db = setup_db()
        try:
            seed_employees()
            result = (
                Employee.query()
                .group_by("department")
                .having(F("_count") >= 2)
                .count()
            )
            depts = {r["department"] for r in result}
            assert depts == {"eng", "sales"}
        finally:
            db.close()


class TestGroupByWithPromotedFields:
    def test_group_by_promoted_field(self):
        class Product(SQLerModel):
            __promoted__ = {"category": "TEXT"}
            name: str
            category: str
            price: float

        db = SQLerDB.in_memory(shared=False)
        try:
            Product.set_db(db)
            Product.save_many([
                Product(name="Widget", category="tools", price=10.0),
                Product(name="Gadget", category="tools", price=20.0),
                Product(name="Phone", category="electronics", price=500.0),
            ])
            result = Product.query().group_by("category").sum("price")
            by_cat = {r["category"]: r["sum"] for r in result}
            assert by_cat["tools"] == 30.0
            assert by_cat["electronics"] == 500.0
        finally:
            db.close()
