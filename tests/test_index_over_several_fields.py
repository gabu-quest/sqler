"""``create_index`` over several fields, each with a direction, on the column a field is stored in.

Every test runs on ``SQLerDB`` and on ``AsyncSQLerDB`` (``KINDS``); ``add_index`` and
``ensure_index`` run on every model base (``BASES``). An index is asserted by the text
``sqlite_master`` keeps of it, a query by the plan of the statement it really sends.
"""

import asyncio
import dataclasses
import inspect
import warnings
from typing import Any

import pytest

from sqler import (
    AsyncSQLerDB,
    AsyncSQLerLiteModel,
    AsyncSQLerLiteSafeModel,
    AsyncSQLerMsgspecModel,
    AsyncSQLerSafeModel,
    F,
    SQLerDB,
    SQLerLiteModel,
    SQLerLiteSafeModel,
    SQLerModel,
    SQLerMsgspecModel,
    SQLerSafeModel,
    UniqueConstraintError,
)
from sqler.db._statements import _create_index_sql
from sqler.exceptions import InvalidFieldNameError, InvalidIdentifierError, InvalidTableNameError
from sqler.logging import query_logger
from sqler.models.async_model import AsyncSQLerModel

KINDS = [SQLerDB, AsyncSQLerDB]
KIND_IDS = ["sync", "async"]


