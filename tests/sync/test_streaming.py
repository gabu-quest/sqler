"""Tests for streaming query results (iter_dicts, queryset.iter, stream_jsonl)."""

import contextlib
import itertools
import json
import signal
import sqlite3
import threading
import types
from dataclasses import dataclass

import pytest
from exception_points import needs_libc_raise, stop_once_and_interrupt

from sqler import SQLerDB, SQLerModel, SQLerSafeModel, stream_jsonl
from sqler.models import SQLerLiteModel, SQLerLiteModelBase, SQLerLiteSafeModel
from sqler.query import SQLerField as F


class Item(SQLerModel):
    name: str
    category: str
    price: float


def setup_db():
    db = SQLerDB.in_memory(shared=False)
    Item.set_db(db)
    return db


def seed_items():
    Item.save_many(
        [
            Item(name="A", category="x", price=10.0),
            Item(name="B", category="x", price=20.0),
            Item(name="C", category="y", price=30.0),
            Item(name="D", category="y", price=40.0),
            Item(name="E", category="z", price=50.0),
        ]
    )


class TestIterDicts:
    def test_iter_dicts_yields_all_rows(self):
        db = setup_db()
        try:
            seed_items()
            q = db.query("items")
            results = list(q.iter_dicts())
            assert len(results) == 5
            names = {r["name"] for r in results}
            assert names == {"A", "B", "C", "D", "E"}
            for r in results:
                assert isinstance(r["_id"], int)
                assert r["category"] in {"x", "y", "z"}
                assert isinstance(r["price"], (int, float))
        finally:
            db.close()

    def test_iter_dicts_with_filter(self):
        db = setup_db()
        try:
            seed_items()
            q = db.query("items").filter(F("category") == "x")
            results = list(q.iter_dicts())
            assert len(results) == 2
            names = {r["name"] for r in results}
            assert names == {"A", "B"}
        finally:
            db.close()

    def test_iter_dicts_with_limit(self):
        db = setup_db()
        try:
            seed_items()
            q = db.query("items").order_by("name").limit(3)
            results = list(q.iter_dicts())
            assert len(results) == 3
            assert [r["name"] for r in results] == ["A", "B", "C"]
        finally:
            db.close()

    def test_iter_dicts_empty_table(self):
        db = setup_db()
        try:
            q = db.query("items")
            results = list(q.iter_dicts())
            assert results == []
        finally:
            db.close()

    def test_iter_dicts_is_generator_with_correct_data(self):
        """Verify iter_dicts returns a generator that yields correct dicts."""
        db = setup_db()
        try:
            seed_items()
            q = db.query("items").order_by("name")
            gen = q.iter_dicts()
            assert isinstance(gen, types.GeneratorType)
            first = next(gen)
            assert first["name"] == "A"
            assert first["price"] == 10.0
            assert isinstance(first["_id"], int)
            second = next(gen)
            assert second["name"] == "B"
        finally:
            db.close()

    def test_iter_dicts_matches_all_dicts(self):
        """iter_dicts and all_dicts should return identical data."""
        db = setup_db()
        try:
            seed_items()
            q = db.query("items").order_by("name")
            streamed = list(q.iter_dicts())
            eager = q.all_dicts()
            assert len(streamed) == len(eager) == 5
            for s, e in zip(streamed, eager):
                assert s == e
        finally:
            db.close()


class TestQuerySetIter:
    def test_queryset_iter(self):
        db = setup_db()
        try:
            seed_items()
            results = list(Item.query().order_by("name").iter())
            assert len(results) == 5
            assert [inst.name for inst in results] == ["A", "B", "C", "D", "E"]
            for inst in results:
                assert isinstance(inst, Item)
                assert isinstance(inst._id, int)
        finally:
            db.close()

    def test_queryset_iter_with_filter(self):
        db = setup_db()
        try:
            seed_items()
            results = list(Item.query().filter(F("price") > 25).iter())
            assert len(results) == 3
            names = {inst.name for inst in results}
            assert names == {"C", "D", "E"}
        finally:
            db.close()

    def test_queryset_iter_is_generator_with_data(self):
        db = setup_db()
        try:
            seed_items()
            gen = Item.query().order_by("name").iter()
            assert isinstance(gen, types.GeneratorType)
            first = next(gen)
            assert first.name == "A"
            assert first.price == 10.0
        finally:
            db.close()


