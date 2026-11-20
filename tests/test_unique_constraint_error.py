"""A unique index that refuses a row raises ``UniqueConstraintError`` (sync adapter and SQLerDB).

The error is also a ``sqlite3.IntegrityError``, names what SQLite named, and has the
driver error as its cause. Other integrity errors stay what they are.
"""

import sqlite3

import pytest

from sqler import SQLerDB, SQLerModel, SQLerSafeModel, UniqueConstraintError


@pytest.fixture
def db():
    d = SQLerDB.in_memory(shared=False)
    yield d
    d.close()


@pytest.fixture
def emails(db):
    """Table ``users`` with a unique index on ``$.email`` and one row ``a@x``."""
    db.create_index("users", "email", unique=True)
    db.insert_document("users", {"email": "a@x"})
    return db


def _count(db, table):
    return db.adapter.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TestRaisedFromEveryWrite:
    def test_insert_document(self, emails):
        with pytest.raises(UniqueConstraintError) as info:
            emails.insert_document("users", {"email": "a@x"})
        err = info.value
        assert isinstance(err, sqlite3.IntegrityError)
        assert isinstance(err.__cause__, sqlite3.IntegrityError)
        assert not isinstance(err.__cause__, UniqueConstraintError)
        assert str(err) == "UNIQUE constraint failed: index 'idx_users_email'"
        assert err.index == "idx_users_email"
        assert err.table is None
        assert err.columns == []
        assert err.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"

    def test_upsert_document_update(self, emails):
        second = emails.insert_document("users", {"email": "b@x"})
        with pytest.raises(UniqueConstraintError):
            emails.upsert_document("users", second, {"email": "a@x"})

    def test_raw_adapter_execute(self, db):
        db.adapter.execute("CREATE TABLE t (v TEXT UNIQUE)")
        db.adapter.execute("INSERT INTO t (v) VALUES ('dup')")
        db.adapter.commit()
        with pytest.raises(UniqueConstraintError) as info:
            db.adapter.execute("INSERT INTO t (v) VALUES ('dup')")
        assert info.value.table == "t"
        assert info.value.columns == ["v"]
        assert info.value.field == "v"
        assert info.value.index is None

    def test_executemany(self, db):
        db.adapter.execute("CREATE TABLE t (v TEXT UNIQUE)")
        db.adapter.commit()
        with pytest.raises(UniqueConstraintError):
            db.adapter.executemany("INSERT INTO t (v) VALUES (?)", [["a"], ["a"]])
        assert _count(db, "t") == 0

    def test_executescript(self, db):
        with pytest.raises(UniqueConstraintError):
            db.adapter.executescript(
                "CREATE TABLE t (v TEXT UNIQUE); INSERT INTO t VALUES ('a'); INSERT INTO t VALUES ('a');"
            )

    def test_two_column_index(self, db):
        db.adapter.execute("CREATE TABLE t (a INTEGER, b INTEGER, UNIQUE (a, b))")
        db.adapter.execute("INSERT INTO t VALUES (1, 2)")
        db.adapter.commit()
        with pytest.raises(UniqueConstraintError) as info:
            db.adapter.execute("INSERT INTO t VALUES (1, 2)")
        assert info.value.table == "t"
        assert info.value.columns == ["a", "b"]
        assert info.value.field is None

    def test_promoted_write(self, db):
        db._ensure_table_with_promoted("jobs", {"ulid": "TEXT NOT NULL DEFAULT ''"})
        db.adapter.execute("CREATE UNIQUE INDEX jobs_ulid ON jobs (ulid)")
        db.upsert_document_promoted("jobs", None, {"ulid": "u1"}, ["ulid"])
        with pytest.raises(UniqueConstraintError) as info:
            db.upsert_document_promoted("jobs", None, {"ulid": "u1"}, ["ulid"])
        assert (info.value.table, info.value.columns) == ("jobs", ["ulid"])
        with pytest.raises(UniqueConstraintError):
            db.insert_many_promoted("jobs", [{"ulid": "u1"}], ["ulid"])

    def test_versioned_writes(self, db):
        db._ensure_table_with_promoted("jobs", {"ulid": "TEXT NOT NULL DEFAULT ''"})
        db._ensure_versioned_table("jobs")
        db.adapter.execute("CREATE UNIQUE INDEX jobs_ulid ON jobs (ulid)")
        db.upsert_with_version_promoted("jobs", None, {"ulid": "u1"}, None, ["ulid"])
        other, _ = db.upsert_with_version_promoted("jobs", None, {"ulid": "u2"}, None, ["ulid"])
        with pytest.raises(UniqueConstraintError):
            db.upsert_with_version_promoted("jobs", None, {"ulid": "u1"}, None, ["ulid"])
        with pytest.raises(UniqueConstraintError):
            db.upsert_with_version_promoted("jobs", other, {"ulid": "u1"}, 0, ["ulid"])
        # the failed versioned UPDATE was rolled back: the connection holds no transaction
        assert db.adapter._conn().in_transaction is False
        assert db.find_document_with_version("jobs", other)["ulid"] == "u2"

    def test_upsert_with_version(self, db):
        db.create_index("vs", "k", unique=True)
        db.upsert_with_version("vs", None, {"k": 1}, None)
        other, _ = db.upsert_with_version("vs", None, {"k": 2}, None)
        with pytest.raises(UniqueConstraintError):
            db.upsert_with_version("vs", other, {"k": 1}, 0)

    def test_model_save(self, db):
        class Person(SQLerModel):
            email: str

        Person.set_db(db, "people")
        db.create_index("people", "email", unique=True)
        Person(email="a@x").save()
        with pytest.raises(UniqueConstraintError):
            Person(email="a@x").save()

    def test_safe_model_save_is_not_retried(self, db):
        class SafePerson(SQLerSafeModel):
            email: str

        SafePerson.set_db(db, "safe_people")
        db.create_index("safe_people", "email", unique=True)
        SafePerson(email="a@x").save()
        second = SafePerson(email="b@x").save()
        second.email = "a@x"
        with pytest.raises(UniqueConstraintError):
            SafePerson(email="a@x").save()
        with pytest.raises(UniqueConstraintError):
            second.save()


