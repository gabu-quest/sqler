"""The model classes of the soak, on the sync bases (one process imports this or its async twin).

Every class stores in the table ``soak_ops.TABLE`` names for it; the async twins in
``soak_models_async.py`` store in the same tables, so a sync and an async process write one
file through the same rows.
"""

import warnings
from dataclasses import dataclass
from typing import Optional

from sqler import (
    AuditLogMixin,
    PartialUpdateMixin,
    SearchableMixin,
    SQLerLiteModel,
    SQLerLiteSafeModel,
    SQLerModel,
    SQLerMsgspecModel,
    SQLerSafeModel,
    TrackedModel,
)

KIND = "sync"


class PlainP(SQLerModel):
    __tablename__ = "soak_plain_p"
    owner: str
    key: str
    n: int = 0
    note: str = ""


@dataclass
class PlainL(SQLerLiteModel):
    __tablename__ = "soak_plain_l"
    owner: str = ""
    key: str = ""
    n: int = 0
    note: str = ""


class PlainM(SQLerMsgspecModel):
    __tablename__ = "soak_plain_m"
    owner: str = ""
    key: str = ""
    n: int = 0
    note: str = ""


class SafeP(PartialUpdateMixin, TrackedModel, SQLerSafeModel):
    __tablename__ = "soak_safe_p"
    owner: str
    key: str
    n: int = 0
    note: str = ""


@dataclass
class SafeL(SQLerLiteSafeModel):
    __tablename__ = "soak_safe_l"
    owner: str = ""
    key: str = ""
    n: int = 0
    note: str = ""


# ``count`` is the field the default rebase rule takes; it shadows the queryset ``count()``
warnings.filterwarnings("ignore", message='Field name "count" in "Counter" shadows')


class Counter(SQLerSafeModel):
    """A shared row: every worker adds 1 to ``count``; the default rebase rule takes it."""

    __tablename__ = "soak_counter"
    name: str
    count: int = 0


class Crew(SQLerModel):
    __tablename__ = "soak_crew"
    owner: str
    key: str
    n: int = 0


class Job(SQLerModel):
    __tablename__ = "soak_job"
    owner: str
    key: str
    n: int = 0
    crew: Optional[Crew] = None


class Promo(SQLerModel):
    """``prio`` is promoted by the class; ``stage`` is promoted while the writers run."""

    __tablename__ = "soak_promo"
    __promoted__ = {"prio": "INTEGER NOT NULL DEFAULT 0"}
    owner: str
    key: str
    prio: int = 0
    stage: str = "a"
    n: int = 0


class Audited(AuditLogMixin, SQLerModel):
    __tablename__ = "soak_audit"
    owner: str
    key: str
    n: int = 0


class SearchS(SearchableMixin, SQLerModel):
    """Search index: its triggers follow every write."""

    __tablename__ = "soak_search_s"
    owner: str
    key: str
    body: str = ""
    n: int = 0

    class FTS:
        fields = ["body"]


class Task(SQLerModel):
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
    "task": Task,
}


def open_search_indexes(db) -> None:
    """Make (or find) the search index on ``db`` and hold it on its class."""
    from sqler import FTSIndex

    SearchS._fts_index = FTSIndex(SearchS, ["body"], db=db)
    SearchS._fts_index.create(db)
