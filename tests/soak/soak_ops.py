"""The operations of one soak worker, written once for the sync and the async classes.

A worker is a generator of steps. Each step is one call of sqler (a zero-argument callable;
on the async side it returns an awaitable) or the entry or exit of a transaction; a runner
(:func:`drive_sync`, :func:`drive_async`) makes the call, sends its result back, and throws
an exception the call raised into the generator at that point. So the choice of operations,
the bookkeeping and the journal are the same code for both kinds.

**Who writes what.** A plain row has one owning worker: only the worker that made it writes
it, so the last write the owner's journal has is the document the row must hold. The rows
every worker writes (the counters) are written only through versioned saves of a safe model
with the default rebase rule, and the checker compares them with the sum of the increments
the journals hold. References point only at rows of the same worker, so a delete policy's
effect on the referring rows is known to the worker that deletes.

**The journal.** Before each operation the worker writes an ``intent`` line (the ids and new
keys the call may touch, and ``fx``, everything the call writes when it goes through); after
the call returns, a ``done`` line with what it was told had been written. Both are flushed to
the file at once. A killed worker leaves at most one ``intent`` with no ``done``: the checker
accepts that call wholly stored or not stored at all, nothing in between. Each error a worker
meets goes to ``errors-<worker>.jsonl`` beside the journal, flushed, before it goes on.

**The seed.** Each worker's generator is ``random.Random(f"{seed}/{worker id}")``, and its
choices depend only on its own rows, so a seed gives the same operations per worker in every
run, with two exceptions: a worker started in place of a killed one has a new id and so other
operations, and in a timed run without ``--precreate`` the promotion comes at a time, not at
an operation number. The interleaving of workers, and so the ids rows get and the conflicts
the counters meet, differ from run to run.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqler import F, ReferentialIntegrityError, StaleVersionError

#: Families of owned rows a worker writes, the logical model name and its table.
PLAIN = ("plain_p", "plain_l", "plain_m")
SAFE = ("safe_p", "safe_l")
#: Above this many live rows of a family the worker deletes rather than creates.
CAP = 60
#: The rebase rule takes a conflict of a counter; when it gives up the worker reads again.
COUNTER_TRIES = 50

TABLE = {
    "plain_p": "soak_plain_p",
    "plain_l": "soak_plain_l",
    "plain_m": "soak_plain_m",
    "safe_p": "soak_safe_p",
    "safe_l": "soak_safe_l",
    "counter": "soak_counter",
    "crew": "soak_crew",
    "job": "soak_job",
    "promo": "soak_promo",
    "audit": "soak_audit",
    "search_s": "soak_search_s",
    "search_m": "soak_search_m",
    "task": "soak_task",
}
ARCHIVE = "soak_task_archive"
#: An operation that raised "locked" sooner than this waited for no lock (the busy timeout is 5 s).
LOCK_WAIT_S = 1.0
PROMOTED_LATER = {"prio": "INTEGER NOT NULL DEFAULT 0", "stage": "TEXT"}


class Rollback(Exception):
    """Raised on purpose inside a transaction that must leave nothing."""


class RestrictNotRefused(Exception):
    """A ``restrict`` delete of a referenced row went through."""


@dataclass
class Enter:
    cm: Any


@dataclass
class Exit:
    cm: Any
    exc: Optional[BaseException]


def drive_sync(gen) -> Any:
    """Run a step generator with plain calls."""
    send: Any = None
    exc: Optional[BaseException] = None
    while True:
        try:
            step = gen.throw(exc) if exc is not None else gen.send(send)
        except StopIteration as stop:
            return stop.value
        exc, send = None, None
        try:
            if isinstance(step, Enter):
                send = step.cm.__enter__()
            elif isinstance(step, Exit):
                e = step.exc
                send = step.cm.__exit__(type(e) if e else None, e, e.__traceback__ if e else None)
            else:
                send = step()
        except BaseException as err:  # noqa: BLE001 - thrown back into the operation
            exc = err


async def drive_async(gen) -> Any:
    """Run a step generator, awaiting each call."""
    send: Any = None
    exc: Optional[BaseException] = None
    while True:
        try:
            step = gen.throw(exc) if exc is not None else gen.send(send)
        except StopIteration as stop:
            return stop.value
        exc, send = None, None
        try:
            if isinstance(step, Enter):
                send = await step.cm.__aenter__()
            elif isinstance(step, Exit):
                e = step.exc
                send = await step.cm.__aexit__(
                    type(e) if e else None, e, e.__traceback__ if e else None
                )
            else:
                send = await step()
        except BaseException as err:  # noqa: BLE001 - thrown back into the operation
            exc = err


class Journal:
    """One worker's journal: a JSON line per intent and per finished operation."""

    def __init__(self, path: str):
        self.path = path
        self._f = open(path, "a", encoding="utf-8")
        self._err: Optional[Any] = None

    def write(self, obj: dict) -> None:
        self._f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self._f.flush()

    def error(self, item: dict) -> None:
        """One error of the worker, as a line of ``errors-<worker>.jsonl`` beside the journal,
        flushed to the file before this returns."""
        if self._err is None:
            self._err = open(errors_path(self.path), "a", encoding="utf-8")
        self._err.write(json.dumps(item, separators=(",", ":")) + "\n")
        self._err.flush()

    def close(self) -> None:
        self._f.close()
        if self._err is not None:
            self._err.close()


