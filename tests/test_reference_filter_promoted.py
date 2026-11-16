"""A filter through a reference finds the same rows whichever side keeps a field in a column of
its own: the related class (``Team.__promoted__``), the class filtered (``Player.__promoted__``),
both or neither, on the class's own table and on another table of its rows
(``using(db, table)``), sync and async, on the pydantic, lite and msgspec bases.

The condition on the related row is a query on the related table, laid out as the database
remembers that table, as any query on it is. So a field promoted after the statement was made,
by this database object or by another one on the file, is followed by the same queryset.

The rows (``_seed``): teams 1 red (rank 1, Oslo), 2 blue (rank 2, Rome), 3 green (rank 3,
Oslo); players 1 (team red, teams [blue, red]), 2 (blue, [blue]), 3 (green, []), 4 (no team).
The players are named and ranked unlike their teams (1 "blue" rank 3, 2 "green" 1, 3 "red" 2,
4 "red" 0), so a condition read from the player's row instead of the team's finds other rows.
The lite and msgspec bases have no ``ref()``; their filters are built with
``SQLerModelField``, which is what ``ref()`` builds.
"""

import inspect
import itertools
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerModel,
    AsyncSQLerMsgspecModel,
    SQLerDB,
    SQLerLiteModel,
    SQLerLiteModelBase,
    SQLerModel,
    SQLerMsgspecModel,
)
from sqler.models import SQLerModelField
from sqler.query import F

BASES = {
    "pydantic": SQLerModel,
    "async-pydantic": AsyncSQLerModel,
    "lite": SQLerLiteModel,
    "async-lite": AsyncSQLerLiteModel,
    "msgspec": SQLerMsgspecModel,
    "async-msgspec": AsyncSQLerMsgspecModel,
}
PROMOTED = {"name": "TEXT", "rank": "INTEGER", "founded": "TEXT"}
WHEN = [datetime(2020 + i, 1, 1, tzinfo=timezone.utc) for i in range(3)]

_names = itertools.count()


async def _v(value: Any) -> Any:
    """``value``, awaited when an async base returned an awaitable."""
    return await value if inspect.isawaitable(value) else value


def _model(kind: str, fields: dict[str, tuple[Any, Any]], table: str, extra: Optional[dict] = None, bases: tuple = ()) -> type:
    """A class of base ``kind`` storing in ``table``, published in this module so a string
    annotation can name it."""
    name = f"Rp{next(_names)}"

    def body(ns):
        ns["__module__"] = __name__
        ns["__annotations__"] = {k: t for k, (t, _) in fields.items()}
        for k, (_, d) in fields.items():
            ns[k] = d
        ns["__tablename__"] = table
        ns.update(extra or {})

    cls = types.new_class(name, bases or (BASES[kind],), {}, body)
    if issubclass(cls, SQLerLiteModelBase):
        cls = dataclass(cls)
    globals()[name] = cls
    return cls


def _classes(kind: str, promoted: str, p: str) -> tuple[type, type]:
    """``(Team, Player)``; ``promoted`` is ``related``, ``outer``, ``both`` or ``neither``."""
    team = _model(
        kind,
        {
            "name": (str, ""),
            "rank": (int, 0),
            "city": (str, ""),
            "founded": (Optional[datetime], None),
            "meta": (Optional[dict], None),
        },
        f"rp_{p}_teams",
        {"__promoted__": dict(PROMOTED)} if promoted in ("related", "both") else None,
    )
    player = _model(
        kind,
        {
            "name": (str, ""),
            "rank": (int, 0),
            "city": (str, ""),
            "team": (Optional[team], None),
            "teams": (Optional[list[team]], None),
        },
        f"rp_{p}_players",
        {"__promoted__": {"name": "TEXT", "rank": "INTEGER", "city": "TEXT"}} if promoted in ("outer", "both") else None,
    )
    return team, player


def _ref(cls: type, field: str, *rest: str, any_: bool = False) -> SQLerModelField:
    """``cls.ref(field)[.any()].field(*rest)``, or what it builds on a base without ``ref()``."""
    if hasattr(cls, "ref"):
        ref = cls.ref(field)
        return (ref.any() if any_ else ref).field(*rest)
    return SQLerModelField(cls, [field, *rest], array_any=any_)


async def _db(kind: str, path: Optional[str] = None):
    if kind.startswith("async"):
        db = AsyncSQLerDB.on_disk(path) if path else AsyncSQLerDB.in_memory(shared=False)
        await db.connect()
        return db
    return SQLerDB.on_disk(path) if path else SQLerDB.in_memory(shared=False)


async def _seed(team: type, player: type, db) -> None:
    red, blue, green = [
        await _v(team(name=n, rank=i + 1, city=c, founded=WHEN[i], meta=m).save(db=db))
        for i, (n, c, m) in enumerate(
            (("red", "Oslo", {"a.b": 1, "it's": 2}), ("blue", "Rome", {}), ("green", "Oslo", {}))
        )
    ]
    for name, rank, city, t, ts in (
        ("blue", 3, "Rome", red, [blue, red]),
        ("green", 1, "Oslo", blue, [blue]),
        ("red", 2, "Rome", green, []),
        ("red", 0, "Oslo", None, None),
    ):
        await _v(player(name=name, rank=rank, city=city, team=t, teams=ts).save(db=db))


