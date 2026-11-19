"""AuditLogMixin: an audit row is made of stored documents.

``changes`` names the keys whose stored value differs between the row read before the save and
the document the save wrote; ``snapshot`` is the written document. A value nobody changed is
not reported, whatever its type, and a document with a datetime, a date or a reference saves.
"""

import asyncio
import enum
import inspect
import json
import os
import pathlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any, NamedTuple, Optional

import pydantic
import pytest
from sqler import AsyncSQLerDB, AsyncSQLerSafeModel, SQLerDB, SQLerModel, SQLerSafeModel
from sqler.exceptions import NotFoundError
from sqler.models.async_model import AsyncSQLerModel
from sqler.models.lite.async_model import AsyncSQLerLiteModel
from sqler.models.mixins import AsyncAuditLogMixin, AuditLogMixin, TimestampMixin
from sqler.models.msgspec.model import SQLerMsgspecModel

DAY = date(2026, 11, 14)
DAY_TEXT = "2026-11-14"
WHEN = datetime(2026, 11, 14, 9, 30, 0)
WHEN_TEXT = "2026-11-14T09:30:00"
WHEN_UTC = datetime(2026, 11, 14, 9, 30, 0, tzinfo=timezone.utc)
WHEN_UTC_TEXT = "2026-11-14T09:30:00+00:00"
UID = uuid.UUID("12345678-1234-5678-1234-567812345678")
UID_TEXT = "12345678-1234-5678-1234-567812345678"


class Colour(enum.Enum):
    RED = "red"


class Inner(pydantic.BaseModel):
    day: date


class Base(NamedTuple):
    cls: Any
    mixin: Any
    is_async: bool
    typed: bool  # declares real annotations (pydantic); else the field is ``Any``


BASES = {
    "pydantic": Base(SQLerModel, AuditLogMixin, False, True),
    "msgspec": Base(SQLerMsgspecModel, AuditLogMixin, False, False),
    "async": Base(AsyncSQLerModel, AsyncAuditLogMixin, True, True),
    "async-lite": Base(AsyncSQLerLiteModel, AsyncAuditLogMixin, True, False),
}
BASE_IDS = list(BASES)


def make(spec: Base, name: str, **fields):
    """An audited model class with ``name: str`` and ``fields`` ({field: (annotation, default)})."""
    ann = {"name": str}
    body: dict[str, Any] = {"name": ""}
    for key, (annotation, default) in fields.items():
        ann[key] = annotation if spec.typed else Any
        body[key] = default
    body["__annotations__"] = ann
    kwargs = {"kw_only": True, "tag": False} if spec.cls is SQLerMsgspecModel else {}
    cls = type(name, (spec.mixin, spec.cls), body, **kwargs)
    return dataclass(cls) if spec.cls is AsyncSQLerLiteModel else cls


async def _aw(value):
    return await value if inspect.isawaitable(value) else value


class Rows:
    def __init__(self, is_async: bool):
        self.is_async = is_async

    async def __aenter__(self):
        if self.is_async:
            self.db = AsyncSQLerDB.in_memory(shared=False)
            await self.db.connect()
        else:
            self.db = SQLerDB.in_memory(shared=False)
        return self

    async def __aexit__(self, *exc):
        await _aw(self.db.close())

    async def sql(self, statement: str) -> list[tuple]:
        cur = await _aw(self.db.adapter.execute(statement))
        rows = [tuple(r) for r in await _aw(cur.fetchall())]
        await _aw(cur.close())
        await _aw(self.db.adapter.auto_commit())
        return rows

    async def audit(self, table: str) -> list[tuple]:
        """``(action, changes, snapshot)`` of each audit row, the two columns parsed."""
        rows = await self.sql(f"SELECT action, changes, snapshot FROM {table}_audit ORDER BY _id")
        return [(a, c and json.loads(c), json.loads(s)) for a, c, s in rows]


def _run(steps, is_async: bool):
    async def scenario():
        async with Rows(is_async) as rows:
            return await steps(rows)

    return asyncio.run(scenario())


def _bind(rows, table, *classes):
    for cls, tbl in zip(classes, [table, "owners"]):
        cls.set_db(rows.db, tbl)


# ---- an update of one field reports that field, whatever the other fields hold --------------