class TestCompatibility:
    def test_except_driver_error_still_catches(self, emails):
        caught = None
        try:
            emails.insert_document("users", {"email": "a@x"})
        except sqlite3.IntegrityError as exc:
            caught = exc
        assert type(caught) is UniqueConstraintError

    def test_primary_key_violation(self, db):
        db.adapter.execute("CREATE TABLE t (k TEXT PRIMARY KEY)")
        db.adapter.execute("INSERT INTO t VALUES ('a')")
        db.adapter.commit()
        with pytest.raises(UniqueConstraintError) as info:
            db.adapter.execute("INSERT INTO t VALUES ('a')")
        assert info.value.columns == ["k"]

    @pytest.mark.parametrize(
        "ddl,insert,message",
        [
            (
                "CREATE TABLE t (v TEXT NOT NULL)",
                "INSERT INTO t VALUES (NULL)",
                "NOT NULL constraint failed: t.v",
            ),
            (
                "CREATE TABLE t (v INTEGER CHECK (v > 0))",
                "INSERT INTO t VALUES (-1)",
                "CHECK constraint failed: v > 0",
            ),
        ],
        ids=["not_null", "check"],
    )
    def test_other_integrity_errors_unchanged(self, db, ddl, insert, message):
        db.adapter.execute(ddl)
        db.adapter.commit()
        with pytest.raises(sqlite3.IntegrityError) as info:
            db.adapter.execute(insert)
        assert type(info.value) is sqlite3.IntegrityError
        assert str(info.value) == message

    def test_foreign_key_error_unchanged(self, db):
        db.adapter.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
        db.adapter.execute("CREATE TABLE c (pid INTEGER REFERENCES p(id))")
        db.adapter.commit()
        with pytest.raises(sqlite3.IntegrityError) as info:
            db.adapter.execute("INSERT INTO c VALUES (99)")
        assert type(info.value) is sqlite3.IntegrityError
        assert str(info.value) == "FOREIGN KEY constraint failed"

    def test_insert_many_third_row_stores_nothing(self, emails):
        docs = [{"email": "b@x"}, {"email": "c@x"}, {"email": "a@x"}, {"email": "d@x"}]
        with pytest.raises(UniqueConstraintError):
            emails.insert_many("users", docs)
        rows = emails.adapter.execute("SELECT data FROM users ORDER BY _id").fetchall()
        assert [r[0] for r in rows] == ['{"email":"a@x"}']

    def test_violation_inside_transaction_keeps_it_open(self, emails):
        with emails.transaction():
            emails.insert_document("users", {"email": "b@x"})
            with pytest.raises(UniqueConstraintError):
                emails.insert_document("users", {"email": "a@x"})
            assert emails.adapter.in_transaction is True
            emails.insert_document("users", {"email": "c@x"})
        rows = emails.adapter.execute("SELECT data FROM users ORDER BY _id").fetchall()
        assert [r[0] for r in rows] == ['{"email":"a@x"}', '{"email":"b@x"}', '{"email":"c@x"}']

    def test_violation_outside_transaction_is_rolled_back(self, emails):
        with pytest.raises(UniqueConstraintError):
            emails.insert_document("users", {"email": "a@x"})
        assert emails.adapter._conn().in_transaction is False


