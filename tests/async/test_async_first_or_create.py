"""Tests for async first_or_create."""

import pytest
import pytest_asyncio

from sqler import AsyncSQLerDB, AsyncSQLerModel


class User(AsyncSQLerModel):
    name: str
    email: str
    role: str = "user"


@pytest_asyncio.fixture
async def db():
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    User.set_db(db)
    await User._ensure_schema()
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_creates_when_not_found(db):
    user, created = await User.first_or_create(
        lookup={"email": "alice@example.com"},
        defaults={"name": "Alice", "role": "admin"},
    )
    assert created is True
    assert user.email == "alice@example.com"
    assert user.name == "Alice"
    assert user.role == "admin"
    assert user._id is not None


@pytest.mark.asyncio
async def test_finds_existing(db):
    original = User(name="Bob", email="bob@example.com", role="user")
    await original.save()

    user, created = await User.first_or_create(
        lookup={"email": "bob@example.com"},
        defaults={"name": "Robert", "role": "admin"},
    )
    assert created is False
    assert user.email == "bob@example.com"
    assert user.name == "Bob"
    assert user.role == "user"
    assert user._id == original._id


@pytest.mark.asyncio
async def test_idempotent(db):
    user1, created1 = await User.first_or_create(
        lookup={"email": "carol@example.com"},
        defaults={"name": "Carol"},
    )
    user2, created2 = await User.first_or_create(
        lookup={"email": "carol@example.com"},
        defaults={"name": "Different"},
    )
    assert created1 is True
    assert created2 is False
    assert user1._id == user2._id
    assert user2.name == "Carol"
    assert await User.query().count() == 1


@pytest.mark.asyncio
async def test_with_db_parameter(db):
    user, created = await User.first_or_create(
        lookup={"email": "dan@example.com"},
        defaults={"name": "Dan"},
        db=db,
    )
    assert created is True
    assert user.name == "Dan"