OTHER_FIELDS = [
    pytest.param("plain", {"n": (int, 1)}, {"n": 1}, id="plain"),
    pytest.param("dated", {"when": (Optional[datetime], WHEN)}, {"when": WHEN_TEXT}, id="datetime"),
    pytest.param("dayed", {"day": (Optional[date], DAY)}, {"day": DAY_TEXT}, id="date"),
]


@pytest.mark.parametrize("base", BASE_IDS)
@pytest.mark.parametrize("label, fields, stored", OTHER_FIELDS)
def test_update_of_name_reports_only_name(base, label, fields, stored):
    spec = BASES[base]

    async def steps(rows):
        cls = make(spec, "Other" + label, **fields)
        _bind(rows, "items", cls)
        item = await _aw(cls(name="a", **{k: v[1] for k, v in fields.items()}).save())
        item.name = "b"
        await _aw(item.save())
        log = await _aw(item.get_audit_log())
        return await rows.audit("items"), log

    audit, log = _run(steps, spec.is_async)
    assert audit == [
        ("create", None, {"name": "a", **stored}),
        ("update", {"name": {"old": "a", "new": "b"}}, {"name": "b", **stored}),
    ]
    assert [(e["action"], e["changes"], e["snapshot"]) for e in log] == audit[::-1]


@pytest.mark.parametrize("base", BASE_IDS)
def test_reference_field_create_update_and_repoint(base):
    spec = BASES[base]

    async def steps(rows):
        owner_cls = make(spec, "Owner")
        cls = make(spec, "Owned", owner=(Optional[owner_cls], None))
        _bind(rows, "owned", cls, owner_cls)
        first = await _aw(owner_cls(name="o1").save())
        second = await _aw(owner_cls(name="o2").save())
        item = await _aw(cls(name="a", owner=first).save())
        item.name = "b"
        await _aw(item.save())
        item.owner = second
        await _aw(item.save())
        return await rows.audit("owned"), first._id, second._id

    audit, one, two = _run(steps, spec.is_async)
    ref1, ref2 = {"_table": "owners", "_id": one}, {"_table": "owners", "_id": two}
    assert audit == [
        ("create", None, {"name": "a", "owner": ref1}),
        ("update", {"name": {"old": "a", "new": "b"}}, {"name": "b", "owner": ref1}),
        ("update", {"owner": {"old": ref1, "new": ref2}}, {"name": "b", "owner": ref2}),
    ]


@pytest.mark.parametrize("base", BASE_IDS)
def test_date_datetime_and_uuid_fields_two_changed(base):
    spec = BASES[base]
    fields = {
        "day": (Optional[date], DAY),
        "when": (Optional[datetime], WHEN),
        "uid": (Optional[uuid.UUID], UID),
    }

    async def steps(rows):
        cls = make(spec, "Sv", **fields)
        _bind(rows, "sv", cls)
        item = await _aw(cls(name="a", day=DAY, when=WHEN, uid=UID).save())
        item.day = date(2026, 11, 15)
        item.when = WHEN_UTC
        await _aw(item.save())
        return await rows.audit("sv")

    created = {"name": "a", "day": DAY_TEXT, "when": WHEN_TEXT, "uid": UID_TEXT}
    assert _run(steps, spec.is_async) == [
        ("create", None, created),
        (
            "update",
            {
                "day": {"old": DAY_TEXT, "new": "2026-11-15"},
                "when": {"old": WHEN_TEXT, "new": WHEN_UTC_TEXT},
            },
            {**created, "day": "2026-11-15", "when": WHEN_UTC_TEXT},
        ),
    ]


# ---- unchanged fields of every kind that has a stored form ----------------------------------

UNCHANGED = {
    "colour": (Colour, Colour.RED, "red"),
    "tags": (set, {"b", "a"}, ["a", "b"]),
    "pair": (tuple, (1, 2), [1, 2]),
    "inner": (Inner, Inner(day=DAY), {"day": DAY_TEXT}),
    # a path is stored as str(path): the form of the OS that wrote it
    "path": (pathlib.Path, pathlib.Path("a", "b"), "a\\b" if os.sep == "\\" else "a/b"),
    "at": (time, time(9, 30), "09:30:00"),
}


