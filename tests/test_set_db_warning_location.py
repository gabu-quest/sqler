"""``set_db()`` warns at the line of the caller, once, on all nine model bases.

The warning used to be attributed to sqler's own file when a safe base called
``super().set_db()``, where Python's default filters hide it.
"""

import inspect
import warnings
from dataclasses import dataclass

import pytest

from sqler import SQLerDB, SQLerModel, SQLerSafeModel
from sqler.db.async_db import AsyncSQLerDB
from sqler.models.async_model import AsyncSQLerModel
from sqler.models.async_safe import AsyncSQLerSafeModel
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.lite.async_safe import AsyncSQLerLiteSafeModel
from sqler.models.lite.model import SQLerLiteModel
from sqler.models.lite.safe import SQLerLiteSafeModel
from sqler.models.msgspec.async_model import AsyncSQLerMsgspecModel
from sqler.models.msgspec.model import SQLerMsgspecModel


class WPyd(SQLerModel):
    __tablename__ = "wl_pyd"
    n: int = 0


class WPydSafe(SQLerSafeModel):
    __tablename__ = "wl_pyd_safe"
    n: int = 0


class WPydSafeChild(WPydSafe):
    __tablename__ = "wl_pyd_safe_child"


class WPydSafeGrandchild(WPydSafeChild):
    __tablename__ = "wl_pyd_safe_grandchild"


@dataclass
class WLite(SQLerLiteModel):
    __tablename__ = "wl_lite"
    n: int = 0


@dataclass
class WLiteSafe(SQLerLiteSafeModel):
    __tablename__ = "wl_lite_safe"
    n: int = 0


class WMsg(SQLerMsgspecModel):
    __tablename__ = "wl_msg"
    n: int = 0


class WAPyd(AsyncSQLerModel):
    __tablename__ = "wl_apyd"
    n: int = 0


class WAPydSafe(AsyncSQLerSafeModel):
    __tablename__ = "wl_apyd_safe"
    n: int = 0


@dataclass
class WALite(AsyncSQLerLiteModel):
    __tablename__ = "wl_alite"
    n: int = 0


@dataclass
class WALiteSafe(AsyncSQLerLiteSafeModel):
    __tablename__ = "wl_alite_safe"
    n: int = 0


SYNC = [WPyd, WPydSafe, WPydSafeChild, WPydSafeGrandchild, WLite, WLiteSafe, WMsg]


class WAMsg(AsyncSQLerMsgspecModel):
    __tablename__ = "wl_amsg"
    n: int = 0


ASYNC = [WAPyd, WAPydSafe, WALite, WALiteSafe, WAMsg]


def _call_and_record(cls, db):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        line = inspect.currentframe().f_lineno + 1
        cls.set_db(db)
    return [w for w in caught if issubclass(w.category, DeprecationWarning)], line


@pytest.mark.parametrize("cls", SYNC, ids=lambda c: c.__name__)
def test_sync_set_db_warns_once_at_the_callers_line(cls):
    db = SQLerDB.in_memory(shared=False)
    caught, line = _call_and_record(cls, db)
    assert len(caught) == 1
    assert caught[0].filename == __file__
    assert caught[0].lineno == line
    assert "set_db() is deprecated" in str(caught[0].message)
    db.close()


@pytest.mark.parametrize("cls", ASYNC, ids=lambda c: c.__name__)
def test_async_set_db_warns_once_at_the_callers_line(cls):
    db = AsyncSQLerDB.in_memory(shared=False)
    caught, line = _call_and_record(cls, db)
    assert len(caught) == 1
    assert caught[0].filename == __file__
    assert caught[0].lineno == line
    assert "set_db() is deprecated" in str(caught[0].message)