class TestStreamJsonl:
    def test_stream_jsonl_yields_all_rows(self):
        db = setup_db()
        try:
            seed_items()
            lines = list(stream_jsonl(Item))
            assert len(lines) == 5
            parsed = [json.loads(line) for line in lines]
            names = {obj["name"] for obj in parsed}
            assert names == {"A", "B", "C", "D", "E"}
        finally:
            db.close()

    def test_stream_jsonl_is_generator_with_data(self):
        db = setup_db()
        try:
            seed_items()
            gen = stream_jsonl(Item)
            assert isinstance(gen, types.GeneratorType)
            first = json.loads(next(gen))
            assert first["_id"] is not None
            assert first["name"] in {"A", "B", "C", "D", "E"}
        finally:
            db.close()

    def test_stream_jsonl_valid_json_with_values(self):
        db = setup_db()
        try:
            seed_items()
            lines = list(stream_jsonl(Item))
            assert len(lines) == 5
            for line in lines:
                obj = json.loads(line)
                assert obj["name"] in {"A", "B", "C", "D", "E"}
                assert isinstance(obj["_id"], int)
                assert obj["category"] in {"x", "y", "z"}
                assert isinstance(obj["price"], (int, float))
        finally:
            db.close()

    def test_stream_jsonl_exclude_id(self):
        db = setup_db()
        try:
            seed_items()
            lines = list(stream_jsonl(Item, include_id=False))
            assert len(lines) == 5
            for line in lines:
                obj = json.loads(line)
                assert "_id" not in obj
                assert obj["name"] in {"A", "B", "C", "D", "E"}
        finally:
            db.close()

    def test_stream_jsonl_field_projection(self):
        db = setup_db()
        try:
            seed_items()
            lines = list(stream_jsonl(Item, fields=["name", "price"]))
            assert len(lines) == 5
            for line in lines:
                obj = json.loads(line)
                assert "name" in obj
                assert "price" in obj
                assert "category" not in obj
        finally:
            db.close()

    def test_stream_jsonl_empty_table(self):
        db = setup_db()
        try:
            lines = list(stream_jsonl(Item))
            assert lines == []
        finally:
            db.close()


# --- rows as their statement saw them, also when the thread sends statements meanwhile -----

ROWS = 150
_tables = itertools.count()
BASES = {
    "pydantic": SQLerModel,
    "pydantic-safe": SQLerSafeModel,
    "lite": SQLerLiteModel,
    "lite-safe": SQLerLiteSafeModel,
}
KINDS = ["file", "memory", "shared-cache memory"]


def _database(kind, tmp_path):
    if kind == "file":
        return SQLerDB.on_disk(str(tmp_path / f"s{next(_tables)}.db"))
    return SQLerDB.in_memory(shared=kind != "memory")


def _declare(base):
    """A class of ``base`` with one field ``n``, on a table of its own."""
    table = f"st_{next(_tables)}"
    ns = {"__module__": __name__, "__tablename__": table, "__annotations__": {"n": int}, "n": 0}
    cls = types.new_class(f"Row_{table}", (base,), {}, lambda body: body.update(ns))
    return dataclass(cls) if issubclass(base, SQLerLiteModelBase) else cls


def _table(db, rows=ROWS):
    """A raw table ``t (id, n)`` with ``n`` from 0, indexed on ``n``, committed."""
    adapter = db.adapter
    adapter.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)")
    adapter.executemany("INSERT INTO t (n) VALUES (?)", [(i,) for i in range(rows)])
    adapter.execute("CREATE INDEX t_n ON t (n)")
    adapter.commit()
    return adapter