@pytest.mark.parametrize("base", BASE_IDS)
def test_unchanged_kinds_beside_a_changed_str_only_the_str_is_reported(base):
    spec = BASES[base]

    async def steps(rows):
        cls = make(spec, "Kinds", **{k: (a, None) for k, (a, _, _) in UNCHANGED.items()})
        _bind(rows, "kinds", cls)
        item = await _aw(cls(name="a", **{k: v for k, (_, v, _) in UNCHANGED.items()}).save())
        item.name = "b"
        await _aw(item.save())
        return await rows.audit("kinds")

    stored = {k: s for k, (_, _, s) in UNCHANGED.items()}
    assert _run(steps, spec.is_async) == [
        ("create", None, {"name": "a", **stored}),
        ("update", {"name": {"old": "a", "new": "b"}}, {"name": "b", **stored}),
    ]


@pytest.mark.parametrize("base", BASE_IDS)
def test_a_list_edited_in_place_is_reported_with_both_lists(base):
    spec = BASES[base]

    async def steps(rows):
        cls = make(spec, "Tagged", tags=(list, None))
        _bind(rows, "tagged", cls)
        item = await _aw(cls(name="a", tags=["x"]).save())
        item.tags.append("y")
        await _aw(item.save())
        return await rows.audit("tagged")

    assert _run(steps, spec.is_async) == [
        ("create", None, {"name": "a", "tags": ["x"]}),
        ("update", {"tags": {"old": ["x"], "new": ["x", "y"]}}, {"name": "a", "tags": ["x", "y"]}),
    ]


@pytest.mark.parametrize("base", BASE_IDS)
def test_an_update_that_changes_nothing_writes_no_audit_row(base):
    spec = BASES[base]

    async def steps(rows):
        cls = make(spec, "Still", day=(Optional[date], DAY))
        _bind(rows, "still", cls)
        item = await _aw(cls(name="a", day=DAY).save())
        await _aw(item.save())
        return [a for a, _, _ in await rows.audit("still")]

    assert _run(steps, spec.is_async) == ["create"]


# ---- failures -------------------------------------------------------------------------------


@pytest.mark.parametrize("base", BASE_IDS)
def test_a_decimal_field_is_saved_and_audited_as_a_number(base):
    spec = BASES[base]

    async def steps(rows):
        cls = make(spec, "Priced", price=(Optional[Decimal], None))
        _bind(rows, "priced", cls)
        item = await _aw(cls(name="a", price=Decimal("1.5")).save())
        item.price = Decimal("2.25")
        await _aw(item.save())
        return await rows.audit("priced")

    assert _run(steps, spec.is_async) == [
        ("create", None, {"name": "a", "price": 1.5}),
        ("update", {"price": {"old": 1.5, "new": 2.25}}, {"name": "a", "price": 2.25}),
    ]


@pytest.mark.parametrize("base", BASE_IDS)
def test_a_decimal_a_number_cannot_hold_raises_and_leaves_nothing(base):
    spec = BASES[base]

    async def steps(rows):
        cls = make(spec, "Priced", price=(Optional[Decimal], None))
        _bind(rows, "priced", cls)
        await _aw(cls(name="seed").save())
        item = cls(name="a", price=Decimal("0.1234567890123456789"))
        with pytest.raises(TypeError, match="a number cannot hold it"):
            await _aw(item.save())
        return (
            item._id,
            await rows.sql("SELECT json_extract(data, '$.name') FROM priced ORDER BY _id"),
            [a for a, _, _ in await rows.audit("priced")],
        )

    assert _run(steps, spec.is_async) == (None, [("seed",)], ["create"])


@pytest.mark.parametrize("base", BASE_IDS)
def test_a_related_model_never_saved_raises_and_writes_no_audit_row(base):
    spec = BASES[base]

    async def steps(rows):
        owner_cls = make(spec, "Owner")
        cls = make(spec, "Owned", owner=(Optional[owner_cls], None))
        _bind(rows, "owned", cls, owner_cls)
        with pytest.raises(ValueError):
            await _aw(cls(name="a", owner=owner_cls(name="o")).save())
        return await rows.sql("SELECT name FROM sqlite_master WHERE name = 'owned_audit'")

    assert _run(steps, spec.is_async) == []


# ---- delete, promoted column, a key the class has no field for ------------------------------


@pytest.mark.parametrize("base", BASE_IDS)
def test_delete_snapshot_is_the_stored_document(base):
    spec = BASES[base]

    async def steps(rows):
        cls = make(spec, "Gone", when=(Optional[datetime], WHEN))
        _bind(rows, "gone", cls)
        item = await _aw(cls(name="a", when=WHEN).save())
        await _aw(item.delete())
        return await rows.audit("gone")

    assert _run(steps, spec.is_async) == [
        ("create", None, {"name": "a", "when": WHEN_TEXT}),
        ("delete", None, {"name": "a", "when": WHEN_TEXT}),
    ]