async def _ids(qs) -> list[int]:
    return sorted(p._id for p in await _v(qs.all()))


async def _columns(db, table: str) -> list[str]:
    cur = await _v(db.adapter.execute(f'PRAGMA table_xinfo("{table}")'))
    return [r[1] for r in await _v(cur.fetchall())]


def _cases(player: type) -> dict[str, Any]:
    name, rank, city = (_ref(player, "team", f) for f in ("name", "rank", "city"))
    return {
        "name ==": name == "red",
        "name !=": name != "red",
        "rank ==": rank == 2,
        "rank !=": rank != 2,
        "rank <": rank < 2,
        "rank <=": rank <= 2,
        "rank >": rank > 2,
        "rank >=": rank >= 2,
        "~": ~(rank == 2),
        "|": (name == "red") | (rank == 3),
        "& the player's own field": (name == "red") & (F("name") == "blue"),
        "& the player's own field, no match": (name == "red") & (F("name") == "red"),
        "& two related fields": (rank >= 2) & (city == "Oslo"),
        "a field not promoted": city == "Oslo",
        "any() entry": _ref(player, "teams", "name", any_=True) == "red",
        "any() entry rank": _ref(player, "teams", "rank", any_=True) == 2,
        "_id": _ref(player, "team", "_id") == 2,
        "datetime ==": _ref(player, "team", "founded") == WHEN[0],
        "datetime <": _ref(player, "team", "founded") < WHEN[1],
        "None ==": name == None,  # noqa: E711 - the value under test
        "~ None ==": ~(name == None),  # noqa: E711
        "a key with a dot": _ref(player, "team", "meta", "a.b") == 1,
        "a key with a quote": _ref(player, "team", "meta", "it's") == 2,
    }


EXPECTED = {
    "name ==": [1],
    "name !=": [2, 3],
    "rank ==": [2],
    "rank !=": [1, 3],
    "rank <": [1],
    "rank <=": [1, 2],
    "rank >": [3],
    "rank >=": [2, 3],
    "~": [1, 3, 4],
    "|": [1, 3],
    "& the player's own field": [1],
    "& the player's own field, no match": [],
    "& two related fields": [3],
    "a field not promoted": [1, 3],
    "any() entry": [1],
    "any() entry rank": [1, 2],
    "_id": [2],
    "datetime ==": [1],
    "datetime <": [1],
    "None ==": [],
    "~ None ==": [1, 2, 3, 4],
    "a key with a dot": [1],
    "a key with a quote": [1],
}


@pytest.mark.parametrize("where", ["own table", "another table"])
@pytest.mark.parametrize("promoted", ["related", "outer", "both", "neither"])
@pytest.mark.parametrize("kind", list(BASES))
@pytest.mark.asyncio
async def test_a_reference_filter_finds_the_rows_whichever_side_promotes(kind, promoted, where):
    p = f"m{next(_names)}"
    team, player = _classes(kind, promoted, p)
    db = await _db(kind)
    try:
        await _seed(team, player, db)
        table = None
        if where == "another table":
            table = f"rp_{p}_archive"
            await _v(player.using(db).copy_to(table))
        found = {}
        for case, expr in _cases(player).items():
            qs = player.using(db, table) if table else player.using(db)
            found[case] = await _ids(qs.filter(expr))
        assert found == EXPECTED
    finally:
        await _v(db.close())


# ----- a field promoted after the statement was made ---------------------------------------


async def _promote(way: str, kind: str, team: type, db, path: Optional[str]) -> None:
    table = team.__tablename__
    if way == "ensure_table":
        await _v(db.ensure_table(table, promoted={"name": "TEXT"}))
    elif way == "a save of a class that declares it":
        fields = {"name": (str, ""), "rank": (int, 0), "city": (str, ""), "founded": (Optional[datetime], None), "meta": (Optional[dict], None)}
        declares = _model(kind, fields, table, {"__promoted__": {"name": "TEXT"}})
        await _v(declares(name="violet").save(db=db))
    else:  # another database object on the file
        other = await _db(kind, path)
        try:
            await _v(other.ensure_table(table, promoted={"name": "TEXT"}))
        finally:
            await _v(other.close())


@pytest.mark.parametrize("way", ["ensure_table", "a save of a class that declares it", "another database object"])
@pytest.mark.parametrize("kind", list(BASES))
@pytest.mark.asyncio
async def test_a_field_promoted_after_the_statement_was_made_is_followed(kind, way, tmp_path):
    p = f"l{next(_names)}"
    team, player = _classes(kind, "neither", p)
    path = str(tmp_path / "rows.db")
    db = await _db(kind, path)
    try:
        await _seed(team, player, db)
        qs = player.using(db).filter(_ref(player, "team", "name") == "red")
        not_qs = player.using(db).filter(~(_ref(player, "team", "name") == "red"))
        before = (await _ids(qs), await _ids(not_qs))
        await _promote(way, kind, team, db, path)
        after = (await _ids(qs), await _ids(not_qs))
        assert (before, after, "name" in await _columns(db, team.__tablename__)) == (([1], [2, 3, 4]), ([1], [2, 3, 4]), True)
    finally:
        await _v(db.close())