class Stepped:
    """The SQL function ``stepped(x)``: returns ``x`` and counts the rows SQLite has stepped to;
    ``fail_at``: the call with that ``x`` raises."""

    def __init__(self, fail_at=None):
        self.count, self.fail_at = 0, fail_at

    def __call__(self, x):
        if x == self.fail_at:
            raise LookupError(x)
        self.count += 1
        return x


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("unit", ["no transaction", "transaction"])
@pytest.mark.parametrize("way", [*BASES, "raw cursor"])
def test_a_loop_that_writes_each_row_gets_each_row_once_as_its_statement_saw_it(
    way, unit, kind, tmp_path
):
    db = _database(kind, tmp_path)
    try:
        seen = []
        if way == "raw cursor":
            adapter = _table(db)
            with db.transaction() if unit == "transaction" else contextlib.nullcontext():
                for row in adapter.execute("SELECT id, n FROM t ORDER BY n"):
                    seen.append((row[0], row[1]))
                    if len(seen) > ROWS:  # a walk that meets the rows it moved would not end
                        break
                    adapter.execute("UPDATE t SET n = n + ? WHERE id = ?", [ROWS, row[0]])
                    adapter.auto_commit()
            stored = [
                tuple(r) for r in adapter.execute("SELECT id, n FROM t ORDER BY id").fetchall()
            ]
        else:
            cls = _declare(BASES[way])
            cls.save_many([cls(n=i) for i in range(ROWS)], db=db)
            db.create_index(cls.__tablename__, "n")  # a save moves its row on in the order walked
            with db.transaction() if unit == "transaction" else contextlib.nullcontext():
                for m in cls.using(db).order_by("n").iter():
                    seen.append((m._id, m.n))
                    if len(seen) > ROWS:
                        break
                    m.n += ROWS
                    m.save()
            stored = sorted((m._id, m.n) for m in cls.using(db).all())
    finally:
        db.close()
    assert (seen, stored) == (
        [(i, i - 1) for i in range(1, ROWS + 1)],
        [(i, i - 1 + ROWS) for i in range(1, ROWS + 1)],
    )


def test_two_cursors_of_one_connection_read_in_turn_with_a_write_between_give_their_statements_rows(
    tmp_path,
):
    db = _database("file", tmp_path)
    try:
        adapter = _table(db)
        up = adapter.execute("SELECT n FROM t ORDER BY n")
        down = adapter.execute("SELECT n FROM t ORDER BY n DESC")
        got_up, got_down = [], []
        for _ in range(ROWS):
            got_up.append(next(up)[0])
            adapter.execute("UPDATE t SET n = n + ?", [ROWS])
            adapter.commit()
            got_down.append(next(down)[0])
        after = (up.fetchone(), down.fetchone())
    finally:
        db.close()
    assert (got_up, got_down, after) == (
        list(range(ROWS)),
        list(range(ROWS - 1, -1, -1)),
        (None, None),
    )


@pytest.mark.parametrize("read", ["lazily", "ahead"])
def test_a_cursor_closed_inside_its_own_loop_gives_no_row_after_the_close(read, tmp_path):
    db = _database("file", tmp_path)
    try:
        adapter = _table(db)
        cursor = adapter.execute("SELECT n FROM t ORDER BY n")
        given = []
        with pytest.raises(sqlite3.ProgrammingError) as in_loop:
            for row in cursor:
                given.append(row[0])
                if read == "ahead":
                    adapter.execute(
                        "SELECT 1"
                    ).fetchall()  # a statement on the connection: the rest is read ahead
                cursor.close()
        failures = [str(in_loop.value)]
        for fetch in (cursor.fetchone, cursor.fetchall, cursor.fetchmany):
            with pytest.raises(sqlite3.ProgrammingError) as raised:
                fetch()
            failures.append(str(raised.value))
    finally:
        db.close()
    assert (given, failures) == ([0], ["Cannot operate on a closed cursor."] * 4)


def _raised(qs, kept):
    # the error is kept, and with it this frame and the iterator it holds
    it = qs.iter()
    with pytest.raises(LookupError) as stopped:
        for _ in it:
            raise LookupError
    kept.append(stopped)


def _bad_row(qs, kept):
    with pytest.raises(json.JSONDecodeError) as stopped:
        for _ in qs.iter():
            pass
    kept.append(stopped)


@pytest.mark.parametrize("ending", [_raised, _bad_row], ids=lambda f: f.__name__.strip("_"))
def test_a_write_after_a_loop_left_part_read_goes_through_after_another_connections_commit(
    ending, tmp_path
):
    db = _database("file", tmp_path)
    other = sqlite3.connect(db.adapter.path)
    try:
        cls = _declare(SQLerModel)
        cls.save_many([cls(n=i) for i in range(ROWS)], db=db)
        other.execute(f"UPDATE {cls.__tablename__} SET data = '{{not json' WHERE _id = 3")
        other.commit()
        kept = []
        ending(cls.using(db), kept)
        other.execute(f"INSERT INTO {cls.__tablename__} (data) VALUES ('{{\"n\": -1}}')")
        other.commit()
        cls(n=-2).save(db=db)
        committed = other.execute(f"SELECT count(*) FROM {cls.__tablename__}").fetchone()[0]
    finally:
        other.close()
        db.close()
    assert committed == ROWS + 2


