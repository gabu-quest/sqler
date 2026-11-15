"""A search field with a dot is refused where it is given (constructor and setter)."""

import pytest
from sqler import FTSIndex, SQLerDB, SQLerModel
from sqler.exceptions import InvalidIdentifierError


class Doc(SQLerModel):
    __tablename__ = "fts_dot_docs"
    title: str = ""
    meta: dict = {}


def test_plain_fields_still_create():
    db = SQLerDB.in_memory(shared=False)
    FTSIndex(Doc, fields=["title"], db=db).create()
    names = [r[0] for r in db.adapter.execute("SELECT name FROM sqlite_master;").fetchall()]
    assert "fts_dot_docs_fts" in names


def test_constructor_refuses_a_dotted_field():
    with pytest.raises(InvalidIdentifierError):
        FTSIndex(Doc, fields=["title", "meta.title"], db=SQLerDB.in_memory(shared=False))


def test_fields_setter_refuses_a_dotted_field():
    fts = FTSIndex(Doc, fields=["title"], db=SQLerDB.in_memory(shared=False))
    with pytest.raises(InvalidIdentifierError):
        fts.fields = ["title", "meta.title"]
    assert fts.fields == ["title"]
