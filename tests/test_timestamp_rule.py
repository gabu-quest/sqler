"""The timestamp rule: first save sets created_at == updated_at; every later save
gives a strictly later updated_at, even when the clock has not advanced or moved back."""

from datetime import datetime, timedelta, timezone

import pytest
from sqler import SQLerDB, SQLerModel
from sqler.models import mixins
from sqler.models.mixins import AsyncAuditMixin, AuditMixin, TimestampMixin

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, value):
        self.value = value


@pytest.fixture
def clock(monkeypatch):
    c = _Clock(T0)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return c.value

    monkeypatch.setattr(mixins, "datetime", FrozenDatetime)
    return c


class _TS(TimestampMixin):
    pass


class _Audit(AuditMixin):
    pass


class _AsyncAudit(AsyncAuditMixin):
    pass


def _stamp(obj):
    if isinstance(obj, TimestampMixin):
        obj._set_timestamps()
    else:
        obj._set_audit_fields()


KINDS = [_TS, _Audit, _AsyncAudit]


@pytest.mark.parametrize("kind", KINDS)
def test_first_save_equal(clock, kind):
    obj = kind()
    _stamp(obj)
    assert obj.created_at == T0
    assert obj.updated_at == T0


@pytest.mark.parametrize("kind", KINDS)
def test_second_save_same_tick_strictly_later(clock, kind):
    obj = kind()
    _stamp(obj)
    first = obj.updated_at
    _stamp(obj)
    assert obj.updated_at == first + timedelta(microseconds=1)
    assert obj.created_at == T0


@pytest.mark.parametrize("kind", KINDS)
def test_three_saves_same_tick_strictly_increasing(clock, kind):
    obj = kind()
    seen = []
    for _ in range(3):
        _stamp(obj)
        seen.append(obj.updated_at)
    assert seen == [T0, T0 + timedelta(microseconds=1), T0 + timedelta(microseconds=2)]


@pytest.mark.parametrize("kind", KINDS)
def test_clock_backwards_still_strictly_later(clock, kind):
    obj = kind()
    _stamp(obj)
    _stamp(obj)
    before = obj.updated_at
    clock.value = T0 - timedelta(seconds=5)
    _stamp(obj)
    assert obj.updated_at == before + timedelta(microseconds=1)


@pytest.mark.parametrize("kind", KINDS)
def test_clock_advancing_uses_now(clock, kind):
    obj = kind()
    _stamp(obj)
    clock.value = T0 + timedelta(seconds=5)
    _stamp(obj)
    assert obj.updated_at == T0 + timedelta(seconds=5)


@pytest.mark.parametrize("kind", KINDS)
def test_caller_set_created_at_kept(clock, kind):
    obj = kind()
    obj.created_at = T0 - timedelta(days=1)
    _stamp(obj)
    assert obj.created_at == T0 - timedelta(days=1)
    assert obj.updated_at == T0


@pytest.mark.parametrize("kind", KINDS)
def test_caller_created_at_in_future_gives_one_microsecond_after(clock, kind):
    obj = kind()
    future = T0 + timedelta(days=1)
    obj.created_at = future
    _stamp(obj)
    assert obj.created_at == future
    assert obj.updated_at == future + timedelta(microseconds=1)


@pytest.mark.parametrize("kind", KINDS)
def test_loaded_row_with_updated_at_missing(clock, kind):
    obj = kind()
    obj.created_at = T0 - timedelta(hours=1)
    obj.updated_at = None
    _stamp(obj)
    assert obj.updated_at == T0
    assert obj.created_at == T0 - timedelta(hours=1)


@pytest.mark.parametrize("kind", KINDS)
def test_stored_updated_at_in_future_still_advances(clock, kind):
    obj = kind()
    obj.created_at = T0 - timedelta(hours=1)
    obj.updated_at = T0 + timedelta(hours=1)
    _stamp(obj)
    assert obj.updated_at == T0 + timedelta(hours=1, microseconds=1)


@pytest.mark.parametrize("kind", KINDS)
def test_naive_created_at_today(clock, kind):
    """Documents today's behaviour: a naive created_at raises TypeError (compared with aware now)."""
    obj = kind()
    obj.created_at = datetime(2020, 1, 1)
    with pytest.raises(TypeError):
        _stamp(obj)


@pytest.fixture
def db():
    """Create an in-memory database for testing."""
    return SQLerDB.in_memory()

class TestTimestampMixin:
    """Tests for TimestampMixin functionality."""

    def test_timestamps_set_on_save(self, db):
        """Test that timestamps are set automatically on save()."""

        class User(TimestampMixin, SQLerModel):
            name: str

        User.set_db(db)

        # save() should automatically set timestamps
        user = User(name="Alice").save()

        assert user.created_at is not None
        assert user.updated_at is not None
        assert user.created_at == user.updated_at

        # Verify persisted to database
        reloaded = User.from_id(user._id)
        assert reloaded.created_at is not None
        assert reloaded.updated_at is not None

    def test_updated_at_changes_on_update(self, db):
        """Test that updated_at changes on subsequent saves."""
        import time

        class User(TimestampMixin, SQLerModel):
            name: str

        User.set_db(db)

        user = User(name="Alice").save()
        first_created = user.created_at
        first_updated = user.updated_at

        time.sleep(0.01)  # Small delay

        user.name = "Alice Updated"
        user.save()

        assert user.updated_at > first_updated
        assert user.created_at == first_created  # created_at unchanged
