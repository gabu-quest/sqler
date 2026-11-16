"""A filter through a reference field (``Model.ref(name).field(...)``) names the table each
class stores in: the outer table is the one of the model filtered, the related table the one
of the class the field's annotation admits, both by ``_model_table_name``.

``ROWS`` is the one table of shapes; each runs on ``SQLerModel`` and on ``AsyncSQLerModel``.
A row builds the filter, checks that its SQL names the related table (and the outer row's
``data`` by no table name), runs it on a database with
one matching and one other row, and checks the one row it returns. The classes are made per
base with names of their own, so the two bases never share a table.
"""

import types
from typing import Any, Optional

import pytest

from sqler import AsyncSQLerDB, SQLerDB
from sqler.models import AsyncSQLerModel, SQLerModel
from sqler.query import F


def _model(base: type, name: str, fields: dict[str, Any], tablename: Optional[str] = None) -> type:
    """A model class ``name`` on ``base`` with ``fields`` ({field: annotation}), published in
    this module so a string annotation can name it."""

    def body(ns):
        ns["__module__"] = __name__
        ns["__annotations__"] = {"name": str, **fields}
        ns["name"] = ""
        for field, ann in fields.items():
            if not isinstance(ann, str) or ann.startswith("Optional"):
                ns[field] = None
        if tablename is not None:
            ns["__tablename__"] = tablename

    cls = types.new_class(name, (base,), {}, body)
    globals()[name] = cls
    return cls


def _shapes(base: type, p: str) -> dict[str, tuple]:
    """``{row: (outer class, field, related class, any(), bind, outer table, related
    table)}`` on ``base``, the tables written out by hand; ``p`` keeps the names of the two
    bases apart."""
    t = p.lower()
    rows: dict[str, tuple] = {}

    squad = _model(base, f"Dn{p}squad", {})
    rows["tablename_on_the_outer_class_only"] = (
        _model(base, f"Dn{p}Fan", {f"dn{t}squad": Optional[squad]}, f"dn_{t}_fans"),
        f"dn{t}squad",
        squad,
        False,
        False,
        f"dn_{t}_fans",
        f"dn{t}squads",
    )
    band = _model(base, f"Dn{p}band", {}, f"dn_{t}_bands")
    rows["tablename_on_the_related_class_only"] = (
        _model(base, f"Dn{p}Fan2", {f"dn{t}band": Optional[band]}),
        f"dn{t}band",
        band,
        False,
        False,
        f"dn{t}fan2s",
        f"dn_{t}_bands",
    )
    team = _model(base, f"Dn{p}Team", {}, f"my_{t}_teams")
    rows["tablename_on_both_classes"] = (
        _model(base, f"Dn{p}Player", {"team": Optional[team]}, f"my_{t}_players"),
        "team",
        team,
        False,
        False,
        f"my_{t}_players",
        f"my_{t}_teams",
    )
    address = _model(base, f"Dn{p}Address", {})
    rows["related_class_named_unlike_the_field"] = (
        _model(base, f"Dn{p}Person", {"home": Optional[address]}),
        "home",
        address,
        False,
        False,
        f"dn{t}persons",
        f"dn{t}address",
    )
    # a class ``Dn<p>Team2`` stores in ``dn<p>team2s``, the table the field's name gives
    _model(base, f"Dn{p}Team2", {})
    club = _model(base, f"Dn{p}Club", {}, f"dn_{t}_clubs")
    rows["field_named_as_the_table_of_another_class"] = (
        _model(base, f"Dn{p}Member", {f"dn{t}team2": Optional[club]}),
        f"dn{t}team2",
        club,
        False,
        False,
        f"dn{t}members",
        f"dn_{t}_clubs",
    )
    rows["string_annotation"] = (
        _model(base, f"Dn{p}Reader", {"club": f"Optional[Dn{p}StrClub]"}),
        "club",
        _model(base, f"Dn{p}StrClub", {}, f"dn_{t}_str_clubs"),
        False,
        False,
        f"dn{t}readers",
        f"dn_{t}_str_clubs",
    )
    captain = _model(
        base, f"Dn{p}Captained", {"captain": f"Optional[Dn{p}Captained]"}, f"dn_{t}_crew"
    )
    rows["reference_to_its_own_class"] = (
        captain,
        "captain",
        captain,
        False,
        False,
        f"dn_{t}_crew",
        f"dn_{t}_crew",
    )
    leader = _model(base, f"Dn{p}Leader", {}, f"dn_{t}_leaders")
    rows["required_reference"] = (
        _model(base, f"Dn{p}Troop", {"leader": leader}, f"dn_{t}_troops"),
        "leader",
        leader,
        False,
        False,
        f"dn_{t}_troops",
        f"dn_{t}_leaders",
    )
    item = _model(base, f"Dn{p}Item", {}, f"dn_{t}_items")
    rows["list_of_references_with_any"] = (
        _model(base, f"Dn{p}Cart", {"items": list[item]}, f"dn_{t}_carts"),
        "items",
        item,
        True,
        False,
        f"dn_{t}_carts",
        f"dn_{t}_items",
    )
    owner = _model(base, f"Dn{p}Owner", {})
    rows["default_tables_on_both_classes"] = (
        _model(base, f"Dn{p}Pet", {f"dn{t}owner": Optional[owner]}),
        f"dn{t}owner",
        owner,
        False,
        False,
        f"dn{t}pets",
        f"dn{t}owners",
    )
    rows["classes_bound_with_set_db_to_tables_of_their_own"] = (
        _model(base, f"Dn{p}Boat", {"skipper": Optional[_model(base, f"Dn{p}Skipper", {})]}),
        "skipper",
        globals()[f"Dn{p}Skipper"],
        False,
        True,
        f"dn_{t}_bound_outer",
        f"dn_{t}_bound_related",
    )
    return rows