@pytest.mark.parametrize("kind", list(BASES))
@pytest.mark.asyncio
async def test_a_promotion_rolled_back_is_forgotten_by_the_statement(kind):
    """The related field is promoted only inside a transaction of this object's, which rolls
    back: the filter reads the column inside it and the documents after it."""
    p = f"r{next(_names)}"
    team, player = _classes(kind, "neither", p)
    db = await _db(kind)
    try:
        await _seed(team, player, db)
        qs = player.using(db).filter(_ref(player, "team", "name") == "red")
        seen = [await _ids(qs)]
        with pytest.raises(RuntimeError, match="^undo$"):
            if kind.startswith("async"):
                async with db.transaction():
                    await db.ensure_table(team.__tablename__, promoted={"name": "TEXT"})
                    seen.append(await _ids(qs))
                    raise RuntimeError("undo")
            else:
                with db.transaction():
                    db.ensure_table(team.__tablename__, promoted={"name": "TEXT"})
                    seen.append(await _ids(qs))
                    raise RuntimeError("undo")
        seen.append(await _ids(qs))
        assert (seen, "name" in await _columns(db, team.__tablename__)) == ([[1], [1], [1]], False)
    finally:
        await _v(db.close())


# ----- hostile inputs ------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(BASES))
@pytest.mark.asyncio
async def test_a_related_table_that_does_not_exist_yet_matches_no_row(kind):
    """No team was ever saved: the filter matches no player and its negation every player,
    and nothing raises; once the table is made, the same querysets find the row."""
    p = f"x{next(_names)}"
    team, player = _classes(kind, "related", p)
    db = await _db(kind)
    try:
        lone = await _v(player(name="lone").save(db=db))
        await _v(player(name="other").save(db=db))
        qs = player.using(db).filter(_ref(player, "team", "name") == "red")
        not_qs = player.using(db).filter(~(_ref(player, "team", "name") == "red"))
        before = (await _ids(qs), await _ids(not_qs))
        lone.team = await _v(team(name="red").save(db=db))
        await _v(lone.save(db=db))
        after = (await _ids(qs), await _ids(not_qs))
        assert (before, after) == (([], [1, 2]), ([1], [2]))
    finally:
        await _v(db.close())


@pytest.mark.parametrize("kind", ["pydantic", "async-pydantic"])
@pytest.mark.asyncio
async def test_a_related_class_bound_to_a_table_of_its_own_with_set_db(kind):
    """The related class is bound with ``set_db(db, table)`` to a table its name does not give,
    and promotes the field there."""
    p = f"b{next(_names)}"
    team, player = _classes(kind, "related", p)
    db = await _db(kind)
    try:
        team.set_db(db, f"rp_{p}_bound_teams")
        player.set_db(db, f"rp_{p}_bound_players")
        await _seed(team, player, db)
        found = await _ids(player.using(db).filter(_ref(player, "team", "name") == "red"))
        assert (found, "name" in await _columns(db, f"rp_{p}_bound_teams")) == ([1], True)
    finally:
        await _v(db.close())


@pytest.mark.parametrize("kind", ["pydantic", "async-pydantic"])
def test_the_filter_text_with_no_database_reads_the_documents(kind):
    """Built, before any database lays it out, the condition is the one a query on the related
    table writes, read from the related row (``rt``), whatever either class promotes."""
    p = f"t{next(_names)}"
    team, player = _classes(kind, "both", p)
    expr = _ref(player, "team", "name") == "red"
    assert (expr.sql, expr.params) == (
        f"EXISTS (SELECT 1 FROM (SELECT _id AS rid FROM rp_{p}_teams rt WHERE json_extract(rt.data, '$.name') = ?) r "
        "WHERE r.rid = json_extract(data, '$.team._id'))",
        ["red"],
    )


def test_a_query_with_no_reference_filter_sends_only_its_own_statement():
    """A plain filter reads no other table's layout: once its table is known, a run sends its
    one statement, and the filter through a reference sends its one statement too."""
    p = f"s{next(_names)}"
    team, player = _classes("pydantic", "related", p)
    db = SQLerDB.in_memory(shared=False)
    try:
        _seed_sync(team, player, db)
        plain = player.using(db).filter(F("name") == "red")
        through = player.using(db).filter(_ref(player, "team", "name") == "red")
        assert (plain.count(), through.count()) == (2, 1)  # each statement made once
        sent: list[str] = []
        db.adapter._conn().set_trace_callback(sent.append)
        try:
            plain.count()
            through.count()
        finally:
            db.adapter._conn().set_trace_callback(None)
        assert [s.split(" WHERE ")[0] for s in sent] == [f"SELECT COUNT(*) FROM rp_{p}_players"] * 2
    finally:
        db.close()


def _seed_sync(team: type, player: type, db) -> None:
    import asyncio

    asyncio.run(_seed(team, player, db))