@pytest.mark.parametrize("kind", ["file", "memory"])
def test_a_write_that_fails_inside_the_loop_leaves_the_rest_as_the_statement_saw_them(
    kind, tmp_path
):
    db = _database(kind, tmp_path)
    try:
        adapter = _table(db)
        adapter.execute("CREATE UNIQUE INDEX t_n_once ON t (n)")
        adapter.commit()
        given, failed = [], 0
        for row in adapter.execute("SELECT id, n FROM t ORDER BY n"):
            given.append(row[1])
            adapter.execute("UPDATE t SET n = n + ? WHERE id = ?", [ROWS, row[0]])
            try:
                adapter.execute(
                    "INSERT INTO t (n) VALUES (?)", [row[1] + ROWS]
                )  # taken by the update
            except sqlite3.IntegrityError:
                failed += 1
            adapter.auto_commit()
        stored = [r[0] for r in adapter.execute("SELECT n FROM t ORDER BY id").fetchall()]
    finally:
        db.close()
    # outside a transaction the failed insert rolls back what was sent since the last commit: its update
    assert (given, failed, stored) == (list(range(ROWS)), ROWS, list(range(ROWS)))


def test_a_loop_that_sends_nothing_reads_its_rows_as_it_gives_them(tmp_path):
    db = _database("file", tmp_path)
    try:
        adapter = _table(db, rows=200_000)
        stepped = Stepped()
        adapter._conn().create_function("stepped", 1, stepped)
        cursor = adapter.execute("SELECT stepped(n) FROM t")
        first = next(cursor)[0]
        handed_out = stepped.count
        rest = sum(1 for _ in cursor)
    finally:
        db.close()
    # sqlite3 steps one row past the row it gives
    assert (first, handed_out, rest) == (0, 2, 199_999)


def test_a_thread_reading_while_another_thread_writes_on_its_own_connection_gets_its_statements_rows(
    tmp_path,
):
    db = _database("file", tmp_path)
    try:
        adapter = _table(db)
        stepped = Stepped()
        read_one, written = threading.Event(), threading.Event()
        got = {}

        def reader():
            adapter._conn().create_function("stepped", 1, stepped)
            cursor = adapter.execute("SELECT stepped(n) FROM t ORDER BY n")
            first = next(cursor)[0]
            read_one.set()
            written.wait(10)
            got["stepped before the rest"] = stepped.count
            got["rows"] = [first, *(row[0] for row in cursor)]

        thread = threading.Thread(target=reader)
        thread.start()
        assert read_one.wait(10)
        adapter.execute("UPDATE t SET n = n + ?", [ROWS])
        adapter.commit()
        written.set()
        thread.join(10)
    finally:
        db.close()
    assert got == {"stepped before the rest": 2, "rows": list(range(ROWS))}


def test_a_statement_that_fails_while_its_rows_are_read_ahead_raises_at_the_read_that_runs_out(
    tmp_path,
):
    db = _database("file", tmp_path)
    try:
        adapter = _table(db)
        adapter._conn().create_function("stepped", 1, Stepped(fail_at=10))
        cursor = adapter.execute("SELECT stepped(n) FROM t ORDER BY n")
        given = [next(cursor)[0]]
        adapter.execute(
            "UPDATE t SET n = n + ?", [ROWS]
        )  # the rest of the cursor is read before it
        adapter.commit()
        with pytest.raises(sqlite3.OperationalError) as raised:
            for row in cursor:
                given.append(row[0])
    finally:
        db.close()
    # read lazily, sqlite3 drops the row it gives as the step after it fails: 9 is not given either
    assert (given, "user-defined function raised exception" in str(raised.value)) == (
        list(range(9)),
        True,
    )


# --- a rollback reads nothing on -------------------------------------------------------------

ROLLED_BACK_UNREAD = (
    "the transaction was rolled back before the rest of this cursor's rows were read"
)


@pytest.mark.parametrize("kind", ["file", "shared-cache memory"])
@pytest.mark.parametrize("unit", ["transaction", "savepoint"])
def test_a_rollback_reads_none_of_the_rows_a_cursor_has_left_and_its_next_read_says_so(
    unit, kind, tmp_path
):
    db = _database(kind, tmp_path)
    try:
        adapter = _table(db)
        stepped = Stepped()
        adapter._conn().create_function("stepped", 1, stepped)
        with db.transaction() if unit == "savepoint" else contextlib.nullcontext():
            with pytest.raises(LookupError):
                with db.transaction():
                    cursor = adapter.execute("SELECT stepped(n) FROM t ORDER BY n")
                    first = next(cursor)[0]
                    raise LookupError
        with pytest.raises(sqlite3.OperationalError) as raised:
            cursor.fetchall()
    finally:
        db.close()
    # sqlite3 steps one row past the row it gives; the rollback steps no further
    assert (first, stepped.count, str(raised.value)) == (0, 2, ROLLED_BACK_UNREAD)