SYNC = _shapes(SQLerModel, "S")
ASYNC = _shapes(AsyncSQLerModel, "A")
ROWS = list(SYNC)


def _check_sql(sql: str, outer: str, related: str, any_: bool) -> None:
    """The filter reads the related table, and the outer row's ``data`` by no table name: it
    is the row of whatever table the query reads (``outer`` is the class's own)."""
    related_rows = f"(SELECT _id AS rid FROM {related} rt WHERE json_extract(rt.data, '$.name') = ?) r"
    if any_:
        expected = f"FROM json_each(data, '$.items') a JOIN {related_rows} ON "
    else:
        expected = f"FROM {related_rows} WHERE r.rid = json_extract(data, "
    assert expected in sql, sql
    assert f"{outer}.data" not in sql, sql


def _rows(outer: type, field: str, related_rows: list, any_: bool) -> list:
    if outer is related_rows[0].__class__:  # a reference to its own class
        return [outer(name=f"p-{r.name}", **{field: r}) for r in related_rows]
    value = (lambda r: [r]) if any_ else (lambda r: r)
    return [outer(name=f"p-{r.name}", **{field: value(r)}) for r in related_rows]


@pytest.mark.parametrize("row", ROWS)
def test_a_reference_filter_names_the_tables_of_both_classes(row):
    outer, field, related, any_, bind, outer_table, related_table = SYNC[row]
    db = SQLerDB.in_memory(shared=False)
    try:
        if bind:
            related.set_db(db, related_table)
            outer.set_db(db, outer_table)
        reds = [related(name=n).save(db=db) for n in ("red", "blue")]
        for o in _rows(outer, field, reds, any_):
            o.save(db=db)
        ref = outer.ref(field)
        expr = (ref.any() if any_ else ref).field("name") == "red"
        _check_sql(expr.sql, outer_table, related_table, any_)
        found = outer.using(db).filter(expr).all()
        assert [o.name for o in found] == ["p-red"]
    finally:
        db.close()


@pytest.mark.parametrize("row", ROWS)
@pytest.mark.asyncio
async def test_a_reference_filter_names_the_tables_of_both_classes_async(row):
    outer, field, related, any_, bind, outer_table, related_table = ASYNC[row]
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    try:
        if bind:
            related.set_db(db, related_table)
            outer.set_db(db, outer_table)
        reds = [await related(name=n).save(db=db) for n in ("red", "blue")]
        for o in _rows(outer, field, reds, any_):
            await o.save(db=db)
        ref = outer.ref(field)
        expr = (ref.any() if any_ else ref).field("name") == "red"
        _check_sql(expr.sql, outer_table, related_table, any_)
        found = await outer.using(db).filter(expr).all()
        assert [o.name for o in found] == ["p-red"]
    finally:
        await db.close()


@pytest.mark.parametrize(
    "base,p", [(SQLerModel, "S"), (AsyncSQLerModel, "A")], ids=["sync", "async"]
)
def test_a_field_that_admits_no_model_class_keeps_the_table_of_its_name(base, p):
    holder = _model(base, f"Dn{p}Blobbed", {"blob": dict}, f"dn_{p.lower()}_blobbed")
    expr = holder.ref("blob").field("name") == "red"
    assert expr.sql == (
        "EXISTS (SELECT 1 FROM (SELECT _id AS rid FROM blobs rt WHERE json_extract(rt.data, "
        "'$.name') = ?) r WHERE r.rid = json_extract(data, '$.blob._id'))"
    )


@pytest.mark.parametrize(
    "base,p", [(SQLerModel, "S"), (AsyncSQLerModel, "A")], ids=["sync", "async"]
)
def test_a_field_that_admits_classes_of_two_tables_is_refused(base, p):
    one = _model(base, f"Dn{p}UnionOne", {}, f"dn_{p.lower()}_union_one")
    two = _model(base, f"Dn{p}UnionTwo", {}, f"dn_{p.lower()}_union_two")
    holder = _model(base, f"Dn{p}UnionHolder", {"pick": Optional[one | two]})
    with pytest.raises(
        ValueError, match=rf"^Dn{p}UnionHolder\.pick: its annotation admits classes"
    ):
        holder.ref("pick").field("name") == "red"  # noqa: B015 - the build under test


