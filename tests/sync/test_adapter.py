from sqlite3 import OperationalError, ProgrammingError

import pytest

from sqler.adapter import AdapterABC, NotConnectedError, SQLiteAdapter


def tests_run_at_all():
    assert True


@pytest.fixture(params=[("memory", "in_memory"), ("disk", "on_disk")])
def adapter(request, tmp_path):
    """yields a connected db in memory and on disk"""
    path = request.param
    if path == "disk":
        path = str(tmp_path / "test.db")
    else:
        path = ":memory:"

    adapter = SQLiteAdapter(path)
    adapter.connect()
    yield adapter
    adapter.close()


def test_db_implements_abc():
    """verify the inheritance is cool"""
    # also pytest needs any test at all to not fail on github action?
    assert issubclass(SQLiteAdapter, AdapterABC)


def test_factories():
    """makes sure the factory funcs work"""
    mem_adapter = SQLiteAdapter.in_memory()
    mem_adapter.connect()
    disk_adapter = SQLiteAdapter.in_memory()
    disk_adapter.connect()
    for adapter in [mem_adapter, disk_adapter]:
        cursor = adapter.execute("PRAGMA user_version;")
        assert isinstance(cursor.fetchone()[0], int)


def test_execute_and_commit(oligo_adapter):
    """tests basic execution"""
    oligo_adapter.execute("INSERT INTO oligos (length) VALUES (?);", [100])
    oligo_adapter.commit()
    cursor = oligo_adapter.execute("SELECT length FROM oligos;")
    assert cursor.fetchone()[0] == 100


def test_executemany_batch_insert(oligo_adapter):
    """tests execute many"""
    values = [[i] for i in range(100)]
    oligo_adapter.executemany("INSERT INTO oligos(length) VALUES (?);", values)
    cursor = oligo_adapter.execute("SELECT COUNT(*) FROM oligos;")
    assert cursor.fetchone()[0] == 100


def test_executescript(oligo_adapter):
    """test multiline scripts"""
    script = """
    INSERT INTO oligos(length) VALUES (1), (2);
    INSERT INTO oligos(length) VALUES (3);
    """
    oligo_adapter.executescript(script)

    # this part should fail
    with pytest.raises(ProgrammingError):
        oligo_adapter.execute(script)

    cursor = oligo_adapter.execute("SELECT length FROM oligos ORDER BY length;")
    assert [row[0] for row in cursor.fetchall()] == [1, 2, 3]


def test_context_manager(tmp_path):
    """test context manager commit"""
    path = str(tmp_path / "context_manager.db")
    with SQLiteAdapter(path) as adapter:
        adapter.execute("CREATE TABLE cm(x TEXT);")
        adapter.execute("INSERT INTO cm(x) VALUES (?);", ["hi"])
        adapter.commit()
        cursor = adapter.execute("SELECT x FROM cm;")
        assert cursor.fetchone()[0] == "hi"


def test_close_then_error(oligo_adapter):
    """ensure close disables operations"""
    oligo_adapter.close()
    with pytest.raises(NotConnectedError):
        oligo_adapter.execute("SELECT 1;")
    with pytest.raises(NotConnectedError):
        oligo_adapter.commit()
    with pytest.raises(NotConnectedError):
        oligo_adapter.executemany("SELECT 1;", [])


def test_execute_invalid_sql(oligo_adapter):
    """invalid sql raises OperationalError"""
    with pytest.raises(OperationalError):
        oligo_adapter.execute("THIS IS NOT VALID SQL")


def test_executemany_empty_list(oligo_adapter):
    """executemany with empty list should do nothing, not error"""
    oligo_adapter.execute("CREATE TABLE test_empty(x INTEGER);")
    oligo_adapter.executemany("INSERT INTO test_empty(x) VALUES (?);", [])
    cursor = oligo_adapter.execute("SELECT COUNT(*) FROM test_empty;")
    assert cursor.fetchone()[0] == 0


def test_commit_without_connection():
    """commit before connect should error"""
    adapter = SQLiteAdapter(":memory:")
    with pytest.raises(NotConnectedError):
        adapter.commit()


def test_multiple_connects_and_closes(tmp_path):
    """connect/close multiple times, then ensure closed disables executes"""
    path = str(tmp_path / "multi.db")
    adapter = SQLiteAdapter(path)
    adapter.connect()
    adapter.connect()
    adapter.close()
    adapter.close()
    with pytest.raises(NotConnectedError):
        adapter.execute("SELECT 1;")


def test_context_manager_rollback_on_exception(tmp_path):
    """rollback on exception: should not commit inserts"""
    path = str(tmp_path / "cm_rollback.db")
    try:
        with SQLiteAdapter(path) as adapter:
            adapter.execute("CREATE TABLE foo(x INTEGER);")
            adapter.execute("INSERT INTO foo(x) VALUES (1);")
            raise RuntimeError("Force rollback")
    except RuntimeError:
        pass
    # Data should NOT be committed if rollback works right
    adapter2 = SQLiteAdapter(path)
    adapter2.connect()
    cursor = adapter2.execute("SELECT COUNT(*) FROM foo;")
    assert cursor.fetchone()[0] == 0