@pytest.mark.parametrize("kind", KINDS)
def test_a_cursor_read_ahead_before_the_rollback_keeps_the_rows_its_statement_saw(kind, tmp_path):
    db = _database(kind, tmp_path)
    try:
        adapter = _table(db)
        with pytest.raises(LookupError):
            with db.transaction():
                cursor = adapter.execute("SELECT n FROM t ORDER BY n")
                first = next(cursor)[0]
                adapter.execute(
                    "UPDATE t SET n = n + ?", [ROWS]
                )  # the rest of the cursor is read before it
                raise LookupError
        rest = [row[0] for row in cursor]
        stored = [r[0] for r in adapter.execute("SELECT n FROM t ORDER BY n").fetchall()]
    finally:
        db.close()
    assert (first, rest, stored) == (0, list(range(1, ROWS)), list(range(ROWS)))


def test_a_write_after_a_rollback_that_left_a_cursor_part_read_goes_through_after_another_connections_commit(
    tmp_path,
):
    db = _database("file", tmp_path)
    other = sqlite3.connect(db.adapter.path, timeout=0)
    try:
        adapter = _table(db)
        with pytest.raises(LookupError):
            with db.transaction():
                cursor = adapter.execute("SELECT n FROM t ORDER BY n")
                first = next(cursor)[0]
                raise LookupError
        other.execute("INSERT INTO t (n) VALUES (-1)")
        other.commit()
        adapter.execute("INSERT INTO t (n) VALUES (-2)")
        adapter.commit()
        committed = other.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        other.close()
        db.close()
    assert (first, committed) == (0, ROWS + 2)


# --- what a cursor the adapter returned is not for; a reading ahead that is cut short -----------

ONE_STATEMENT = (
    "a cursor the adapter returned runs no other statement; send it with the adapter's execute()"
)


@pytest.mark.parametrize("state", ["read lazily", "read ahead"])
def test_a_cursor_the_adapter_returned_sends_no_statement_of_its_own_and_keeps_its_rows(
    state, tmp_path
):
    db = _database("file", tmp_path)
    try:
        adapter = _table(db)
        cursor = adapter.execute("SELECT n FROM t ORDER BY n")
        first = next(cursor)[0]
        if state == "read ahead":
            adapter.execute("SELECT 1").fetchall()
        refused = []
        for send, args in (
            (cursor.execute, ["UPDATE t SET n = -1"]),
            (cursor.executemany, ["UPDATE t SET n = ?", [(-1,)]]),
            (cursor.executescript, ["UPDATE t SET n = -1"]),
        ):
            with pytest.raises(sqlite3.ProgrammingError) as raised:
                send(*args)
            refused.append(str(raised.value))
        rest = [row[0] for row in cursor]
    finally:
        db.close()
    assert (first, refused, rest) == (0, [ONE_STATEMENT] * 3, list(range(1, ROWS)))


@needs_libc_raise
def test_an_interrupt_while_the_rest_is_read_ahead_is_raised_and_the_cursor_says_its_rows_stop(
    tmp_path,
):
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("this process does not turn SIGINT into KeyboardInterrupt")
    db = _database("file", tmp_path)
    try:
        adapter = _table(db)
        conn = adapter._conn()
        cursor = adapter.execute("SELECT n FROM t ORDER BY n")
        given = [next(cursor)[0]]
        stop_once_and_interrupt(conn)
        with pytest.raises(KeyboardInterrupt):
            adapter.execute(
                "UPDATE t SET n = n + ?", [ROWS]
            )  # reads the rest ahead first: stopped there
        with pytest.raises(sqlite3.OperationalError) as raised:
            for row in cursor:
                given.append(row[0])
        stored = [r[0] for r in adapter.execute("SELECT n FROM t ORDER BY n").fetchall()]
    finally:
        db.close()
    # the update was not sent; the row sqlite3 had stepped to goes with the step that failed
    assert (given, str(raised.value), stored) == ([0], "interrupted", list(range(ROWS)))
