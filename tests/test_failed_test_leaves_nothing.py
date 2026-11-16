"""A test that fails while it holds an async database must not keep the pytest process alive.

Each case runs a small pytest of its own: the thing under test is how that process ends."""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

TESTS = Path(__file__).parent
HEADER = "import asyncio\nfrom sqler.db.async_db import AsyncSQLerDB\n"


def _run_inner(tmp_path, source, timeout=60):
    """Run ``source`` as a test file under this suite's conftest; the child inherits the
    parent's environment (Windows needs SYSTEMROOT to start) with this folder in front on PYTHONPATH."""
    (tmp_path / "test_inner.py").write_text(HEADER + textwrap.dedent(source))
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(TESTS), os.environ.get("PYTHONPATH")])),
    }
    for name in ("PYTEST_ADDOPTS", "PY_COLORS", "FORCE_COLOR"):  # the summary is read as plain text
        env.pop(name, None)
    env["NO_COLOR"] = "1"
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "conftest",
        "-p",
        "no:cacheprovider",
        "-q",
        "--rootdir",
        str(tmp_path),
        str(tmp_path),
    ]
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, cwd=tmp_path, env=env
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"the inner run did not end within {timeout} s:\n{exc.stdout}\n{exc.stderr}"
        ) from exc
    return done, f"inner run, exit {done.returncode}:\n{done.stdout}\n{done.stderr}"


OPEN_ONE = """
    async def open_one():
        db = AsyncSQLerDB.in_memory(shared=False)
        await db.connect()
        return db
"""


def test_a_failed_test_leaves_nothing_open_and_a_passing_one_is_not_touched(tmp_path):
    done, shown = _run_inner(
        tmp_path,
        OPEN_ONE
        + """
    import pytest

    @pytest.fixture(scope="module")
    def wider():
        db = asyncio.run(open_one())
        yield db
        asyncio.run(db.close())

    def test_wider_one(wider):
        asyncio.run(wider.execute_sql("SELECT 1"))

    def test_wider_two(wider):
        asyncio.run(wider.execute_sql("SELECT 1"))

    def test_fails_with_two_open():
        dbs = [asyncio.run(open_one()), asyncio.run(open_one())]
        assert dbs is None, "left it open"

    def test_fails_after_closing_its_own():
        db = asyncio.run(open_one())
        asyncio.run(db.close())
        assert db is None, "left it open"
    """,
    )
    failures = re.findall(r"^E +AssertionError: left it open$", done.stdout, re.M)
    assert (
        done.returncode,
        len(failures),
        re.search(r"^2 failed, 2 passed in ", done.stdout, re.M) is not None,
    ) == (1, 2, True), shown


def test_a_close_that_fails_does_not_hide_the_next_one(tmp_path):
    done, shown = _run_inner(
        tmp_path,
        OPEN_ONE
        + """
    def test_leaves_two_and_the_first_cannot_close():
        first, second = asyncio.run(open_one()), asyncio.run(open_one())
        first.adapter._all_conns[0]._running = False  # its close() now raises "Connection closed"
    """,
    )
    assert (
        done.returncode,
        re.search(r"^1 passed, 1 error in ", done.stdout, re.M) is not None,
    ) == (1, True), shown
    assert "left open and its close raised ValueError" in done.stdout, shown
    assert done.stdout.count("the test left async connections open") == 1, shown


def test_a_connection_in_a_statement_that_never_ends_fails_its_test_and_the_run_ends(tmp_path):
    done, shown = _run_inner(
        tmp_path,
        OPEN_ONE
        + """
    FOREVER = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"

    def test_leaves_a_statement_running():
        async def start():
            db = await open_one()
            asyncio.ensure_future(db.adapter._all_conns[0].execute(FOREVER))
            await asyncio.sleep(0.2)
        asyncio.run(start())
    """,
    )
    assert (
        done.returncode,
        re.search(r"^1 passed, 1 error in ", done.stdout, re.M) is not None,
    ) == (1, True), shown
    assert "was left in a statement that did not end" in done.stdout, shown
