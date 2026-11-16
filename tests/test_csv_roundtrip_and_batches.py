"""CSV export then import returns every string unchanged; import_csv honours batch_size."""

import csv
from typing import Optional

import pytest

from sqler import SQLerDB, SQLerModel
from sqler.export import (
    export_csv,
    export_csv_string,
    import_csv,
    import_csv_string,
)

STRINGS = [
    "=1+1",
    "-5",
    "+x",
    "@a",
    "'=1",
    "'",
    "''",
    "'a",
    "",
    "plain",
    "=cmd|' /C calc'!A0",
    '=1,"q"\nline two',
    "\t=1",
    "\r=1",
    " =1",
    "a=b",
    "''=1",
    "'90s",
    "it's",
    "'''",
    "'\t",
]


def _model(db):
    class Note(SQLerModel):
        text: str
        maybe: Optional[str] = None

    Note.set_db(db)
    return Note


@pytest.fixture
def Note():
    return _model(SQLerDB.in_memory(shared=False))


def _texts(Note):
    return [(n.text, n.maybe) for n in sorted(Note.query().all(), key=lambda n: n._id)]


def _seed(Note):
    for s in STRINGS:
        Note(text=s, maybe=s).save()
    Note(text="none here", maybe=None).save()


def test_file_roundtrip_returns_every_string_unchanged(Note, tmp_path):
    _seed(Note)
    before = _texts(Note)
    path = tmp_path / "n.csv"
    export_csv(Note, path)
    for n in Note.query().all():
        n.delete()
    result = import_csv(Note, path)
    assert result.failed == 0, result.errors
    assert _texts(Note) == before


def test_string_roundtrip_returns_every_string_unchanged(Note):
    _seed(Note)
    before = _texts(Note)
    text = export_csv_string(Note)
    for n in Note.query().all():
        n.delete()
    result = import_csv_string(Note, text)
    assert result.failed == 0, result.errors
    assert _texts(Note) == before


def test_no_cell_starts_with_a_formula_character(Note, tmp_path):
    _seed(Note)
    path = tmp_path / "n.csv"
    export_csv(Note, path)
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    for row in rows[1:]:
        for cell in row:
            assert not cell or cell[0] not in "=+-@\t\r", cell


FOREIGN = "text\r\n'90s\r\n'Tis\r\nplain\r\n'=1+1\r\n''\r\n'\r\n"
FOREIGN_STORED = ["'90s", "'Tis", "plain", "=1+1", "'", ""]


def test_a_foreign_cell_that_starts_with_an_apostrophe_is_read_as_written_file(Note, tmp_path):
    path = tmp_path / "foreign.csv"
    path.write_text(FOREIGN, encoding="utf-8", newline="")
    result = import_csv(Note, path)
    assert result.failed == 0, result.errors
    assert [n.text for n in sorted(Note.query().all(), key=lambda n: n._id)] == FOREIGN_STORED


def test_a_foreign_cell_that_starts_with_an_apostrophe_is_read_as_written_string(Note):
    result = import_csv_string(Note, FOREIGN)
    assert result.failed == 0, result.errors
    assert [n.text for n in sorted(Note.query().all(), key=lambda n: n._id)] == FOREIGN_STORED


# --- batch_size ---------------------------------------------------------------


def _write_rows(path, n, bad_row=None):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "age"])
        for i in range(1, n + 1):
            w.writerow([f"p{i}", "bad" if i == bad_row else str(i)])


@pytest.fixture
def Person():
    class Person(SQLerModel):
        name: str
        age: int

    Person.set_db(SQLerDB.in_memory(shared=False))
    return Person


def test_failure_at_row_7_with_batch_size_3_keeps_two_full_batches(Person, tmp_path):
    path = tmp_path / "p.csv"
    _write_rows(path, 10, bad_row=7)
    result = import_csv(Person, path, batch_size=3)
    assert sorted(p.name for p in Person.query().all()) == [f"p{i}" for i in range(1, 7)]
    assert result.succeeded == 6
    assert result.failed == 1
    assert result.count == 7
    assert result.errors[0]["row"] == 8  # the header is row 1


def test_failure_at_row_8_with_batch_size_3_loses_the_whole_third_batch(Person, tmp_path):
    path = tmp_path / "p.csv"
    _write_rows(path, 10, bad_row=8)
    result = import_csv(Person, path, batch_size=3)
    assert sorted(p.name for p in Person.query().all()) == [f"p{i}" for i in range(1, 7)]
    assert result.succeeded == 6
    assert result.failed == 2
    assert result.count == 8


def test_batch_size_below_one_is_refused(Person, tmp_path):
    path = tmp_path / "p.csv"
    _write_rows(path, 2)
    with pytest.raises(ValueError, match="batch_size"):
        import_csv(Person, path, batch_size=0)


def test_failure_inside_the_first_batch_leaves_nothing(Person, tmp_path):
    path = tmp_path / "p.csv"
    _write_rows(path, 10, bad_row=2)
    result = import_csv(Person, path, batch_size=3)
    assert Person.query().count() == 0
    assert result.succeeded == 0


def test_without_batch_size_rows_before_the_failure_stay(Person, tmp_path):
    path = tmp_path / "p.csv"
    _write_rows(path, 10, bad_row=7)
    result = import_csv(Person, path)
    assert Person.query().count() == 6
    assert result.succeeded == 6
    assert result.failed == 1


def test_batches_with_skip_errors_import_every_good_row(Person, tmp_path):
    path = tmp_path / "p.csv"
    _write_rows(path, 10, bad_row=7)
    result = import_csv(Person, path, batch_size=3, skip_errors=True)
    assert Person.query().count() == 9
    assert result.succeeded == 9
    assert result.failed == 1


def test_a_good_import_in_batches_imports_everything(Person, tmp_path):
    path = tmp_path / "p.csv"
    _write_rows(path, 10)
    result = import_csv(Person, path, batch_size=3)
    assert Person.query().count() == 10
    assert result.succeeded == 10