class TestTriggerMessageIsNotAViolation:
    """A trigger that raises text starting with the unique-violation prefix is not one."""

    @staticmethod
    def _trigger(db, text):
        db.create_index("audit_src", "k")
        db.insert_document("audit_src", {"k": 0})
        db.adapter.execute(
            "CREATE TRIGGER t_deny BEFORE INSERT ON audit_src "
            f"BEGIN SELECT RAISE(ABORT, '{text}'); END"
        )
        db.adapter.commit()

    @pytest.mark.parametrize(
        "text", ["UNIQUE constraint failed: audit", "UNIQUE constraint failed: a.b"]
    )
    def test_insert_document(self, db, text):
        self._trigger(db, text)
        with pytest.raises(sqlite3.IntegrityError) as info:
            db.insert_document("audit_src", {"k": 1})
        assert type(info.value) is sqlite3.IntegrityError
        assert str(info.value) == text

    def test_raw_execute(self, db):
        self._trigger(db, "UNIQUE constraint failed: audit")
        with pytest.raises(sqlite3.IntegrityError) as info:
            db.adapter.execute("INSERT INTO audit_src (data) VALUES ('{}')")
        assert type(info.value) is sqlite3.IntegrityError

    def test_model_save(self, db):
        class Row(SQLerModel):
            k: int

        Row.set_db(db, "audit_src")
        self._trigger(db, "UNIQUE constraint failed: audit")
        with pytest.raises(sqlite3.IntegrityError) as info:
            Row(k=1).save()
        assert type(info.value) is sqlite3.IntegrityError

    def test_real_violation_on_a_table_with_the_trigger_is_converted(self, db):
        db.create_index("u", "email", unique=True)
        db.insert_document("u", {"email": "a@x"})
        db.adapter.execute(
            "CREATE TRIGGER t_deny BEFORE INSERT ON u WHEN json_extract(NEW.data, '$.email') = 'deny' "
            "BEGIN SELECT RAISE(ABORT, 'UNIQUE constraint failed: audit'); END"
        )
        db.adapter.commit()
        with pytest.raises(UniqueConstraintError):
            db.insert_document("u", {"email": "a@x"})
        with pytest.raises(sqlite3.IntegrityError) as info:
            db.insert_document("u", {"email": "deny"})
        assert type(info.value) is sqlite3.IntegrityError
