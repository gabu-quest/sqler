"""Tests for multi-field order_by support."""

import pytest
from sqler import SQLerDB, SQLerModel, F


# ---- SQL generation tests (no DB needed) ----


class TestMultiOrderSQL:
    """Test ORDER BY SQL generation without hitting a real DB."""

    def test_single_field_backward_compat(self, dummy_adapter):
        from sqler.query.query import SQLerQuery

        q = SQLerQuery("jobs", adapter=dummy_adapter).order_by("name", desc=True)
        sql = q.sql
        assert "ORDER BY json_extract(data, '$.name') DESC" in sql

    def test_single_field_asc(self, dummy_adapter):
        from sqler.query.query import SQLerQuery

        q = SQLerQuery("jobs", adapter=dummy_adapter).order_by("name")
        sql = q.sql
        assert "ORDER BY json_extract(data, '$.name')" in sql
        assert "DESC" not in sql

    def test_single_field_dash_prefix(self, dummy_adapter):
        from sqler.query.query import SQLerQuery

        q = SQLerQuery("jobs", adapter=dummy_adapter).order_by("-name")
        sql = q.sql
        assert "ORDER BY json_extract(data, '$.name') DESC" in sql

    def test_multi_field_mixed(self, dummy_adapter):
        from sqler.query.query import SQLerQuery

        q = SQLerQuery("jobs", adapter=dummy_adapter).order_by(
            "-priority", "eta", "ulid"
        )
        sql = q.sql
        assert "ORDER BY" in sql
        assert "json_extract(data, '$.priority') DESC" in sql
        assert "json_extract(data, '$.eta')" in sql
        assert "json_extract(data, '$.ulid')" in sql
        # Check ordering within the SQL: priority DESC comes first
        pri_idx = sql.index("priority")
        eta_idx = sql.index("eta")
        ulid_idx = sql.index("ulid")
        assert pri_idx < eta_idx < ulid_idx

    def test_multi_field_all_desc(self, dummy_adapter):
        from sqler.query.query import SQLerQuery

        q = SQLerQuery("jobs", adapter=dummy_adapter).order_by("-a", "-b")
        sql = q.sql
        assert "json_extract(data, '$.a') DESC" in sql
        assert "json_extract(data, '$.b') DESC" in sql

    def test_multi_field_all_asc(self, dummy_adapter):
        from sqler.query.query import SQLerQuery

        q = SQLerQuery("jobs", adapter=dummy_adapter).order_by("a", "b", "c")
        sql = q.sql
        assert "DESC" not in sql
        assert "json_extract(data, '$.a')" in sql
        assert "json_extract(data, '$.b')" in sql
        assert "json_extract(data, '$.c')" in sql

    def test_empty_order_by_noop(self, dummy_adapter):
        from sqler.query.query import SQLerQuery

        q = SQLerQuery("jobs", adapter=dummy_adapter).order_by()
        sql = q.sql
        assert "ORDER BY" not in sql

    def test_clone_preserves_order_fields(self, dummy_adapter):
        from sqler.query.query import SQLerQuery

        q = SQLerQuery("jobs", adapter=dummy_adapter).order_by("-priority", "eta")
        q2 = q.filter(F("status") == "pending")
        assert q2._order_fields == [("priority", True), ("eta", False)]
        assert "ORDER BY" in q2.sql


# ---- Execution tests (real SQLite) ----


class Job(SQLerModel):
    name: str = ""
    priority: int = 0
    eta: int = 0


@pytest.fixture
def db_with_jobs():
    db = SQLerDB.in_memory(shared=False)
    Job.set_db(db, "jobs")
    Job(name="low-late", priority=1, eta=100).save()
    Job(name="high-early", priority=10, eta=10).save()
    Job(name="high-late", priority=10, eta=100).save()
    Job(name="low-early", priority=1, eta=10).save()
    yield db
    db.close()


class TestMultiOrderExecution:
    def test_single_field_execution(self, db_with_jobs):
        results = Job.query().order_by("priority").all()
        priorities = [j.priority for j in results]
        assert priorities == sorted(priorities)

    def test_single_field_desc_execution(self, db_with_jobs):
        results = Job.query().order_by("priority", desc=True).all()
        priorities = [j.priority for j in results]
        assert priorities == sorted(priorities, reverse=True)

    def test_multi_field_execution(self, db_with_jobs):
        results = Job.query().order_by("-priority", "eta").all()
        names = [j.name for j in results]
        # priority DESC (10 first, then 1), then eta ASC within same priority
        assert names == ["high-early", "high-late", "low-early", "low-late"]

    def test_multi_field_both_asc(self, db_with_jobs):
        results = Job.query().order_by("priority", "eta").all()
        names = [j.name for j in results]
        assert names == ["low-early", "low-late", "high-early", "high-late"]

    def test_multi_field_both_desc(self, db_with_jobs):
        results = Job.query().order_by("-priority", "-eta").all()
        names = [j.name for j in results]
        assert names == ["high-late", "high-early", "low-late", "low-early"]

    def test_dash_prefix_single(self, db_with_jobs):
        results = Job.query().order_by("-priority").all()
        priorities = [j.priority for j in results]
        assert priorities == sorted(priorities, reverse=True)
