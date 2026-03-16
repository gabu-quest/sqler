"""Tests for async GROUP BY / HAVING."""

import pytest
import pytest_asyncio

from sqler import AsyncSQLerDB, AsyncSQLerModel
from sqler.query import SQLerField as F


class Employee(AsyncSQLerModel):
    name: str
    department: str
    role: str
    salary: float


@pytest_asyncio.fixture
async def db():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    Employee.set_db(db)
    await Employee._ensure_schema()
    yield db
    await db.close()


async def seed_employees():
    await Employee.asave_many([
        Employee(name="Alice", department="eng", role="senior", salary=150000),
        Employee(name="Bob", department="eng", role="junior", salary=90000),
        Employee(name="Charlie", department="eng", role="senior", salary=140000),
        Employee(name="Diana", department="sales", role="lead", salary=120000),
        Employee(name="Eve", department="sales", role="junior", salary=80000),
        Employee(name="Frank", department="hr", role="lead", salary=110000),
    ])


@pytest.mark.asyncio
async def test_group_by_count(db):
    await seed_employees()
    result = await Employee.query().group_by("department").count()
    assert isinstance(result, list)
    by_dept = {r["department"]: r["count"] for r in result}
    assert by_dept == {"eng": 3, "sales": 2, "hr": 1}


@pytest.mark.asyncio
async def test_group_by_sum(db):
    await seed_employees()
    result = await Employee.query().group_by("department").sum("salary")
    by_dept = {r["department"]: r["sum"] for r in result}
    assert by_dept["eng"] == 380000.0
    assert by_dept["sales"] == 200000.0
    assert by_dept["hr"] == 110000.0


@pytest.mark.asyncio
async def test_group_by_with_filter(db):
    await seed_employees()
    result = (
        await Employee.query()
        .filter(F("salary") >= 100000)
        .group_by("department")
        .count()
    )
    by_dept = {r["department"]: r["count"] for r in result}
    assert by_dept == {"eng": 2, "sales": 1, "hr": 1}


@pytest.mark.asyncio
async def test_group_by_with_having(db):
    await seed_employees()
    result = (
        await Employee.query()
        .group_by("department")
        .having(F("_count") > 1)
        .count()
    )
    by_dept = {r["department"]: r["count"] for r in result}
    assert "hr" not in by_dept
    assert by_dept["eng"] == 3
    assert by_dept["sales"] == 2


@pytest.mark.asyncio
async def test_group_by_multiple_fields(db):
    await seed_employees()
    result = await Employee.query().group_by("department", "role").count()
    lookup = {(r["department"], r["role"]): r["count"] for r in result}
    assert lookup[("eng", "senior")] == 2
    assert lookup[("eng", "junior")] == 1


@pytest.mark.asyncio
async def test_scalar_aggregates_still_work(db):
    """Ensure non-grouped aggregates still return scalars."""
    await seed_employees()
    assert await Employee.query().count() == 6
    assert await Employee.query().sum("salary") == 690000.0


@pytest.mark.asyncio
async def test_group_by_empty_table(db):
    result = await Employee.query().group_by("department").count()
    assert result == []