# ----- a reference filter on a queryset of another table (``using(db, table)``) ----------
#
# The outer rows are copied by plain SQL into ``<outer table>_arch``; the filter on a
# queryset of that table finds the copy of the one matching row there, whatever table the
# outer class stores in, and the rows of the class's own table are not read.


def _archived(outer_table: str) -> tuple[str, str]:
    arch = f"{outer_table}_arch"
    return arch, f"CREATE TABLE {arch} AS SELECT * FROM {outer_table}"


@pytest.mark.parametrize("row", ROWS)
def test_a_reference_filter_on_a_queryset_of_another_table(row):
    outer, field, related, any_, bind, outer_table, related_table = SYNC[row]
    db = SQLerDB.in_memory(shared=False)
    try:
        if bind:
            related.set_db(db, related_table)
            outer.set_db(db, outer_table)
        reds = [related(name=n).save(db=db) for n in ("red", "blue")]
        for o in _rows(outer, field, reds, any_):
            o.save(db=db)
        arch, copy = _archived(outer_table)
        db.adapter.execute(copy)
        db.adapter.execute(f"UPDATE {arch} SET data = json_set(data, '$.name', 'a-' || json_extract(data, '$.name'))")
        db.adapter.commit()
        ref = outer.ref(field)
        expr = (ref.any() if any_ else ref).field("name") == "red"
        found = outer.using(db, arch).filter(expr).all()
        assert [o.name for o in found] == ["a-p-red"]
    finally:
        db.close()


@pytest.mark.parametrize("row", ROWS)
@pytest.mark.asyncio
async def test_a_reference_filter_on_a_queryset_of_another_table_async(row):
    outer, field, related, any_, bind, outer_table, related_table = ASYNC[row]
    db = AsyncSQLerDB.in_memory(shared=False)
    await db.connect()
    try:
        if bind:
            related.set_db(db, related_table)
            outer.set_db(db, outer_table)
        reds = [await related(name=n).save(db=db) for n in ("red", "blue")]
        for o in _rows(outer, field, reds, any_):
            await o.save(db=db)
        arch, copy = _archived(outer_table)
        async with db.transaction():
            await db.adapter.execute(copy)
            await db.adapter.execute(
                f"UPDATE {arch} SET data = json_set(data, '$.name', 'a-' || json_extract(data, '$.name'))"
            )
        ref = outer.ref(field)
        expr = (ref.any() if any_ else ref).field("name") == "red"
        found = await outer.using(db, arch).filter(expr).all()
        assert [o.name for o in found] == ["a-p-red"]
    finally:
        await db.close()


@pytest.mark.parametrize(
    "base,p", [(SQLerModel, "S"), (AsyncSQLerModel, "A")], ids=["sync", "async"]
)
@pytest.mark.asyncio
async def test_a_reference_filter_beside_a_promoted_field_of_the_queryset_table(base, p):
    """The outer class keeps ``name`` in a column of its own; a filter on it and through the
    reference, on the class's table and on another table in its layout, finds one row."""
    t = p.lower()
    team = _model(base, f"Dn{p}PrTeam", {}, f"dn_{t}_pr_teams")
    player = _model(base, f"Dn{p}PrPlayer", {"team": Optional[team]}, f"dn_{t}_pr_players")
    player.__promoted__ = {"name": "TEXT NOT NULL DEFAULT ''"}
    is_async = base is AsyncSQLerModel
    db = AsyncSQLerDB.in_memory(shared=False) if is_async else SQLerDB.in_memory(shared=False)

    async def v(x):
        return await x if is_async else x

    if is_async:
        await db.connect()
    try:
        red = await v(team(name="red").save(db=db))
        blue = await v(team(name="blue").save(db=db))
        for name, t_ in (("ann", red), ("bob", blue), ("cid", red)):
            await v(player(name=name, team=t_).save(db=db))
        await v(db.ensure_table(f"dn_{t}_pr_arch", promoted={"name": "TEXT NOT NULL DEFAULT ''"}))
        await v(db.adapter.execute(f"INSERT INTO dn_{t}_pr_arch SELECT * FROM dn_{t}_pr_players"))
        await v(db.adapter.commit())
        expr = (player.ref("team").field("name") == "red") & (F("name") == "cid")
        own = await v(player.using(db).filter(expr).all())
        arch = await v(player.using(db, f"dn_{t}_pr_arch").filter(expr).all())
        assert ([(o._id, o.name) for o in own], [(o._id, o.name) for o in arch]) == ([(3, "cid")], [(3, "cid")])
    finally:
        await v(db.close())