@pytest.mark.parametrize("base", BASE_IDS)
def test_a_promoted_field_changed_is_reported_once(base):
    spec = BASES[base]

    async def steps(rows):
        cls = make(spec, "Promo", status=(str, "new"))
        cls.__promoted__ = {"status": "TEXT NOT NULL DEFAULT 'new'"}
        _bind(rows, "promo", cls)
        item = await _aw(cls(name="a").save())
        item.status = "done"
        await _aw(item.save())
        return await rows.audit("promo")

    assert _run(steps, spec.is_async) == [
        ("create", None, {"name": "a", "status": "new"}),
        ("update", {"status": {"old": "new", "new": "done"}}, {"name": "a", "status": "done"}),
    ]


@pytest.mark.parametrize("base", BASE_IDS)
def test_a_stored_key_the_class_has_no_field_for_reads_none_on_the_new_side(base):
    spec = BASES[base]

    async def steps(rows):
        cls = make(spec, "Extra")
        _bind(rows, "extra", cls)
        item = await _aw(cls(name="a").save())
        await rows.sql("UPDATE extra SET data = json_set(data, '$.stray', 5)")
        item.name = "b"
        await _aw(item.save())
        return await rows.audit("extra")

    assert _run(steps, spec.is_async)[1:] == [
        (
            "update",
            {"name": {"old": "a", "new": "b"}, "stray": {"old": 5, "new": None}},
            {"name": "b"},
        )
    ]


@pytest.mark.parametrize("base", BASE_IDS)
def test_a_row_deleted_behind_the_model_writes_no_update_row(base):
    spec = BASES[base]

    async def steps(rows):
        cls = make(spec, "Vanished")
        _bind(rows, "vanished", cls)
        item = await _aw(cls(name="a").save())
        await rows.sql("DELETE FROM vanished")
        item.name = "b"
        with pytest.raises(NotFoundError, match="Row 1 of vanished is not there"):
            await _aw(item.save())
        return [a for a, _, _ in await rows.audit("vanished")]

    assert _run(steps, spec.is_async) == ["create"]


# ---- a versioned model with timestamps: the shape of examples/fastapi/models.py -------------


class Writer(AuditLogMixin, TimestampMixin, SQLerSafeModel):
    name: str = ""
    count: int = 0


class AsyncWriter(AsyncAuditLogMixin, TimestampMixin, AsyncSQLerSafeModel):
    name: str = ""
    count: int = 0


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_versioned_model_with_timestamps_is_audited(is_async):
    cls = AsyncWriter if is_async else Writer

    async def steps(rows):
        cls.set_db(rows.db, "writers")
        item = await _aw(cls(name="a").save())
        item.name = "b"
        await _aw(item.save())
        return await rows.audit("writers"), item.created_at, item.updated_at

    (create, update), created_at, updated_at = _run(steps, is_async)
    born = created_at.isoformat()
    assert create == (
        "create",
        None,
        {"name": "a", "count": 0, "created_at": born, "updated_at": born},
    )
    assert updated_at > created_at
    assert update == (
        "update",
        {
            "name": {"old": "a", "new": "b"},
            "updated_at": {"old": born, "new": updated_at.isoformat()},
        },
        {"name": "b", "count": 0, "created_at": born, "updated_at": updated_at.isoformat()},
    )


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_a_save_merged_with_a_newer_row_is_audited_against_that_row(is_async):
    cls = AsyncWriter if is_async else Writer

    async def steps(rows):
        cls.set_db(rows.db, "writers")
        first = await _aw(cls(name="a", count=10).save())
        mine = await _aw(cls.from_id(first._id))
        first.count += 1
        await _aw(first.save())  # the other writer: 10 -> 11
        mine.count += 1  # on the stale row: merged onto 11, gives 12
        mine.name = "mine"
        await _aw(mine.save())
        audit = await rows.audit("writers")
        return [(a, c and {k: v for k, v in c.items() if k != "updated_at"}) for a, c, _ in audit]

    assert _run(steps, is_async) == [
        ("create", None),
        ("update", {"count": {"old": 10, "new": 11}}),
        ("update", {"count": {"old": 11, "new": 12}, "name": {"old": "a", "new": "mine"}}),
    ]
