"""pydantic is an optional extra: the lite model bases work where it is not installed.

Each test runs a fresh interpreter in which ``import pydantic`` fails.
"""

import subprocess
import sys
import textwrap

BLOCK = "import sys\nsys.modules['pydantic'] = None\n"


def _run(body: str) -> str:
    done = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", BLOCK + textwrap.dedent(body)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def test_lite_model_saves_and_loads_without_pydantic():
    out = _run(
        """
        from dataclasses import dataclass
        from sqler import SQLerDB
        from sqler.models.lite.model import SQLerLiteModel

        @dataclass
        class Note(SQLerLiteModel):
            title: str = ""

        db = SQLerDB.in_memory()
        Note.set_db(db, "notes")
        saved = Note(title="a").save()
        print(Note.from_id(saved._id).title, "pydantic" in sys.modules and sys.modules["pydantic"])
        """
    )
    assert out == "a None"


def test_lite_safe_model_rebases_a_save_without_pydantic():
    out = _run(
        """
        from dataclasses import dataclass
        from sqler import SQLerDB
        from sqler.models.lite.safe import SQLerLiteSafeModel

        @dataclass
        class Hit(SQLerLiteSafeModel):
            title: str = ""
            count: int = 0

        db = SQLerDB.in_memory()
        Hit.set_db(db, "hits")
        first = Hit(title="t", count=10).save()
        mine = Hit.from_id(first._id)
        theirs = Hit.from_id(first._id)
        theirs.title = "theirs"
        theirs.save()
        mine.count += 1
        mine.save()
        row = Hit.from_id(first._id)
        print(row.title, row.count, mine.title, mine.count)
        """
    )
    assert out == "theirs 11 theirs 11"