async def _v(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _run(kind: Any, body: Any, path: Any = None) -> Any:
    """``body(db)`` on a new database of ``kind`` (in memory, or on the file ``path``)."""

    async def go() -> Any:
        db = kind.on_disk(str(path)) if path else kind.in_memory(shared=False)
        await _v(db.connect())
        try:
            return await body(db)
        finally:
            await _v(db.close())

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return asyncio.run(go())


async def _rows(db: Any, sql: str, params: Any = None) -> list:
    """The rows of ``sql``, read with the adapter's own ``execute``."""
    cur = await _v(db.adapter.execute(sql, params or []))
    try:
        got = [tuple(r) for r in await _v(cur.fetchall())]
    finally:
        await _v(cur.close())
    await _v(db.adapter.auto_commit())
    return got


async def _indexes(db: Any, table: str = "jobs") -> list:
    return await _rows(
        db,
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? ORDER BY name",
        [table],
    )


async def _plan_of_sent(db: Any, query: Any) -> list[str]:
    """The plan of the ``SELECT`` on ``jobs`` that ``query.all()`` sends: its own steps, without
    the subqueries of the guard it carries (a ``CASE`` branch that never runs)."""
    with query_logger.capture() as sent:
        await _v(query.all())
    (log,) = [
        log for log in sent if log.sql.lstrip().upper().startswith("SELECT") and " jobs" in log.sql
    ]
    plan = await _rows(db, "EXPLAIN QUERY PLAN " + log.sql, log.params)
    return [row[3] for row in plan if row[1] == 0 and "SUBQUERY" not in row[3]]


async def _jobs(db: Any, promoted: Any = None, rows: Any = ()) -> None:
    if promoted:
        await _v(db.ensure_table("jobs", promoted=promoted))
    for row in rows:
        await _v(db.insert_document("jobs", dict(row)))


# ----- one field as a string: today's statement and name ----------------------------------


def test_one_field_as_a_string_is_the_statement_it_was():
    assert _create_index_sql("jobs", "queue", False, None, None) == (
        "CREATE  INDEX IF NOT EXISTS idx_jobs_queue ON jobs (json_extract(data, '$.queue')) ;"
    )


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
@pytest.mark.parametrize(
    "given", ["meta.level", ["meta.level"], [("meta.level", "asc")]], ids=["string", "list", "asc"]
)
def test_one_ascending_field_makes_the_index_it_made(kind, given):
    async def body(db):
        await _v(db.create_index("jobs", given))
        return await _indexes(db)

    assert _run(kind, body) == [
        (
            "idx_jobs_meta_level",
            "CREATE INDEX idx_jobs_meta_level ON jobs (json_extract(data, '$.meta.level')) ",
        )
    ]


# ----- several fields, each with a direction ----------------------------------------------


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_several_fields_make_one_index_in_their_order_with_their_directions(kind):
    async def body(db):
        await _v(db.create_index("jobs", ["queue", ("priority", "desc"), "eta"]))
        return await _indexes(db)

    assert _run(kind, body) == [
        (
            "idx_jobs_queue__priority_desc__eta",
            "CREATE INDEX idx_jobs_queue__priority_desc__eta ON jobs (json_extract(data, '$.queue'), "
            "json_extract(data, '$.priority') DESC, json_extract(data, '$.eta')) ",
        )
    ]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_query_on_the_first_field_ordered_by_the_second_descending_uses_the_index(kind):
    async def body(db):
        await _jobs(
            db,
            {"status": "TEXT", "priority": "INTEGER"},
            [{"queue": "a", "status": "pending", "priority": p, "eta": p} for p in range(5)],
        )
        await _v(
            db.create_index(
                "jobs", ["queue", ("priority", "desc"), "eta"], where="status = 'pending'"
            )
        )
        q = await _v(db.query("jobs"))
        q = q.filter((F("queue") == "a") & (F("status") == "pending")).order_by(
            "priority", desc=True
        )
        return await _plan_of_sent(db, q)

    assert _run(kind, body) == [
        "SEARCH jobs USING INDEX idx_jobs_queue__priority_desc__eta (<expr>=?)"
    ]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_lists_that_differ_in_a_direction_or_in_order_make_two_indexes(kind):
    async def body(db):
        await _v(db.create_index("jobs", ["a", "b"]))
        await _v(db.create_index("jobs", ["a", ("b", "desc")]))
        await _v(db.create_index("jobs", ["b", "a"]))
        await _v(db.create_index("jobs", [("a", "DESC")]))
        return [name for name, _ in await _indexes(db)]

    assert _run(kind, body) == [
        "idx_jobs_a__b",
        "idx_jobs_a__b_desc",
        "idx_jobs_a_desc",
        "idx_jobs_b__a",
    ]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_name_given_is_the_name_of_the_index(kind):
    async def body(db):
        await _v(db.create_index("jobs", ["a", ("b", "Desc")], name="by_a_b"))
        return await _indexes(db)

    assert _run(kind, body) == [
        (
            "by_a_b",
            "CREATE INDEX by_a_b ON jobs (json_extract(data, '$.a'), json_extract(data, '$.b') DESC) ",
        )
    ]


# ----- a field is indexed where the table stores it ---------------------------------------


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_promoted_field_is_indexed_as_its_column_and_a_filter_on_it_searches_the_index(kind):
    async def body(db):
        await _jobs(db, {"priority": "INTEGER"}, [{"queue": "a", "priority": p} for p in range(5)])
        await _v(db.create_index("jobs", "priority"))
        q = await _v(db.query("jobs"))
        return [await _indexes(db), await _plan_of_sent(db, q.filter(F("priority") == 3))]

    assert _run(kind, body) == [
        [("idx_jobs_priority", "CREATE INDEX idx_jobs_priority ON jobs (priority) ")],
        ["SEARCH jobs USING INDEX idx_jobs_priority (priority=?)"],
    ]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_list_that_mixes_a_column_a_path_and_a_nested_path_is_one_index(kind):
    async def body(db):
        await _jobs(db, {"priority": "INTEGER"})
        await _v(db.create_index("jobs", ["queue", ("priority", "desc"), "meta.level"]))
        return await _indexes(db)

    assert _run(kind, body) == [
        (
            "idx_jobs_queue__priority_desc__meta_level",
            "CREATE INDEX idx_jobs_queue__priority_desc__meta_level ON jobs (json_extract(data, '$.queue'), "
            "priority DESC, json_extract(data, '$.meta.level')) ",
        )
    ]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_id_and_version_are_indexed_as_their_columns(kind):
    async def body(db):
        await _v(db.ensure_table("jobs", versioned=True))
        await _v(db.create_index("jobs", ["_version", ("_id", "desc")]))
        return await _indexes(db)

    assert _run(kind, body) == [
        (
            "idx_jobs__version___id_desc",
            "CREATE INDEX idx_jobs__version___id_desc ON jobs (_version, _id DESC) ",
        )
    ]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_where_names_a_promoted_field_by_its_column_and_a_data_field_by_its_path(kind):
    async def body(db):
        await _jobs(
            db,
            {"status": "TEXT"},
            [
                {"status": "pending", "kind": "x", "eta": 1},
                {"status": "pending", "kind": "y", "eta": 2},
                {"status": "done", "kind": "x", "eta": 3},
            ],
        )
        await _v(
            db.create_index(
                "jobs",
                "eta",
                name="pending_x",
                where="status = 'pending' AND json_extract(data, '$.kind') = 'x'",
            )
        )
        indexed = await _rows(
            db,
            "SELECT _id FROM jobs INDEXED BY pending_x "
            "WHERE status = 'pending' AND json_extract(data, '$.kind') = 'x' AND json_extract(data, '$.eta') > 0",
        )
        return [await _indexes(db), indexed]

    assert _run(kind, body) == [
        [
            (
                "pending_x",
                "CREATE INDEX pending_x ON jobs (json_extract(data, '$.eta')) "
                "WHERE status = 'pending' AND json_extract(data, '$.kind') = 'x'",
            )
        ],
        [(1,)],
    ]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_field_promoted_through_another_object_on_the_file_is_indexed_as_its_column(
    kind, tmp_path
):
    path = tmp_path / "two.db"

    async def body(db):
        await _v(db.insert_document("jobs", {"priority": 1}))
        await _v(db.find_document("jobs", 1))  # this object has read the table before the promotion
        other = type(db).on_disk(str(path))
        await _v(other.connect())
        try:
            await _v(other.ensure_table("jobs", promoted={"priority": "INTEGER"}))
        finally:
            await _v(other.close())
        await _v(db.create_index("jobs", "priority"))
        return await _indexes(db)

    assert _run(kind, body, path) == [
        ("idx_jobs_priority", "CREATE INDEX idx_jobs_priority ON jobs (priority) ")
    ]


# ----- an index follows a promotion -------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_an_index_sqler_made_is_made_again_on_the_column_when_its_field_is_promoted(kind):
    async def body(db):
        await _jobs(
            db,
            None,
            [{"queue": "a", "priority": 1, "eta": 5}, {"queue": "a", "priority": 2, "eta": 0}],
        )
        await _v(
            db.create_index(
                "jobs",
                ["queue", ("priority", "desc")],
                unique=True,
                where="json_extract(data, '$.eta') > 0",
            )
        )
        await _v(db.create_index("jobs", "eta"))
        await _rows(db, "CREATE INDEX by_hand ON jobs (json_extract(data, '$.priority'))")
        await _v(db.ensure_table("jobs", promoted={"priority": "INTEGER"}))
        q = await _v(db.query("jobs"))
        return [
            await _indexes(db),
            await _plan_of_sent(
                db, q.filter((F("queue") == "a") & (F("eta") > 0)).order_by("priority", desc=True)
            ),
        ]

    assert _run(kind, body) == [
        [
            ("by_hand", "CREATE INDEX by_hand ON jobs (json_extract(data, '$.priority'))"),
            ("idx_jobs_eta", "CREATE INDEX idx_jobs_eta ON jobs (json_extract(data, '$.eta')) "),
            (
                "idx_jobs_queue__priority_desc",
                "CREATE UNIQUE INDEX idx_jobs_queue__priority_desc ON jobs (json_extract(data, '$.queue'), "
                "priority DESC) WHERE json_extract(data, '$.eta') > 0",
            ),
        ],
        ["SEARCH jobs USING INDEX idx_jobs_queue__priority_desc (<expr>=?)"],
    ]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_one_field_index_follows_ensure_table_promoting_its_field(kind):
    async def body(db):
        await _jobs(db, None, [{"priority": p} for p in range(3)])
        await _v(db.create_index("jobs", "priority"))
        await _v(db.ensure_table("jobs", promoted={"priority": "INTEGER"}))
        q = await _v(db.query("jobs"))
        return [await _indexes(db), await _plan_of_sent(db, q.filter(F("priority") == 2))]

    assert _run(kind, body) == [
        [("idx_jobs_priority", "CREATE INDEX idx_jobs_priority ON jobs (priority) ")],
        ["SEARCH jobs USING INDEX idx_jobs_priority (priority=?)"],
    ]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_an_index_follows_move_promoted(kind):
    async def body(db):
        await _v(db.ensure_table("jobs", promoted={"priority": "INTEGER"}))
        await _rows(db, "INSERT INTO jobs (data, priority) VALUES ('{\"priority\": 4}', 1)")
        await _rows(
            db, "CREATE INDEX idx_jobs_priority ON jobs (json_extract(data, '$.priority')) "
        )
        await _v(db.move_promoted("jobs", "priority", keep="document"))
        return [
            await _indexes(db),
            await _rows(
                db, "SELECT priority FROM jobs INDEXED BY idx_jobs_priority WHERE priority = 4"
            ),
        ]

    assert _run(kind, body) == [
        [("idx_jobs_priority", "CREATE INDEX idx_jobs_priority ON jobs (priority) ")],
        [(4,)],
    ]


class _PlainJob(SQLerModel):
    queue: str = ""
    priority: int = 0
    __tablename__ = "jobs"


class _PromotedJob(SQLerModel):
    queue: str = ""
    priority: int = 0
    __tablename__ = "jobs"
    __promoted__ = {"priority": "INTEGER NOT NULL DEFAULT 0"}


def test_an_index_follows_the_promotion_a_model_save_makes():
    async def body(db):
        _PlainJob(queue="a", priority=3).save(db=db)
        _PlainJob.add_index(["queue", ("priority", "desc")], db=db)
        _PromotedJob(queue="b", priority=1).save(db=db)
        q = db.query("jobs").filter(F("queue") == "a").order_by("priority", desc=True)
        return [
            await _indexes(db),
            await _rows(db, "SELECT _id, priority FROM jobs ORDER BY _id"),
            await _plan_of_sent(db, q),
        ]

    assert _run(SQLerDB, body) == [
        [
            (
                "idx_jobs_queue__priority_desc",
                "CREATE INDEX idx_jobs_queue__priority_desc ON jobs (json_extract(data, '$.queue'), priority DESC) ",
            )
        ],
        [(1, 3), (2, 1)],
        ["SEARCH jobs USING INDEX idx_jobs_queue__priority_desc (<expr>=?)"],
    ]


@dataclasses.dataclass
class _PlainLite(SQLerLiteModel):
    queue: str = ""
    priority: int = 0
    __tablename__ = "jobs"


@dataclasses.dataclass
class _PromotedLite(SQLerLiteModel):
    queue: str = ""
    priority: int = 0
    __tablename__ = "jobs"
    __promoted__ = {"priority": "INTEGER NOT NULL DEFAULT 0"}


class _PlainAsync(AsyncSQLerModel):
    queue: str = ""
    priority: int = 0
    __tablename__ = "jobs"


class _PromotedAsync(AsyncSQLerModel):
    queue: str = ""
    priority: int = 0
    __tablename__ = "jobs"
    __promoted__ = {"priority": "INTEGER NOT NULL DEFAULT 0"}


def test_an_index_follows_the_promotion_an_async_model_save_makes():
    async def body(db):
        await _PlainAsync(queue="a", priority=3).save(db=db)
        await _PlainAsync.add_index(["queue", ("priority", "desc")], db=db)
        await _PromotedAsync(queue="b", priority=1).save(db=db)
        return [await _indexes(db), await _rows(db, "SELECT _id, priority FROM jobs ORDER BY _id")]

    assert _run(AsyncSQLerDB, body) == [
        [
            (
                "idx_jobs_queue__priority_desc",
                "CREATE INDEX idx_jobs_queue__priority_desc ON jobs (json_extract(data, '$.queue'), priority DESC) ",
            )
        ],
        [(1, 3), (2, 1)],
    ]


def test_an_index_follows_the_promotion_a_lite_model_save_makes():
    async def body(db):
        _PlainLite(queue="a", priority=3).save(db=db)
        _PlainLite.add_index(["queue", ("priority", "desc")], db=db)
        _PromotedLite(queue="b", priority=1).save(db=db)
        return [await _indexes(db), await _rows(db, "SELECT _id, priority FROM jobs ORDER BY _id")]

    assert _run(SQLerDB, body) == [
        [
            (
                "idx_jobs_queue__priority_desc",
                "CREATE INDEX idx_jobs_queue__priority_desc ON jobs (json_extract(data, '$.queue'), priority DESC) ",
            )
        ],
        [(1, 3), (2, 1)],
    ]


# ----- a where names a field one way, promoted or not -------------------------------------

PENDING_PATH = "json_extract(data, '$.status') = 'pending'"
PENDING_INDEX = (
    "idx_jobs_queue",
    "CREATE UNIQUE INDEX idx_jobs_queue ON jobs (json_extract(data, '$.queue')) WHERE status = 'pending'",
)


async def _two_pending_then_a_filter_plan(db: Any) -> list:
    """The second pending row of one queue is refused; a done one is taken; then the plan of a filter."""
    await _v(db.insert_document("jobs", {"queue": "q", "status": "pending"}))
    refused = None
    try:
        await _v(db.insert_document("jobs", {"queue": "q", "status": "pending"}))
    except UniqueConstraintError as exc:
        refused = type(exc).__name__
    await _v(db.insert_document("jobs", {"queue": "q", "status": "done"}))
    q = await _v(db.query("jobs"))
    plan = await _plan_of_sent(db, q.filter((F("status") == "pending") & (F("queue") == "q")))
    return [await _indexes(db), refused, plan]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_where_by_json_path_follows_the_promotion_of_its_field(kind):
    async def body(db):
        await _v(db.create_index("jobs", "queue", unique=True, where=PENDING_PATH))
        await _v(db.ensure_table("jobs", promoted={"status": "TEXT"}))
        return await _two_pending_then_a_filter_plan(db)

    got = _run(kind, body)
    assert got[0] == [PENDING_INDEX]
    assert got[1] == "UniqueConstraintError"
    assert got[2][0].startswith("SEARCH jobs USING INDEX idx_jobs_queue")


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_where_by_json_path_on_an_already_promoted_field_reads_the_column(kind):
    async def body(db):
        await _v(db.ensure_table("jobs", promoted={"status": "TEXT"}))
        await _v(db.create_index("jobs", "queue", unique=True, where=PENDING_PATH))
        return await _two_pending_then_a_filter_plan(db)

    got = _run(kind, body)
    assert got[0] == [PENDING_INDEX]
    assert got[1] == "UniqueConstraintError"
    assert got[2][0].startswith("SEARCH jobs USING INDEX idx_jobs_queue")


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_where_that_names_the_column_or_an_unpromoted_field_is_left_byte_for_byte(kind):
    async def body(db):
        await _v(db.ensure_table("jobs", promoted={"status": "TEXT"}))
        await _v(db.create_index("jobs", "queue", name="a", where="status = 'pending'"))
        await _v(
            db.create_index("jobs", "queue", name="b", where="JSON_EXTRACT(data,'$.kind')='x'")
        )
        return await _indexes(db)

    assert [sql for _, sql in _run(kind, body)] == [
        "CREATE INDEX a ON jobs (json_extract(data, '$.queue')) WHERE status = 'pending'",
        "CREATE INDEX b ON jobs (json_extract(data, '$.queue')) WHERE JSON_EXTRACT(data,'$.kind')='x'",
    ]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_second_promotion_of_another_field_in_the_where_makes_the_index_again(kind):
    where = "json_extract(data, '$.status') = 'pending' AND json_extract(data, '$.kind') = 'x'"

    async def body(db):
        await _v(db.create_index("jobs", "queue", where=where))
        await _v(db.ensure_table("jobs", promoted={"status": "TEXT"}))
        first = await _indexes(db)
        await _v(db.ensure_table("jobs", promoted={"status": "TEXT", "kind": "TEXT"}))
        listed = await _v(db.list_indexes("jobs"))
        await _v(db.drop_index("idx_jobs_queue"))
        return [first, await _indexes(db), [i["name"] for i in listed]]

    assert _run(kind, body) == [
        [
            (
                "idx_jobs_queue",
                "CREATE INDEX idx_jobs_queue ON jobs (json_extract(data, '$.queue')) "
                "WHERE status = 'pending' AND json_extract(data, '$.kind') = 'x'",
            )
        ],
        [],
        ["idx_jobs_queue"],
    ]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_where_with_two_promoted_fields_and_one_that_is_not(kind):
    where = "json_extract(data,'$.status') = 'p' AND json_extract(data,'$.kind') = 'x' AND json_extract(data,'$.eta') > 0"

    async def body(db):
        await _v(db.ensure_table("jobs", promoted={"status": "TEXT", "kind": "TEXT"}))
        await _v(db.create_index("jobs", "queue", where=where))
        return await _indexes(db)

    assert _run(kind, body) == [
        (
            "idx_jobs_queue",
            "CREATE INDEX idx_jobs_queue ON jobs (json_extract(data, '$.queue')) "
            "WHERE status = 'p' AND kind = 'x' AND json_extract(data,'$.eta') > 0",
        )
    ]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_path_with_spaces_or_capitals_is_read_as_the_query_rewrite_reads_it(kind):
    # the query owner's rewrite takes json_extract(data, '$.f') in any case, with any space after
    # the comma, and leaves a space inside the parentheses or before the closing one; so does the index
    async def body(db):
        await _v(db.ensure_table("jobs", promoted={"status": "TEXT"}))
        await _v(
            db.create_index(
                "jobs", "queue", name="upper", where="JSON_EXTRACT(data,   '$.status') = 'p'"
            )
        )
        await _v(
            db.create_index(
                "jobs", "queue", name="spaced", where="JSON_EXTRACT( data , '$.status' ) = 'p'"
            )
        )
        return await _indexes(db)

    assert [sql[sql.index("WHERE") :] for _, sql in _run(kind, body)] == [
        "WHERE JSON_EXTRACT( data , '$.status' ) = 'p'",
        "WHERE status = 'p'",
    ]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_string_literal_that_holds_the_path_text_is_rewritten_like_the_query_rewrite_does(kind):
    # a known limit shared with the filters: the rewrite is a text replace and does not see quotes
    async def body(db):
        await _v(db.ensure_table("jobs", promoted={"status": "TEXT"}))
        await _v(
            db.create_index(
                "jobs",
                "queue",
                where="json_extract(data,'$.kind') = \"json_extract(data,'$.status')\"",
            )
        )
        return await _indexes(db)

    assert _run(kind, body)[0][1].endswith("WHERE json_extract(data,'$.kind') = \"status\"")


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_a_nested_path_in_the_where_is_not_the_promoted_field_beside_it(kind):
    async def body(db):
        await _v(db.ensure_table("jobs", promoted={"a_b": "TEXT"}))
        await _v(db.create_index("jobs", "queue", where="json_extract(data, '$.a.b') = 'x'"))
        return await _indexes(db)

    assert _run(kind, body)[0][1].endswith("WHERE json_extract(data, '$.a.b') = 'x'")


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_the_where_of_an_index_made_through_an_object_that_has_not_seen_the_promotion(
    kind, tmp_path
):
    path = tmp_path / "two.db"

    async def body(db):
        await _v(db.insert_document("jobs", {"queue": "q", "status": "pending"}))
        await _v(db.find_document("jobs", 1))
        other = type(db).on_disk(str(path))
        await _v(other.connect())
        try:
            await _v(other.ensure_table("jobs", promoted={"status": "TEXT"}))
        finally:
            await _v(other.close())
        await _v(db.create_index("jobs", "queue", where=PENDING_PATH))
        return await _indexes(db)

    assert _run(kind, body, path) == [
        (
            "idx_jobs_queue",
            "CREATE INDEX idx_jobs_queue ON jobs (json_extract(data, '$.queue')) WHERE status = 'pending'",
        )
    ]


# ----- a re-make that fails rolls the promotion back whole --------------------------------


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_an_index_that_cannot_be_made_again_rolls_the_promotion_back_whole(kind):
    async def body(db):
        await _jobs(db, None, [{"kind": "a"}, {"n": 1}, {"n": 2}])
        await _v(db.create_index("jobs", "kind", unique=True))
        before = [await _indexes(db), await _rows(db, "SELECT _id, data FROM jobs ORDER BY _id")]
        raised = None
        try:
            await _v(db.ensure_table("jobs", promoted={"kind": "TEXT DEFAULT 'x'"}))
        except UniqueConstraintError as exc:
            raised = type(exc).__name__
        names = [r[1] for r in await _rows(db, "PRAGMA table_info(jobs)")]
        after = [await _indexes(db), await _rows(db, "SELECT _id, data FROM jobs ORDER BY _id")]
        return [raised, names, after == before]

    assert _run(kind, body) == ["UniqueConstraintError", ["_id", "data"], True]


# ----- unique over several fields ---------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_unique_over_two_fields_refuses_a_row_equal_in_both_and_takes_one_equal_in_one(kind):
    async def body(db):
        await _v(db.create_index("jobs", ["a", "b"], unique=True))
        await _v(db.insert_document("jobs", {"a": 1, "b": 1}))
        await _v(db.insert_document("jobs", {"a": 1, "b": 2}))
        try:
            await _v(db.insert_document("jobs", {"a": 1, "b": 1}))
        except UniqueConstraintError as exc:
            refused = type(exc).__name__
        return [refused, await _rows(db, "SELECT data FROM jobs ORDER BY _id")]

    assert _run(kind, body) == ["UniqueConstraintError", [('{"a":1,"b":1}',), ('{"a":1,"b":2}',)]]


# ----- hostile inputs ---------------------------------------------------------------------

HOSTILE = [
    ("empty-list", ("jobs", []), {}, ValueError, "an index needs at least one field"),
    (
        "bad-direction",
        ("jobs", [("a", "down")]),
        {},
        ValueError,
        "the direction of an index field is 'asc' or 'desc', not 'down'",
    ),
    (
        "direction-with-space",
        ("jobs", [("a", "desc ")]),
        {},
        ValueError,
        "the direction of an index field is 'asc' or 'desc', not 'desc '",
    ),
    (
        "tuple-of-three",
        ("jobs", [("a", "desc", "x")]),
        {},
        ValueError,
        "an index field is a name or (name, 'asc' | 'desc'), not ('a', 'desc', 'x')",
    ),
    (
        "field-not-a-string",
        ("jobs", [(1, "asc")]),
        {},
        ValueError,
        "an index field is a name or (name, 'asc' | 'desc'), not (1, 'asc')",
    ),
    (
        "a-tuple-of-fields",
        ("jobs", ("a", "desc")),
        {},
        TypeError,
        "the fields of an index are a name or a list, not tuple",
    ),
    (
        "same-field-twice",
        ("jobs", ["a", ("a", "desc")]),
        {},
        ValueError,
        "the field 'a' is in the index twice",
    ),
    ("quote-in-field", ("jobs", ["a", "b'"]), {}, InvalidFieldNameError, None),
    ("space-in-field", ("jobs", ["a b"]), {}, InvalidFieldNameError, None),
    ("semicolon-in-field", ("jobs", [("a;", "desc")]), {}, InvalidFieldNameError, None),
    ("semicolon-in-column", ("jobs", ["_id;"]), {}, InvalidIdentifierError, None),
    ("quote-in-table", ("jo'bs", ["a"]), {}, InvalidTableNameError, None),
    ("space-in-table", ("jo bs", ["a"]), {}, InvalidTableNameError, None),
    ("semicolon-in-table", ("jobs;", "a"), {}, InvalidTableNameError, None),
    ("quote-in-name", ("jobs", ["a", "b"]), {"name": "i'x"}, InvalidIdentifierError, None),
    ("space-in-name", ("jobs", ["a"]), {"name": "i x"}, InvalidIdentifierError, None),
    ("semicolon-in-name", ("jobs", ["a"]), {"name": "ix;"}, InvalidIdentifierError, None),
]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
@pytest.mark.parametrize(
    "args, kwargs, error, message", [h[1:] for h in HOSTILE], ids=[h[0] for h in HOSTILE]
)
def test_a_hostile_argument_is_refused_before_any_statement(kind, args, kwargs, error, message):
    async def body(db):
        with query_logger.capture() as sent:
            with pytest.raises(error) as refused:
                await _v(db.create_index(*args, **kwargs))
        return [
            sent,
            await _rows(db, "SELECT name FROM sqlite_master"),
            message is None or str(refused.value) == message,
        ]

    assert _run(kind, body) == [[], [], True]


@pytest.mark.parametrize("kind", KINDS, ids=KIND_IDS)
def test_the_same_call_twice_makes_one_index(kind):
    async def body(db):
        for _ in range(2):
            await _v(db.create_index("jobs", ["a", ("b", "desc")], unique=True))
        return [name for name, _ in await _indexes(db)]

    assert _run(kind, body) == ["idx_jobs_a__b_desc"]


def test_eight_tasks_make_the_same_index_at_once_on_a_new_async_database():
    async def round_() -> list:
        db = AsyncSQLerDB.in_memory(shared=False)
        await db.connect()
        try:
            await asyncio.gather(*(db.create_index("jobs", ["a", ("b", "desc")]) for _ in range(8)))
            return await _indexes(db)
        finally:
            await db.close()

    async def run() -> list:
        return [await round_() for _ in range(20)]

    made = [
        (
            "idx_jobs_a__b_desc",
            "CREATE INDEX idx_jobs_a__b_desc ON jobs (json_extract(data, '$.a'), json_extract(data, '$.b') DESC) ",
        )
    ]
    assert asyncio.run(run()) == [made] * 20


# ----- add_index and ensure_index on every model base -------------------------------------

# (base, whether it is async, whether it is a dataclass)
BASES = [
    (SQLerModel, False, False),
    (SQLerSafeModel, False, False),
    (AsyncSQLerModel, True, False),
    (AsyncSQLerSafeModel, True, False),
    (SQLerLiteModel, False, True),
    (SQLerLiteSafeModel, False, True),
    (AsyncSQLerLiteModel, True, True),
    (AsyncSQLerLiteSafeModel, True, True),
    (SQLerMsgspecModel, False, False),
    (AsyncSQLerMsgspecModel, True, False),
]


def _model(base: Any, lite: bool) -> Any:
    ns = {
        "__module__": __name__,
        "__annotations__": {"queue": str, "priority": int},
        "queue": "",
        "priority": 0,
        "__tablename__": "jobs",
    }
    name = f"Indexed{base.__name__}"
    cls = type(name, (base,), {"__qualname__": name, **ns})
    return dataclasses.dataclass(cls) if lite else cls


@pytest.mark.parametrize("method", ["add_index", "ensure_index"])
@pytest.mark.parametrize("base, is_async, lite", BASES, ids=[b.__name__ for b, _, _ in BASES])
def test_every_model_base_indexes_several_fields_with_directions(base, is_async, lite, method):
    Job = _model(base, lite)

    async def body(db):
        await _v(db.ensure_table("jobs", promoted={"priority": "INTEGER"}))
        await _v(
            getattr(Job, method)(
                ["queue", ("priority", "desc")], unique=True, where="priority > 0", db=db
            )
        )
        return await _indexes(db)

    assert _run(AsyncSQLerDB if is_async else SQLerDB, body) == [
        (
            "idx_jobs_queue__priority_desc",
            "CREATE UNIQUE INDEX idx_jobs_queue__priority_desc ON jobs (json_extract(data, '$.queue'), "
            "priority DESC) WHERE priority > 0",
        )
    ]
