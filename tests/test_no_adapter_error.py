"""One NoAdapterError: the sync and the async query raise the same class."""

import asyncio
import builtins
import pickle

import pytest
import sqler
from sqler import SQLerError
from sqler.query import SQLerQuery
from sqler.query.async_query import AsyncSQLerQuery
from sqler.query.query import NoAdapterError as SyncModuleNoAdapterError


def test_sync_module_name_is_the_exported_class():
    assert SyncModuleNoAdapterError is sqler.NoAdapterError


def test_sync_query_raises_the_exported_class():
    with pytest.raises(sqler.NoAdapterError):
        SQLerQuery(table="t").all()


def test_async_query_raises_the_exported_class():
    with pytest.raises(sqler.NoAdapterError):
        asyncio.run(AsyncSQLerQuery(table="t").all_dicts())


def test_it_is_a_sqler_error():
    assert issubclass(sqler.NoAdapterError, SQLerError)


def test_builtin_connection_error_still_catches_a_sync_query():
    caught = False
    try:
        SQLerQuery(table="t").count()
    except builtins.ConnectionError:
        caught = True
    assert caught


def test_pickle_round_trip_keeps_class_and_message():
    try:
        SQLerQuery(table="t").first()
    except sqler.NoAdapterError as exc:
        err = exc
    back = pickle.loads(pickle.dumps(err))
    assert type(back) is sqler.NoAdapterError
    assert str(back) == "No adapter set for query"