def errors_path(journal_path: str) -> str:
    """The errors file beside a journal: ``journal-W.jsonl`` -> ``errors-W.jsonl``."""
    folder, name = os.path.split(journal_path)
    return os.path.join(folder, "errors-" + name[len("journal-") :])


def read_errors(path: str) -> list:
    """The errors a worker put on disk. A last line its process was killed while writing is
    an error too (its text is lost; the run must not pass over it)."""
    out: list = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for raw in f:
            try:
                item = json.loads(raw) if raw.endswith("\n") else None
            except json.JSONDecodeError:
                item = None
            if item is None:
                item = {"worker": "?", "seed": "?", "op_no": "?", "op": "?", "error": "unreadable error line", "text": raw[:200]}
            out.append(item)
    return out


@dataclass
class Stats:
    ops: int = 0
    conflicts: int = 0
    expected: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    first: list = field(default_factory=list)

    def merge(self, other: dict) -> None:
        self.ops += other["ops"]
        self.conflicts += other["conflicts"]
        for name in ("expected", "errors"):
            for k, v in other[name].items():
                getattr(self, name)[k] = getattr(self, name).get(k, 0) + v
        for item in other["first"]:
            if len(self.first) < 20:
                self.first.append(item)

    def as_dict(self) -> dict:
        return {
            "ops": self.ops,
            "conflicts": self.conflicts,
            "expected": dict(self.expected),
            "errors": dict(self.errors),
            "first": list(self.first),
        }


def ref(table: str, _id: int) -> dict:
    """The stored form of a reference."""
    return {"_table": table, "_id": _id}


