"""Tests for first_or_create on all model backends."""

from dataclasses import dataclass

import pytest
from sqler import SQLerDB, SQLerLiteModel, SQLerModel
from sqler.query import SQLerField as F


class User(SQLerModel):
    name: str
    email: str
    role: str = "user"


@dataclass
class LiteUser(SQLerLiteModel):
    __tablename__ = "lite_users"
    name: str = ""
    email: str = ""
    role: str = "user"


def setup_db():
    db = SQLerDB.in_memory(shared=False)
    User.set_db(db)
    LiteUser.set_db(db)
    return db


class TestFirstOrCreatePydantic:
    def test_creates_when_not_found(self):
        db = setup_db()
        try:
            user, created = User.first_or_create(
                lookup={"email": "alice@example.com"},
                defaults={"name": "Alice", "role": "admin"},
            )
            assert created is True
            assert user.email == "alice@example.com"
            assert user.name == "Alice"
            assert user.role == "admin"
            assert user._id is not None
        finally:
            db.close()

    def test_finds_existing(self):
        db = setup_db()
        try:
            # Seed
            original = User(name="Bob", email="bob@example.com", role="user")
            original.save()

            user, created = User.first_or_create(
                lookup={"email": "bob@example.com"},
                defaults={"name": "Robert", "role": "admin"},
            )
            assert created is False
            assert user.email == "bob@example.com"
            assert user.name == "Bob"  # original name preserved
            assert user.role == "user"  # original role preserved
            assert user._id == original._id
        finally:
            db.close()

    def test_with_defaults(self):
        db = setup_db()
        try:
            user, created = User.first_or_create(
                lookup={"email": "carol@example.com"},
                defaults={"name": "Carol"},
            )
            assert created is True
            assert user.name == "Carol"
            assert user.role == "user"  # model default
        finally:
            db.close()

    def test_without_defaults(self):
        db = setup_db()
        try:
            user, created = User.first_or_create(
                lookup={"email": "dan@example.com", "name": "Dan"},
            )
            assert created is True
            assert user.email == "dan@example.com"
            assert user.name == "Dan"
        finally:
            db.close()

    def test_with_db_parameter(self):
        db = SQLerDB.in_memory(shared=False)
        try:
            # Use .using() path via db parameter
            User.set_db(db)
            user, created = User.first_or_create(
                lookup={"email": "eve@example.com"},
                defaults={"name": "Eve"},
                db=db,
            )
            assert created is True
            assert user.name == "Eve"
        finally:
            db.close()

    def test_idempotent(self):
        db = setup_db()
        try:
            user1, created1 = User.first_or_create(
                lookup={"email": "frank@example.com"},
                defaults={"name": "Frank"},
            )
            user2, created2 = User.first_or_create(
                lookup={"email": "frank@example.com"},
                defaults={"name": "Different"},
            )
            assert created1 is True
            assert created2 is False
            assert user1._id == user2._id
            assert user2.name == "Frank"  # defaults NOT applied on find
            assert User.query().count() == 1
        finally:
            db.close()


class TestFirstOrCreateLite:
    def test_creates_when_not_found(self):
        db = setup_db()
        try:
            user, created = LiteUser.first_or_create(
                lookup={"email": "alice@lite.com"},
                defaults={"name": "Alice"},
            )
            assert created is True
            assert user.email == "alice@lite.com"
            assert user.name == "Alice"
            assert user._id is not None
        finally:
            db.close()

    def test_finds_existing(self):
        db = setup_db()
        try:
            original = LiteUser(name="Bob", email="bob@lite.com")
            original.save()

            user, created = LiteUser.first_or_create(
                lookup={"email": "bob@lite.com"},
                defaults={"name": "Robert"},
            )
            assert created is False
            assert user.name == "Bob"
            assert user._id == original._id
        finally:
            db.close()

    def test_idempotent(self):
        db = setup_db()
        try:
            user1, created1 = LiteUser.first_or_create(
                lookup={"email": "carol@lite.com"},
                defaults={"name": "Carol"},
            )
            user2, created2 = LiteUser.first_or_create(
                lookup={"email": "carol@lite.com"},
                defaults={"name": "Different"},
            )
            assert created1 is True
            assert created2 is False
            assert user1._id == user2._id
            assert user2.name == "Carol"
            assert LiteUser.query().filter(F("email") == "carol@lite.com").count() == 1
        finally:
            db.close()


class TestFirstOrCreateMsgspec:
    def test_msgspec_model(self):
        msgspec = pytest.importorskip("msgspec")
        from sqler import SQLerMsgspecModel

        class MsgUser(SQLerMsgspecModel, tag=False):
            __tablename__ = "msg_users"
            name: str = ""
            email: str = ""
            role: str = "user"

        db = SQLerDB.in_memory(shared=False)
        try:
            MsgUser.set_db(db)

            user, created = MsgUser.first_or_create(
                lookup={"email": "alice@msg.com"},
                defaults={"name": "Alice"},
            )
            assert created is True
            assert user.email == "alice@msg.com"
            assert user.name == "Alice"

            user2, created2 = MsgUser.first_or_create(
                lookup={"email": "alice@msg.com"},
                defaults={"name": "Different"},
            )
            assert created2 is False
            assert user2.name == "Alice"
        finally:
            db.close()
