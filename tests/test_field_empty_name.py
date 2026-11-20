"""F() refuses an empty field name or an empty path segment when it is built."""

import pytest

from sqler.exceptions import InvalidFieldNameError
from sqler.query import F


def test_empty_name_raises():
    with pytest.raises(InvalidFieldNameError):
        F("")


@pytest.mark.parametrize("name", ["a..b", "a.", ".a"])
def test_dotted_names_with_an_empty_part_are_one_key(name):
    expr = F(name) == 1
    assert expr.sql == f"JSON_EXTRACT(data, '$.\"{name}\"') = ?"


@pytest.mark.parametrize("path", [[""], ["a", ""], ["", "a"], ["a", "", "b"]])
def test_list_paths_with_an_empty_segment_raise(path):
    with pytest.raises(InvalidFieldNameError):
        F(path)


def test_going_deeper_with_an_empty_key_raises():
    with pytest.raises(InvalidFieldNameError):
        F("a")[""]
    with pytest.raises(InvalidFieldNameError):
        F("a") / ""


def test_ordinary_names_still_build():
    assert (F("a") == 1).sql == "JSON_EXTRACT(data, '$.a') = ?"
    assert (F("a.b") == 1).sql == "JSON_EXTRACT(data, '$.\"a.b\"') = ?"
    assert (F(["a", 0, "b"]) == 1).sql == "JSON_EXTRACT(data, '$.a[0].b') = ?"