class Worker:
    """One writer: its own rows, its generator and its journal."""

    def __init__(
        self,
        wid: str,
        seed: int,
        models: dict,
        db: Any,
        journal: Journal,
        stats: Stats,
        counters: list[int],
        promote_at_op: Optional[int] = None,
        promote_at_time: Optional[float] = None,
        only: Any = (),
    ):
        self.wid = wid
        self.base_wid = wid
        self.restarts = 0
        self.seed = seed
        self.rng = random.Random(f"{seed}/{wid}")
        self.m = models
        self.db = db
        self.journal = journal
        self.stats = stats
        self.counters = counters
        self.promote_at_op = promote_at_op
        self.promote_at_time = promote_at_time
        self.promoted = False
        self.only = set(only)
        self.seq = 0
        self.opno = 0
        # logical name -> {id: instance}; the instances are the worker's own rows
        self.live: dict[str, dict[int, Any]] = {name: {} for name in TABLE}
        self.done_tasks: set[int] = set()
        self.started = time.monotonic()
        self.fx: list = []

    # ---- helpers ----

    def key(self) -> str:
        self.seq += 1
        return f"{self.wid}x{self.seq}"

    def doc(self, name: str, inst: Any) -> dict:
        """The document a row of ``name`` holds after ``inst`` was written (stored form)."""
        base = {"owner": inst.owner, "key": inst.key, "n": inst.n}
        if name in PLAIN or name in SAFE:
            base["note"] = inst.note
        elif name == "job":
            base["crew"] = None if inst.crew is None else ref(TABLE["crew"], inst.crew._id)
        elif name == "promo":
            base.update(prio=inst.prio, stage=inst.stage)
        elif name in ("search_s", "search_m"):
            base["body"] = inst.body
        elif name == "task":
            base["done"] = inst.done
        return base

    def plan(self, name: str, inst: Any, audit: int = 0, **changed: Any) -> list:
        """The ``put`` a call will journal for ``inst`` when it goes through, ``changed`` being
        fields the call itself sets; a row not saved yet is named by its key."""
        doc = {**self.doc(name, inst), **changed}
        where = inst._id if inst._id is not None else {"key": inst.key}
        return ["put", TABLE[name], where, doc, audit]

    def put(self, name: str, inst: Any, audit: int = 0) -> None:
        self.fx.append(["put", TABLE[name], inst._id, self.doc(name, inst), audit])

    def pick(self, name: str) -> Optional[tuple[int, Any]]:
        rows = self.live[name]
        if not rows:
            return None
        _id = self.rng.choice(sorted(rows))
        return _id, rows[_id]

    def mine(self, name: str, ids: list[int]):
        insts = [self.live[name][i] for i in ids]
        expr = (F("owner") == self.wid) & F("key").in_list([i.key for i in insts])
        return self.m[name].using(self.db).filter(expr), insts

    def body(self, key: str, n: int) -> str:
        return f"soak {key}v{n} common"

    # ---- the operations: each is a generator of steps ----

    def op_new_plain(self):
        name = self.rng.choice(PLAIN)
        if len(self.live[name]) > CAP:
            return (yield from self.op_delete_plain(name))
        inst = self.m[name](owner=self.wid, key=self.key(), n=self.rng.randrange(1000), note="new")
        self.intent("new_plain", keys=[inst.key], fx=[self.plan(name, inst)])
        yield lambda: inst.save(db=self.db)
        self.live[name][inst._id] = inst
        self.put(name, inst)

    def op_change_plain(self):
        name = self.rng.choice(PLAIN)
        got = self.pick(name)
        if got is None:
            return (yield from self.op_new_plain())
        _id, inst = got
        inst.n += 1
        inst.note = f"c{self.rng.randrange(10**6)}"
        self.intent("change_plain", ids=[[TABLE[name], _id]], fx=[self.plan(name, inst)])
        yield lambda: inst.save(db=self.db)
        self.put(name, inst)

    def op_many_plain(self):
        name = self.rng.choice(PLAIN)
        insts = [
            self.m[name](owner=self.wid, key=self.key(), n=self.rng.randrange(1000), note="many")
            for _ in range(self.rng.randint(2, 5))
        ]
        self.intent("many_plain", keys=[i.key for i in insts], fx=[self.plan(name, i) for i in insts])
        yield lambda: self.m[name].save_many(insts, db=self.db)
        for inst in insts:
            self.live[name][inst._id] = inst
            self.put(name, inst)

    def op_delete_plain(self, name: Optional[str] = None):
        name = name or self.rng.choice(PLAIN)
        got = self.pick(name)
        if got is None:
            return (yield from self.op_new_plain())
        _id, inst = got
        self.intent("delete_plain", ids=[[TABLE[name], _id]], fx=[["del", TABLE[name], _id, 0]])
        yield lambda: inst.delete(db=self.db)
        del self.live[name][_id]
        self.fx.append(["del", TABLE[name], _id, 0])

    def op_qs_update_plain(self):
        name = self.rng.choice(PLAIN)
        ids = sorted(self.live[name])
        if not ids:
            return (yield from self.op_new_plain())
        ids = self.rng.sample(ids, min(3, len(ids)))
        qs, insts = self.mine(name, ids)
        value = 10**6 + self.seq
        self.seq += 1
        self.intent(
            "qs_update_plain", ids=[[TABLE[name], i] for i in ids], fx=[self.plan(name, i, n=value) for i in insts]
        )
        count = yield lambda: qs.update(n=value)
        for inst in insts:
            inst.n = value
            self.put(name, inst)
        self.expect_count("qs_update_plain", count, len(ids))

    def op_qs_delete_plain(self):
        name = self.rng.choice(PLAIN)
        ids = sorted(self.live[name])
        if not ids:
            return (yield from self.op_new_plain())
        ids = self.rng.sample(ids, min(2, len(ids)))
        qs, _ = self.mine(name, ids)
        self.intent("qs_delete_plain", ids=[[TABLE[name], i] for i in ids], fx=[["del", TABLE[name], i, 0] for i in ids])
        count = yield lambda: qs.delete()
        for i in ids:
            del self.live[name][i]
            self.fx.append(["del", TABLE[name], i, 0])
        self.expect_count("qs_delete_plain", count, len(ids))

    def op_new_safe(self):
        name = self.rng.choice(SAFE)
        if len(self.live[name]) > CAP:
            return (yield from self.op_delete_safe(name))
        inst = self.m[name](owner=self.wid, key=self.key(), n=0, note="new")
        self.intent("new_safe", keys=[inst.key], fx=[self.plan(name, inst)])
        yield lambda: inst.save(db=self.db)
        self.live[name][inst._id] = inst
        self.put(name, inst)

    def op_change_safe(self, partial: bool = False):
        name = "safe_p" if partial else self.rng.choice(SAFE)
        got = self.pick(name)
        if got is None:
            return (yield from self.op_new_safe())
        _id, inst = got
        if partial:
            inst.note = f"p{self.rng.randrange(10**6)}"
        else:
            inst.n += 1
        self.intent("partial_safe" if partial else "change_safe", ids=[[TABLE[name], _id]], fx=[self.plan(name, inst)])
        if partial:
            yield lambda: inst.save_partial(db=self.db)
        else:
            yield lambda: inst.save(db=self.db)
        self.put(name, inst)

    def op_partial_safe(self):
        return (yield from self.op_change_safe(partial=True))

    def op_qs_update_safe(self):
        name = self.rng.choice(SAFE)
        ids = sorted(self.live[name])
        if not ids:
            return (yield from self.op_new_safe())
        ids = self.rng.sample(ids, min(3, len(ids)))
        qs, insts = self.mine(name, ids)
        value = 10**6 + self.seq
        self.seq += 1
        self.intent(
            "qs_update_safe", ids=[[TABLE[name], i] for i in ids], fx=[self.plan(name, i, n=value) for i in insts]
        )
        count = yield lambda: qs.update(n=value)
        for inst in insts:
            inst.n = value
            self.put(name, inst)
        self.expect_count("qs_update_safe", count, len(ids))
        # the update moved each version: read them again so the next save is not stale
        for inst in insts:
            yield lambda inst=inst: inst.refresh(db=self.db)

    def op_delete_safe(self, name: Optional[str] = None):
        name = name or self.rng.choice(SAFE)
        got = self.pick(name)
        if got is None:
            return (yield from self.op_new_safe())
        _id, inst = got
        self.intent("delete_safe", ids=[[TABLE[name], _id]], fx=[["del", TABLE[name], _id, 0]])
        yield lambda: inst.delete(db=self.db)
        del self.live[name][_id]
        self.fx.append(["del", TABLE[name], _id, 0])

    def op_counter_inc(self):
        _id = self.rng.choice(self.counters)
        self.intent("counter_inc", ids=[[TABLE["counter"], _id]], fx=[["inc", TABLE["counter"], _id]])
        for _ in range(COUNTER_TRIES):
            inst = yield lambda: self.m["counter"].from_id(_id, db=self.db)
            inst.count += 1
            try:
                yield lambda: inst.save(db=self.db)
            except StaleVersionError:
                self.stats.conflicts += 1
                continue
            self.fx.append(["inc", TABLE["counter"], _id])
            return
        raise RuntimeError(f"counter {_id}: {COUNTER_TRIES} reads in a row met a conflict")

    def op_new_crew(self):
        if len(self.live["crew"]) > CAP // 2:
            return (yield from self.op_delete_crew())
        inst = self.m["crew"](owner=self.wid, key=self.key(), n=0)
        self.intent("new_crew", keys=[inst.key], fx=[self.plan("crew", inst)])
        yield lambda: inst.save(db=self.db)
        self.live["crew"][inst._id] = inst
        self.put("crew", inst)

    def op_new_job(self):
        if len(self.live["job"]) > CAP:
            return (yield from self.op_delete_crew())
        got = self.pick("crew")
        crew = got[1] if got else None
        inst = self.m["job"](owner=self.wid, key=self.key(), n=0, crew=crew)
        self.intent("new_job", keys=[inst.key], fx=[self.plan("job", inst)])
        yield lambda: inst.save(db=self.db)
        self.live["job"][inst._id] = inst
        self.put("job", inst)

    def op_relink_job(self, clear: bool = False):
        got = self.pick("job")
        if got is None:
            return (yield from self.op_new_job())
        _id, inst = got
        crew = None if clear else self.pick("crew")
        inst.crew = None if crew is None else crew[1]
        inst.n += 1
        self.intent("unlink_job" if clear else "relink_job", ids=[[TABLE["job"], _id]], fx=[self.plan("job", inst)])
        yield lambda: inst.save(db=self.db)
        self.put("job", inst)

    def op_unlink_job(self):
        return (yield from self.op_relink_job(clear=True))

    def op_delete_crew(self):
        got = self.pick("crew")
        if got is None:
            return (yield from self.op_new_crew())
        _id, crew = got
        policy = self.rng.choice(("restrict", "set_null", "cascade"))
        jobs = sorted(j for j, inst in self.live["job"].items() if inst.crew is not None and inst.crew._id == _id)
        ids = [[TABLE["crew"], _id]] + [[TABLE["job"], j] for j in jobs]
        if policy == "restrict" and jobs:
            plan: list = []  # refused: the call writes nothing
        else:
            plan = [["del", TABLE["crew"], _id, 0]]
            for j in jobs:
                if policy == "cascade":
                    plan.append(["del", TABLE["job"], j, 0])
                else:
                    plan.append(self.plan("job", self.live["job"][j], crew=None))
        self.intent(f"delete_crew_{policy}", ids=ids, fx=plan)
        if policy == "restrict" and jobs:
            try:
                yield lambda: crew.delete_with_policy(db=self.db, on_delete="restrict")
            except ReferentialIntegrityError:
                self.expected("ReferentialIntegrityError(restrict, referenced)")
                return
            raise RestrictNotRefused(f"crew {_id} referenced by jobs {jobs} was deleted")
        else:
            yield lambda: crew.delete_with_policy(db=self.db, on_delete=policy)
        del self.live["crew"][_id]
        self.fx.append(["del", TABLE["crew"], _id, 0])
        for j in jobs:
            if policy == "cascade":
                del self.live["job"][j]
                self.fx.append(["del", TABLE["job"], j, 0])
            else:
                self.live["job"][j].crew = None
                self.put("job", self.live["job"][j])

    def op_new_promo(self):
        if len(self.live["promo"]) > CAP:
            return (yield from self.op_delete_promo())
        inst = self.m["promo"](
            owner=self.wid,
            key=self.key(),
            prio=self.rng.randrange(10),
            stage=self.rng.choice("abc"),
            n=0,
        )
        self.intent("new_promo", keys=[inst.key], fx=[self.plan("promo", inst)])
        yield lambda: inst.save(db=self.db)
        self.live["promo"][inst._id] = inst
        self.put("promo", inst)

    def op_change_promo(self):
        got = self.pick("promo")
        if got is None:
            return (yield from self.op_new_promo())
        _id, inst = got
        inst.prio = self.rng.randrange(10)
        inst.stage = self.rng.choice("abcd")
        inst.n += 1
        self.intent("change_promo", ids=[[TABLE["promo"], _id]], fx=[self.plan("promo", inst)])
        yield lambda: inst.save(db=self.db)
        self.put("promo", inst)

    def op_delete_promo(self):
        got = self.pick("promo")
        if got is None:
            return (yield from self.op_new_promo())
        _id, inst = got
        self.intent("delete_promo", ids=[[TABLE["promo"], _id]], fx=[["del", TABLE["promo"], _id, 0]])
        yield lambda: inst.delete(db=self.db)
        del self.live["promo"][_id]
        self.fx.append(["del", TABLE["promo"], _id, 0])

    def op_promote(self):
        """Promote ``stage`` through the database's own call while the others write."""
        self.intent("promote", fx=[["promote", TABLE["promo"], "stage"]])
        yield lambda: self.db.ensure_table(TABLE["promo"], promoted=PROMOTED_LATER)
        self.fx.append(["promote", TABLE["promo"], "stage"])

    def op_new_audit(self):
        if len(self.live["audit"]) > CAP:
            return (yield from self.op_delete_audit())
        inst = self.m["audit"](owner=self.wid, key=self.key(), n=0)
        self.intent("new_audit", keys=[inst.key], fx=[self.plan("audit", inst, audit=1)])
        yield lambda: inst.save(db=self.db)
        self.live["audit"][inst._id] = inst
        self.put("audit", inst, audit=1)

    def op_change_audit(self):
        got = self.pick("audit")
        if got is None:
            return (yield from self.op_new_audit())
        _id, inst = got
        inst.n += 1  # every audited write changes the document: one audit row each
        self.intent("change_audit", ids=[[TABLE["audit"], _id]], fx=[self.plan("audit", inst, audit=1)])
        yield lambda: inst.save(db=self.db)
        self.put("audit", inst, audit=1)

    def op_qs_update_audit(self):
        ids = sorted(self.live["audit"])
        if not ids:
            return (yield from self.op_new_audit())
        ids = self.rng.sample(ids, min(3, len(ids)))
        qs, insts = self.mine("audit", ids)
        value = 10**6 + self.seq
        self.seq += 1
        # an update writes an audit row only where the stored document changed
        self.intent(
            "qs_update_audit",
            ids=[[TABLE["audit"], i] for i in ids],
            fx=[self.plan("audit", i, audit=1 if i.n != value else 0, n=value) for i in insts],
        )
        count = yield lambda: qs.update(n=value)
        for inst in insts:
            # an update writes an audit row only where the stored document changed
            changed = 1 if inst.n != value else 0
            inst.n = value
            self.put("audit", inst, audit=changed)
        self.expect_count("qs_update_audit", count, len(ids))

    def op_delete_audit(self):
        got = self.pick("audit")
        if got is None:
            return (yield from self.op_new_audit())
        _id, inst = got
        self.intent("delete_audit", ids=[[TABLE["audit"], _id]], fx=[["del", TABLE["audit"], _id, 1]])
        yield lambda: inst.delete(db=self.db)
        del self.live["audit"][_id]
        self.fx.append(["del", TABLE["audit"], _id, 1])

    def op_new_search(self):
        name = self.rng.choice(("search_s", "search_m"))
        if len(self.live[name]) > CAP:
            return (yield from self.op_delete_search(name))
        key = self.key()
        inst = self.m[name](owner=self.wid, key=key, n=0, body=self.body(key, 0))
        self.intent("new_search", keys=[key], fx=[self.plan(name, inst)])
        yield lambda: inst.save(db=self.db)
        self.live[name][inst._id] = inst
        self.put(name, inst)

    def op_change_search(self):
        name = self.rng.choice(("search_s", "search_m"))
        got = self.pick(name)
        if got is None:
            return (yield from self.op_new_search())
        _id, inst = got
        inst.n += 1
        inst.body = self.body(inst.key, inst.n)
        self.intent("change_search", ids=[[TABLE[name], _id]], fx=[self.plan(name, inst)])
        yield lambda: inst.save(db=self.db)
        self.put(name, inst)

    def op_delete_search(self, name: Optional[str] = None):
        name = name or self.rng.choice(("search_s", "search_m"))
        got = self.pick(name)
        if got is None:
            return (yield from self.op_new_search())
        _id, inst = got
        self.intent("delete_search", ids=[[TABLE[name], _id]], fx=[["del", TABLE[name], _id, 0]])
        yield lambda: inst.delete(db=self.db)
        del self.live[name][_id]
        self.fx.append(["del", TABLE[name], _id, 0])

    def op_many_search_s(self):
        """``save_many`` of the content_sync class (its triggers index the rows)."""
        insts = []
        for _ in range(self.rng.randint(2, 4)):
            key = self.key()
            insts.append(self.m["search_s"](owner=self.wid, key=key, n=0, body=self.body(key, 0)))
        self.intent("many_search_s", keys=[i.key for i in insts], fx=[self.plan("search_s", i) for i in insts])
        yield lambda: self.m["search_s"].save_many(insts, db=self.db)
        for inst in insts:
            self.live["search_s"][inst._id] = inst
            self.put("search_s", inst)

    def op_qs_delete_search_s(self):
        ids = sorted(self.live["search_s"])
        if not ids:
            return (yield from self.op_new_search())
        ids = self.rng.sample(ids, min(2, len(ids)))
        qs, _ = self.mine("search_s", ids)
        self.intent(
            "qs_delete_search_s", ids=[[TABLE["search_s"], i] for i in ids], fx=[["del", TABLE["search_s"], i, 0] for i in ids]
        )
        count = yield lambda: qs.delete()
        for i in ids:
            del self.live["search_s"][i]
            self.fx.append(["del", TABLE["search_s"], i, 0])
        self.expect_count("qs_delete_search_s", count, len(ids))

    def op_txn_commit(self):
        a = self.m["plain_p"](owner=self.wid, key=self.key(), n=1, note="txn")
        b = self.m["audit"](owner=self.wid, key=self.key(), n=1)
        self.intent("txn_commit", keys=[a.key, b.key], fx=[self.plan("plain_p", a), self.plan("audit", b, audit=1)])
        cm = self.db.transaction()
        yield Enter(cm)
        try:
            yield lambda: a.save(db=self.db)
            yield lambda: b.save(db=self.db)
        except BaseException as exc:
            yield Exit(cm, exc)
            raise
        yield Exit(cm, None)
        self.live["plain_p"][a._id] = a
        self.live["audit"][b._id] = b
        self.put("plain_p", a)
        self.put("audit", b, audit=1)

    def op_txn_rollback(self):
        a = self.m["plain_p"](owner=self.wid, key=self.key(), n=2, note="ghost")
        b = self.m["audit"](owner=self.wid, key=self.key(), n=2)
        ck = self.key()
        c = self.m["search_s"](owner=self.wid, key=ck, n=0, body=self.body(ck, 0))
        ghosts = [["ghost", TABLE["plain_p"], a.key], ["ghost", TABLE["audit"], b.key], ["ghost", TABLE["search_s"], ck, f"{ck}v0"]]
        self.intent("txn_rollback", keys=[a.key, b.key, c.key], fx=ghosts)
        cm = self.db.transaction()
        yield Enter(cm)
        try:
            yield lambda: a.save(db=self.db)
            yield lambda: b.save(db=self.db)
            yield lambda: c.save(db=self.db)
            raise Rollback()
        except Rollback as exc:
            yield Exit(cm, exc)
        except BaseException as exc:
            yield Exit(cm, exc)
            raise
        self.fx.extend(ghosts)
        self.expected("Rollback(deliberate)")

    def op_new_task(self):
        if len(self.live["task"]) > CAP:
            return (yield from self.op_archive())
        inst = self.m["task"](owner=self.wid, key=self.key(), done=False, n=0)
        self.intent("new_task", keys=[inst.key], fx=[self.plan("task", inst)])
        yield lambda: inst.save(db=self.db)
        self.live["task"][inst._id] = inst
        self.put("task", inst)

    def op_done_task(self):
        open_ = sorted(i for i in self.live["task"] if i not in self.done_tasks)
        if not open_:
            return (yield from self.op_new_task())
        _id = self.rng.choice(open_)
        inst = self.live["task"][_id]
        inst.done = True
        inst.n += 1
        self.intent("done_task", ids=[[TABLE["task"], _id]], fx=[self.plan("task", inst)])
        yield lambda: inst.save(db=self.db)
        self.done_tasks.add(_id)
        self.put("task", inst)

    def op_archive(self):
        """``copy_to()`` then ``delete()`` of this worker's finished tasks, in one transaction."""
        ids = sorted(self.done_tasks)
        if not ids:
            return (yield from self.op_done_task())
        qs = self.m["task"].using(self.db).filter((F("owner") == self.wid) & (F("done") == True))  # noqa: E712
        self.intent(
            "archive",
            ids=[[TABLE["task"], i] for i in ids] + [[ARCHIVE, i] for i in ids],
            fx=[["arch", TABLE["task"], ARCHIVE, i, self.doc("task", self.live["task"][i])] for i in ids],
        )
        cm = self.db.transaction()
        yield Enter(cm)
        try:
            copied = yield lambda: qs.copy_to(ARCHIVE)
            deleted = yield lambda: qs.delete()
        except BaseException as exc:
            yield Exit(cm, exc)
            raise
        yield Exit(cm, None)
        for i in ids:
            inst = self.live["task"].pop(i)
            self.fx.append(["arch", TABLE["task"], ARCHIVE, i, self.doc("task", inst)])
        self.done_tasks.clear()
        self.expect_count("archive.copy_to", copied, len(ids))
        self.expect_count("archive.delete", deleted, len(ids))

    WEIGHTS = {
        "new_plain": 8, "change_plain": 8, "many_plain": 3, "delete_plain": 3,
        "qs_update_plain": 2, "qs_delete_plain": 1,
        "new_safe": 4, "change_safe": 6, "partial_safe": 3, "qs_update_safe": 1, "delete_safe": 2,
        "counter_inc": 6,
        "new_crew": 2, "new_job": 3, "relink_job": 3, "unlink_job": 2, "delete_crew": 3,
        "new_promo": 3, "change_promo": 4, "delete_promo": 1,
        "new_audit": 3, "change_audit": 4, "qs_update_audit": 1, "delete_audit": 2,
        "new_search": 4, "change_search": 4, "delete_search": 2, "many_search_s": 1,
        "qs_delete_search_s": 1,
        "txn_commit": 2, "txn_rollback": 2,
        "new_task": 3, "done_task": 3, "archive": 1,
    }  # fmt: skip

    # ---- bookkeeping ----

    def intent(self, op: str, ids: Any = (), keys: Any = (), fx: Any = ()) -> None:
        """Journal, before the call, the rows it may touch and ``fx``: everything it writes
        when it goes through (a new row by its key, its id being unknown yet). The checker holds
        a killed worker's unfinished call to all of ``fx`` or none of it."""
        self.journal.write({"i": self.opno, "intent": op, "ids": list(ids), "keys": list(keys), "fx": list(fx)})

    def expected(self, kind: str) -> None:
        self.stats.expected[kind] = self.stats.expected.get(kind, 0) + 1

    def error(self, kind: str, text: str, op: str = "") -> None:
        """Count an error, and put it on disk beside the journal before the worker goes on: a
        process killed before the parent asks for its stats still leaves its errors."""
        item = {"worker": self.wid, "seed": self.seed, "op_no": self.opno, "op": op, "error": kind, "text": text}
        self.journal.error(item)
        self.stats.errors[kind] = self.stats.errors.get(kind, 0) + 1
        if len(self.stats.first) < 20:
            self.stats.first.append(item)

    def expect_count(self, what: str, got: Any, want: int) -> None:
        if got != want:
            self.error("wrong_count", f"{what} returned {got!r}, the rows it was given are {want}")

    def next_op(self) -> tuple[str, Callable]:
        """The next operation, chosen by this worker's generator."""
        if not self.promoted and (
            (self.promote_at_op is not None and self.opno >= self.promote_at_op)
            or (
                self.promote_at_time is not None
                and time.monotonic() - self.started >= self.promote_at_time
            )
        ):
            self.promoted = True
            return "promote", self.op_promote
        names = [n for n in self.WEIGHTS if not self.only or n in self.only]
        name = self.rng.choices(names, weights=[self.WEIGHTS[n] for n in names])[0]
        return name, getattr(self, f"op_{name}")

    def begin(self) -> tuple[str, Any]:
        self.opno += 1
        self.fx = []
        name, op = self.next_op()
        return name, op()

    def finish(self, name: str, exc: Optional[BaseException], seconds: float = 0.0) -> None:
        """Journal what the operation was told had been written, and count the error.

        The error is on disk before the done line: a worker killed between the two writes
        keeps its error, and its call stays in flight for the checker."""
        if exc is not None:
            kind = type(exc).__name__
            if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
                # a wait as long as the busy timeout is SQLite's answer under load; a lock
                # met at once means no wait was made (the operation's time bounds the wait)
                kind += " locked at once" if seconds < LOCK_WAIT_S else " locked after waiting"
            text = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            code = getattr(exc, "sqlite_errorname", None)
            if code:
                text += f" [{code}]"
            frames = [
                f"{os.path.basename(f.filename)}:{f.lineno} {f.name}"
                for f in traceback.extract_tb(exc.__traceback__)
                if f"{os.sep}sqler{os.sep}" in f.filename
            ][-4:]
            where = f" after {seconds:.3f}s in the operation; at {' < '.join(reversed(frames))}"
            self.error(kind, text[:400] + where, op=name)
        self.journal.write({"i": self.opno, "done": name, "fx": self.fx})
        self.stats.ops += 1
        if exc is not None:
            self.resync_after_error()

    def resync_after_error(self) -> None:
        """An unexpected error leaves this worker unsure of its rows: it stops writing them.

        Its rows stay in the journal as they last were; the worker starts a fresh set of
        rows under a new owner name so later operations do not build on an unknown state.
        """
        self.restarts += 1
        self.wid = f"{self.base_wid}r{self.restarts}"
        self.live = {name: {} for name in TABLE}
        self.done_tasks = set()


def run_sync_worker(worker: Worker, gate: Any, ops_limit: Optional[int]) -> None:
    """Run operations until ``gate`` says stop (or ``ops_limit`` operations ran)."""
    while gate.wait_turn(worker):
        if ops_limit is not None and worker.opno >= ops_limit:
            gate.finished(worker)
            return
        name, gen = worker.begin()
        exc: Optional[BaseException] = None
        t0 = time.monotonic()
        try:
            drive_sync(gen)
        except Exception as err:  # noqa: BLE001 - counted, and listed in the summary
            exc = err
        worker.finish(name, exc, time.monotonic() - t0)


async def run_async_worker(worker: Worker, gate: Any, ops_limit: Optional[int]) -> None:
    """Async twin of :func:`run_sync_worker`."""
    while await gate.await_turn(worker):
        if ops_limit is not None and worker.opno >= ops_limit:
            gate.finished(worker)
            return
        name, gen = worker.begin()
        exc: Optional[BaseException] = None
        t0 = time.monotonic()
        try:
            await drive_async(gen)
        except Exception as err:  # noqa: BLE001 - counted, and listed in the summary
            exc = err
        worker.finish(name, exc, time.monotonic() - t0)
