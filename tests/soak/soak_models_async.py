"""The model classes of the soak, on the async bases (the twins of ``soak_models_sync.py``).

Every class stores in the table ``soak_ops.TABLE`` names for it; the async twins in
``soak_models_async.py`` store in the same tables, so a sync and an async process write one
file through the same rows.
"""

import warnings
from dataclasses import dataclass
from typing import Optional

from sqler import (
    AsyncAuditLogMixin,
    AsyncPartialUpdateMixin,
    AsyncSearchableMixin,
    AsyncSQLerLiteModel,
    AsyncSQLerLiteSafeModel,
    AsyncSQLerModel,
    AsyncSQLerMsgspecModel,
    AsyncSQLerSafeModel,
    AsyncTrackedModel,
)

KIND = "async"


class PlainP(AsyncSQLerModel):
    __tablename__ = "soak_plain_p"
    owner: str
    key: str
    n: int = 0
    note: str = ""


@dataclass
class PlainL(AsyncSQLerLiteModel):
    __tablename__ = "soak_plain_l"
    owner: str = ""
    key: str = ""
    n: int = 0
    note: str = ""


class PlainM(AsyncSQLerMsgspecModel):
    __tablename__ = "soak_plain_m"
    owner: str = ""
    key: str = ""
    n: int = 0
    note: str = ""


class SafeP(AsyncPartialUpdateMixin, AsyncTrackedModel, AsyncSQLerSafeModel):
    __tablename__ = "soak_safe_p"
    owner: str
    key: str
    n: int = 0
    note: str = ""


@dataclass
class SafeL(AsyncSQLerLiteSafeModel):
    __tablename__ = "soak_safe_l"
    owner: str = ""
    key: str = ""
    n: int = 0
    note: str = ""


# ``count`` is the field the default rebase rule takes; it shadows the queryset ``count()``
warnings.filterwarnings("ignore", message='Field name "count" in "Counter" shadows')


class Counter(AsyncSQLerSafeModel):
    """A shared row: every worker adds 1 to ``count``; the default rebase rule takes it."""

    __tablename__ = "soak_counter"
    name: str
    count: int = 0


class Crew(AsyncSQLerModel):
    __tablename__ = "soak_crew"
    owner: str
    key: str
    n: int = 0


class Job(AsyncSQLerModel):
    __tablename__ = "soak_job"
    owner: str
    key: str
    n: int = 0
    crew: Optional[Crew] = None


class Promo(AsyncSQLerModel):
    """``prio`` is promoted by the class; ``stage`` is promoted while the writers run."""

    __tablename__ = "soak_promo"
    __promoted__ = {"prio": "INTEGER NOT NULL DEFAULT 0"}
    owner: str
    key: str
    prio: int = 0
    stage: str = "a"
    n: int = 0


class Audited(AsyncAuditLogMixin, AsyncSQLerModel):
    __tablename__ = "soak_audit"
    owner: str
    key: str
    n: int = 0


class SearchS(AsyncSearchableMixin, AsyncSQLerModel):
    """Search index with ``content_sync=True``: its triggers follow every write."""

    __tablename__ = "soak_search_s"
    owner: str
    key: str
    body: str = ""
    n: int = 0

    class FTS:
        fields = ["body"]


class SearchM(AsyncSearchableMixin, AsyncSQLerModel):
    """Search index with ``content_sync=False``: the mixin's ``save()``/``delete()`` keep it."""

    __tablename__ = "soak_search_m"
    owner: str
    key: str
    body: str = ""
    n: int = 0

    class FTS:
        fields = ["body"]


class Task(AsyncSQLerModel):
    __tablename__ = "soak_task"
    owner: str
    key: str
    done: bool = False
    n: int = 0


MODELS = {
    "plain_p": PlainP,
    "plain_l": PlainL,
    "plain_m": PlainM,
    "safe_p": SafeP,
    "safe_l": SafeL,
    "counter": Counter,
    "crew": Crew,
    "job": Job,
    "promo": Promo,
    "audit": Audited,
    "search_s": SearchS,
    "search_m": SearchM,
    "task": Task,
}


async def open_search_indexes(db) -> None:
    """Make (or find) both search indexes on ``db`` and hold them on their classes."""
    from sqler import AsyncFTSIndex

    SearchS._fts_index = AsyncFTSIndex(SearchS, ["body"], content_sync=True, db=db)
    await SearchS._fts_index.create(db)
    SearchM._fts_index = AsyncFTSIndex(SearchM, ["body"], content_sync=False, db=db)
    await SearchM._fts_index.create(db)
