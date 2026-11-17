"""A soak of sqler: concurrent writers for minutes or hours, and a checker of every row.

Run it from the repository root::

    uv run --frozen python tests/soak/soak.py --minutes 2 --seed 7 --storage file

The workloads (``--workload`` runs one; by default every one the storage allows, one after
another, sharing ``--minutes`` evenly):

- ``threads``: 8 threads on one ``SQLerDB``;
- ``tasks``: 8 asyncio tasks on one ``AsyncSQLerDB``;
- ``procs2``: 2 processes on one file (one sync, one async), 4 workers each;
- ``procs4``: 4 processes on one file (two sync, two async), 2 workers each.

Every workload runs in its own child processes, on a database of its own under ``--dir``, and
each worker writes a journal beside it (see ``soak_ops.py``). Every ``--checkpoint`` seconds
the writers pause and ``soak_check.check`` reads the database and the journals; at the end it
runs once more. ``--kill`` (process workloads) kills one writer process with SIGKILL at a
random moment 20 times in the run and starts another in its place.

The exit status is 0 when no checker found a violation and no worker met an error a caller
should never see; 1 otherwise, after the first violations and errors, and 1 when ``--kill``
was given and no process was killed (``kills=`` in the summary is the number made); 2 for a
refused invocation (``--kill`` with ``--ops`` among them: kills are placed in time); 130 after
Ctrl-C with a clean check (the workers stop, the checker still runs). Every worker writes each
error to ``errors-<worker>.jsonl`` beside its journal before it goes on, so a killed process's
errors are counted too. The last line is the summary. A failing seed is re-run with the same
flags: the same seed gives every first-started worker the same operations (see ``soak_ops.py`` for
where it does not).

By default the first writes make the tables and one writer promotes ``stage`` of the promoted
class while the others run (the database's own ``ensure_table``). ``--precreate`` makes every
table and promotes ``stage`` before the writers start, so no schema changes while they write.

A ``database is locked`` that reaches a caller in under a second ("locked at once": no wait
for the busy timeout of 5 s was made) is an error and fails the run. One raised after the
wait ("locked after waiting", or ``LockTimeoutError`` from a safe save's retry) is SQLite's
documented answer when the lock stays busy for the whole timeout: SQLite's busy handler polls
with no queue, so on a loaded machine one waiter can lose the lock to newer writers for 5 s
while every transaction holds it for milliseconds. The summary counts those as ``waited=`` and
they do not fail the run; the operation wrote nothing, and the checker still holds every row.

Expected and not counted as errors: a ``StaleVersionError`` of a shared counter when the rebase
rule gives up (the worker reads the row again: "conflicts retried"); the
``ReferentialIntegrityError`` of a ``restrict`` delete of a row its owner's rows refer to; the
exception a worker raises on purpose to roll a transaction back.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import queue
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import soak_check  # noqa: E402
from soak_ops import (  # noqa: E402
    ARCHIVE,
    PROMOTED_LATER,
    SAFE,
    TABLE,
    WAITED_OUT,
    Journal,
    Stats,
    Worker,
    errors_path,
    read_errors,
    run_async_worker,
    run_sync_worker,
)

WORKLOADS = {
    # name: (kinds of the child processes, workers per child, needs a file)
    "threads": (["sync"], 8, False),
    "tasks": (["async"], 8, False),
    "procs2": (["sync", "async"], 4, True),
    "procs4": (["sync", "async", "sync", "async"], 2, True),
}
MARKER = "_soak_run"
COUNTERS = 4
KILLS = 20


# ---------------------------------------------------------------- the child process


class Gate:
    """Where workers wait between two operations: for a pause, a stop, or their turn."""

    def __init__(self, n: int):
        self.n = n
        self.lock = threading.Lock()
        self.pause = False
        self.stop = False
        self.idle = 0
        self.ended = 0

    def wait_turn(self, worker: Any) -> bool:
        if self.pause and not self.stop:
            with self.lock:
                self.idle += 1
            while self.pause and not self.stop:
                time.sleep(0.005)
            with self.lock:
                self.idle -= 1
        return not self.stop

    async def await_turn(self, worker: Any) -> bool:
        if self.pause and not self.stop:
            with self.lock:
                self.idle += 1
            while self.pause and not self.stop:
                await asyncio.sleep(0.005)
            with self.lock:
                self.idle -= 1
        return not self.stop

    def finished(self, worker: Any) -> None:
        with self.lock:
            self.ended += 1

    def all_idle(self) -> bool:
        with self.lock:
            return self.idle + self.ended >= self.n

    def all_ended(self) -> bool:
        with self.lock:
            return self.ended >= self.n


def say(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _memory_uri(name: str) -> str:
    return f"file:{name}?mode=memory&cache=shared"


def setup_sync(models: Any, db: Any, journal: Journal, precreate: bool = False) -> list[int]:
    """The rows and indexes every worker starts from: the counters, both search indexes and
    the index over two fields. Journaled like any write. ``precreate``: every table (the audit
    and archive tables too) is made here, and ``stage`` is promoted here, before any writer
    starts: no schema changes while they write (the checker's own test runs so)."""
    from sqler import F

    models.open_search_indexes(db)
    models.Promo.ensure_index(["prio", "stage"], db=db)
    fx: list = []
    if precreate:
        for name, table in TABLE.items():
            promoted = {"prio": PROMOTED_LATER["prio"]} if name == "promo" else None
            db.ensure_table(table, promoted=promoted, versioned=name in SAFE or name == "counter")
        a = models.Audited(owner="setup", key="setup", n=0).save(db=db)
        aid = a._id
        fx.append(["put", TABLE["audit"], aid, {"owner": "setup", "key": "setup", "n": 0}, 1])
        a.delete(db=db)
        fx.append(["del", TABLE["audit"], aid, 1])
        models.Task.using(db).filter(F("owner") == "setup").copy_to(ARCHIVE)
        db.ensure_table(TABLE["promo"], promoted=PROMOTED_LATER)
        fx.append(["promote", TABLE["promo"], "stage"])
    ids = []
    for i in range(COUNTERS):
        c = models.Counter(name=f"c{i}", count=0).save(db=db)
        ids.append(c._id)
        fx.append(["put", TABLE["counter"], c._id, {"name": f"c{i}", "count": 0}, 0])
    journal.write({"i": 0, "done": "setup", "fx": fx})
    return ids


async def setup_async(models: Any, db: Any, journal: Journal, precreate: bool = False) -> list[int]:
    """Async twin of :func:`setup_sync`."""
    from sqler import F

    await models.open_search_indexes(db)
    await models.Promo.ensure_index(["prio", "stage"], db=db)
    fx: list = []
    if precreate:
        for name, table in TABLE.items():
            promoted = {"prio": PROMOTED_LATER["prio"]} if name == "promo" else None
            await db.ensure_table(table, promoted=promoted, versioned=name in SAFE or name == "counter")
        a = await models.Audited(owner="setup", key="setup", n=0).save(db=db)
        aid = a._id
        fx.append(["put", TABLE["audit"], aid, {"owner": "setup", "key": "setup", "n": 0}, 1])
        await a.delete(db=db)
        fx.append(["del", TABLE["audit"], aid, 1])
        await models.Task.using(db).filter(F("owner") == "setup").copy_to(ARCHIVE)
        await db.ensure_table(TABLE["promo"], promoted=PROMOTED_LATER)
        fx.append(["promote", TABLE["promo"], "stage"])
    ids = []
    for i in range(COUNTERS):
        c = await models.Counter(name=f"c{i}", count=0).save(db=db)
        ids.append(c._id)
        fx.append(["put", TABLE["counter"], c._id, {"name": f"c{i}", "count": 0}, 0])
    journal.write({"i": 0, "done": "setup", "fx": fx})
    return ids


class Child:
    """One child process: its workers on one database object, and the control loop."""

    def __init__(self, spec: dict):
        self.spec = spec
        self.kind = spec["kind"]
        self.gate = Gate(len(spec["wids"]))
        self.stats: list[Stats] = []
        self.workers: list[Worker] = []
        self.journals: list[Journal] = []

    def make_workers(self, models: Any, db: Any, counters: list[int]) -> None:
        spec = self.spec
        for k, wid in enumerate(spec["wids"]):
            journal = Journal(os.path.join(spec["journal_dir"], f"journal-{wid}.jsonl"))
            stats = Stats()
            promote = spec["promote"] and k == 0
            w = Worker(
                wid,
                spec["seed"],
                models.MODELS,
                db,
                journal,
                stats,
                counters,
                promote_at_op=spec["promote_at_op"] if promote else None,
                promote_at_time=spec["promote_at_time"] if promote else None,
                only=spec["only"],
            )
            self.workers.append(w)
            self.stats.append(stats)
            self.journals.append(journal)

    def merged(self) -> dict:
        total = Stats()
        for s in self.stats:
            total.merge(s.as_dict())
        return total.as_dict()

    def pause(self) -> None:
        self.gate.pause = True
        while not self.gate.all_idle():
            time.sleep(0.005)

    def check_here(self) -> dict:
        """The checker in this process (memory storage: no other process can open it)."""
        conn = soak_check.connect(_memory_uri(self.spec["memname"]), uri=True)
        try:
            report = soak_check.check(conn, self.spec["journal_dir"])
        finally:
            conn.close()
        return {
            "ok": report.ok,
            "lines": [v.line() for v in report.violations[:20]],
            "violations": len(report.violations),
            "summary": report.summary(),
            "checked": report.checked,
            "rows": report.rows,
        }

    def control(self) -> None:
        """Read commands from the parent, one per line."""
        done_said = False
        for line in sys.stdin:
            cmd = line.strip()
            if cmd == "pause":
                self.pause()
                say({"ev": "paused"})
            elif cmd == "resume":
                self.gate.pause = False
                say({"ev": "resumed"})
            elif cmd == "stats":
                if self.gate.all_ended() and not done_said:
                    done_said = True
                say({"ev": "stats", "stats": self.merged(), "ended": self.gate.all_ended()})
            elif cmd == "check":
                self.pause()
                say({"ev": "report", "report": self.check_here()})
            elif cmd == "stop":
                break
        self.gate.stop = True

    def run(self) -> None:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent stops us
        if self.kind == "sync":
            self.run_sync()
        else:
            asyncio.run(self.run_async())
        for j in self.journals:
            j.close()
        say({"ev": "stopped", "stats": self.merged()})

    def run_sync(self) -> None:
        import soak_models_sync as models

        from sqler import SQLerDB

        spec = self.spec
        if spec["storage"] == "memory":
            db = SQLerDB.in_memory(name=spec["memname"])
            setup = Journal(os.path.join(spec["journal_dir"], "journal-setup.jsonl"))
            counters = setup_sync(models, db, setup, spec["precreate"])
            setup.close()
        else:
            db = SQLerDB.on_disk(spec["db"])
            models.open_search_indexes(db)
            counters = spec["counters"]
        self.make_workers(models, db, counters)
        threads = [
            threading.Thread(target=run_sync_worker, args=(w, self.gate, spec["ops"]), daemon=True)
            for w in self.workers
        ]
        for t in threads:
            t.start()
        say({"ev": "ready"})
        self.control()
        for t in threads:
            t.join()
        if spec["storage"] == "memory":
            say({"ev": "report", "report": self.check_here()})
        db.close()

    async def run_async(self) -> None:
        import soak_models_async as models

        from sqler import AsyncSQLerDB

        spec = self.spec
        if spec["storage"] == "memory":
            db = AsyncSQLerDB.in_memory(name=spec["memname"])
            await db.connect()
            setup = Journal(os.path.join(spec["journal_dir"], "journal-setup.jsonl"))
            counters = await setup_async(models, db, setup, spec["precreate"])
            setup.close()
        else:
            db = AsyncSQLerDB.on_disk(spec["db"])
            await db.connect()
            await models.open_search_indexes(db)
            counters = spec["counters"]
        self.make_workers(models, db, counters)
        tasks = [
            asyncio.create_task(run_async_worker(w, self.gate, spec["ops"])) for w in self.workers
        ]
        say({"ev": "ready"})
        ctl = threading.Thread(target=self.control, daemon=True)
        ctl.start()
        while ctl.is_alive():
            await asyncio.sleep(0.02)
        await asyncio.gather(*tasks)
        if spec["storage"] == "memory":
            say({"ev": "report", "report": self.check_here()})
        await db.close()


# ---------------------------------------------------------------- the parent


class Refused(Exception):
    """An invocation the harness does not run (exit status 2)."""


@dataclass
class Proc:
    index: int
    kind: str
    gen: int
    wids: list
    popen: Any
    stats: dict = field(default_factory=dict)
    alive: bool = True


class Run:
    """One workload: its folder, its database, its child processes and its checks."""

    def __init__(self, args: Any, name: str, root: str, seconds: Optional[float], out: Any):
        self.args = args
        self.name = name
        self.kinds, self.per_child, _ = WORKLOADS[name]
        self.seconds = seconds
        self.out = out
        self.folder = os.path.join(root, f"soak-{name}")
        self.db_path = os.path.join(self.folder, "soak.db")
        self.memname = f"soak-{name}-{os.getpid()}-{random.randrange(1 << 30)}"
        self.events: queue.Queue = queue.Queue()
        self.held: list = []  # messages read from ``events`` that no wait has taken yet
        self.procs: list[Proc] = []
        self.killed: list[str] = []
        self.kills = 0
        self.dead_stats: list[dict] = []
        self.checks = 0
        self.report: Optional[dict] = None
        self.reports: list[dict] = []
        self.lock_fd: Optional[int] = None
        self.counters: list[int] = []
        self.started = time.monotonic()
        self.rng = random.Random(f"{args.seed}/kill/{name}")

    # -- the folder, refused unless this run made it

    def claim(self) -> None:
        try:
            os.mkdir(self.folder)
        except FileExistsError:
            lock = os.path.join(self.folder, "soak.lock")
            if os.path.exists(lock):
                fd = os.open(lock, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Refused(f"{self.folder} is in use by another soak run") from None
                finally:
                    os.close(fd)
            if os.path.exists(self.db_path) and not _is_soak_db(self.db_path):
                raise Refused(f"{self.db_path} is not a database this harness made")
            raise Refused(f"{self.folder} is left by an earlier run; give a new --dir")
        self.lock_fd = os.open(os.path.join(self.folder, "soak.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def setup(self) -> None:
        if self.args.storage == "memory":
            return  # the child makes its database and its counters
        conn = sqlite3.connect(self.db_path)
        conn.execute(f"CREATE TABLE {MARKER} (seed INTEGER, workload TEXT, pid INTEGER)")
        conn.execute(f"INSERT INTO {MARKER} VALUES (?, ?, ?)", [self.args.seed, self.name, os.getpid()])
        conn.commit()
        conn.close()
        import soak_models_sync as models

        from sqler import SQLerDB

        db = SQLerDB.on_disk(self.db_path)
        setup = Journal(os.path.join(self.folder, "journal-setup.jsonl"))
        self.counters = setup_sync(models, db, setup, self.args.precreate)
        setup.close()
        db.close()

    # -- children

    def spawn(self, index: int, kind: str, gen: int) -> Proc:
        wids = [f"p{index}g{gen}w{k}" for k in range(self.per_child)]
        if self.name == "threads":
            wids = [f"t{k}" for k in range(self.per_child)]
        elif self.name == "tasks":
            wids = [f"a{k}" for k in range(self.per_child)]
        promote_time = self.seconds / 3 if self.seconds else None
        spec = {
            "kind": kind,
            "wids": wids,
            "db": self.db_path,
            "storage": self.args.storage,
            "seed": self.args.seed,
            "journal_dir": self.folder,
            "counters": self.counters,
            "ops": self.args.ops,
            "promote": index == 0 and not self.args.precreate,
            "promote_at_op": (self.args.ops // 3) if self.args.ops else None,
            "promote_at_time": None if self.args.ops else (promote_time if gen == 0 else 0.0),
            "memname": self.memname,
            "only": self.args.only,
            "precreate": self.args.precreate,
        }
        popen = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--child", json.dumps(spec)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        proc = Proc(index, kind, gen, wids, popen)
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        self.wait_for(proc, "ready", 120)
        return proc

    def _read(self, proc: Proc) -> None:
        for line in proc.popen.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.events.put((proc, msg))
        self.events.put((proc, {"ev": "eof"}))

    def wait_for(self, proc: Proc, ev: str, timeout: float) -> dict:
        """The next message ``ev`` of ``proc``. A message that comes first and is not one for
        this wait is held (``self.held``) in the order it came, and read before the queue by the
        next wait: each child's messages are read in the order it sent them, its end ("eof")
        last. (Putting them back on the queue let a child's "eof" pass its last message.)"""
        deadline = time.monotonic() + timeout
        held = self.held
        k = 0
        while True:
            if k < len(held):
                who, msg = held[k]
                if who is not proc or msg["ev"] not in (ev, "eof"):
                    k += 1
                    continue
                del held[k]
            else:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RuntimeError(f"{self.name}: child {proc.index} sent no {ev!r} in {timeout}s")
                try:
                    who, msg = self.events.get(timeout=left)
                except queue.Empty:
                    continue
            if who is proc and msg["ev"] == ev:
                return msg
            if who is proc and msg["ev"] == "eof":
                try:
                    code = proc.popen.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    code = None
                raise RuntimeError(
                    f"{self.name}: child {proc.index} ({proc.kind}, workers {','.join(proc.wids)}) "
                    f"ended before {ev!r}, exit status {code}"
                    + (f" (signal {-code})" if code is not None and code < 0 else "")
                )
            if msg["ev"] == "stats":
                who.stats = msg["stats"]
            elif msg["ev"] == "report":
                self.report = msg["report"]
            else:
                held.append((who, msg))
                k = len(held)

    def send(self, proc: Proc, cmd: str) -> None:
        proc.popen.stdin.write(cmd + "\n")
        proc.popen.stdin.flush()

    def ask_stats(self) -> bool:
        ended = True
        for p in self.procs:
            self.send(p, "stats")
            msg = self.wait_for(p, "stats", 60)
            p.stats = msg["stats"]
            ended = ended and msg["ended"]
        return ended

    def totals(self) -> Stats:
        total = Stats()
        for d in [*self.dead_stats, *(p.stats for p in self.procs)]:
            if d:
                total.merge(d)
        return total

    def progress(self) -> None:
        t = self.totals()
        el = time.monotonic() - self.started
        rate = t.ops / el if el > 0 else 0.0
        print(
            f"soak [{self.name}] {int(el) // 60}:{int(el) % 60:02d} elapsed, {t.ops} ops, "
            f"{rate:.1f} ops/s, {t.conflicts} conflicts retried, errors {t.errors or '{}'}",
            file=self.out,
            flush=True,
        )

    # -- checks

    def checkpoint(self, final: bool = False) -> dict:
        if self.args.storage == "memory":
            p = self.procs[0]
            if final:
                self.send(p, "stop")
                msg = self.wait_for(p, "report", 240)
                self.report = msg["report"]
            else:
                self.send(p, "check")
                msg = self.wait_for(p, "report", 240)
                self.send(p, "resume")
                self.wait_for(p, "resumed", 60)
            rep = msg["report"]
        else:
            if not final:
                for p in self.procs:
                    self.send(p, "pause")
                for p in self.procs:
                    self.wait_for(p, "paused", 240)
            rep = self.check_file()
            if not final:
                for p in self.procs:
                    self.send(p, "resume")
                for p in self.procs:
                    self.wait_for(p, "resumed", 60)
        self.checks += 1
        self.reports.append(rep)
        if not rep["ok"]:
            print(f"soak [{self.name}] check {self.checks}: {rep['violations']} violations", file=self.out, flush=True)
        return rep

    def check_file(self) -> dict:
        from sqler import SQLerDB

        conn = soak_check.connect(self.db_path)
        db = SQLerDB.on_disk(self.db_path)
        try:
            report = soak_check.check(conn, self.folder, killed=self.killed, db=db)
        finally:
            conn.close()
            db.close()
        return {
            "ok": report.ok,
            "lines": [v.line() for v in report.violations[:20]],
            "violations": len(report.violations),
            "summary": report.summary(),
            "checked": report.checked,
            "rows": report.rows,
            "tolerated": report.tolerated,
        }

    # -- kills

    def kill_one(self) -> None:
        alive = [p for p in self.procs if p.alive]
        victim = self.rng.choice(alive)
        victim.popen.send_signal(signal.SIGKILL)
        victim.popen.wait()
        victim.alive = False
        self.kills += 1
        self.dead_stats.append(dead_stats(self.folder, victim.wids, victim.stats))
        self.killed.extend(victim.wids)
        fresh = self.spawn(victim.index, victim.kind, victim.gen + 1)
        self.procs[self.procs.index(victim)] = fresh

    # -- the whole run

    def run(self, kills: int) -> dict:
        self.claim()
        self.setup()
        interrupted = False
        try:
            for i, kind in enumerate(self.kinds):
                self.procs.append(self.spawn(i, kind, 0))
            self.started = time.monotonic()
            deadline = self.started + (self.seconds or 1e9)
            next_progress = self.started + self.args.progress
            next_check = self.started + self.args.checkpoint
            kill_times = sorted(
                self.started + self.rng.uniform(0.05, 0.95) * (self.seconds or 0) for _ in range(kills)
            )
            while True:
                now = time.monotonic()
                if self.args.ops:
                    if self.ask_stats():
                        break
                    time.sleep(0.2)
                elif now >= deadline:
                    break
                if kill_times and now >= kill_times[0]:
                    kill_times.pop(0)
                    self.kill_one()
                if now >= next_progress:
                    self.ask_stats()
                    self.progress()
                    next_progress = now + self.args.progress
                if now >= next_check and not self.args.ops:
                    self.checkpoint()
                    next_check = time.monotonic() + self.args.checkpoint
                time.sleep(0.05)
        except KeyboardInterrupt:
            interrupted = True
            print(f"soak [{self.name}] interrupted: stopping the workers, then the check", file=self.out, flush=True)
        # from here a Ctrl-C (or the second one a terminal sends) waits for the check
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            self.stop_all()
            if self.args.storage == "file":
                rep = self.checkpoint(final=True)
            else:
                rep = self.report or {"ok": False, "violations": 1, "lines": ["VIOLATION no final report"]}
                self.checks += 1
                self.reports.append(rep)
            self.progress()
        finally:
            if self.lock_fd is not None:
                os.close(self.lock_fd)
                self.lock_fd = None
            if not interrupted:
                signal.signal(signal.SIGINT, signal.default_int_handler)
        return {"interrupted": interrupted, "report": rep}

    def stop_all(self) -> None:
        if self.args.storage == "memory":
            p = self.procs[0]
            self.send(p, "stop")
            self.report = self.wait_for(p, "report", 240)["report"]
            p.stats = self.wait_for(p, "stopped", 240)["stats"]
        else:
            for p in self.procs:
                self.send(p, "stop")
            for p in self.procs:
                p.stats = self.wait_for(p, "stopped", 240)["stats"]
        for p in self.procs:
            p.popen.wait(timeout=60)


def dead_stats(folder: str, wids: list, polled: dict) -> dict:
    """The stats of a killed process: its operations as last polled, and its errors as its
    workers put them on disk (every one of them, also those met after the last poll)."""
    stats = Stats()
    stats.merge({**{"ops": 0, "conflicts": 0, "expected": {}, "errors": {}, "first": []}, **polled})
    stats.errors = {}
    stats.first = []
    for wid in wids:
        for item in read_errors(errors_path(os.path.join(folder, f"journal-{wid}.jsonl"))):
            stats.errors[item["error"]] = stats.errors.get(item["error"], 0) + 1
            if len(stats.first) < 20:
                stats.first.append(item)
    return stats.as_dict()


def _is_soak_db(path: str) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [MARKER]).fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False
    return row is not None


def parse(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="tests/soak/soak.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--minutes", type=float, help="total time, shared by the workloads")
    ap.add_argument("--ops", type=int, help="operations per worker in place of a time (the checker test)")
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--storage", choices=("file", "memory"), default="file")
    ap.add_argument("--workload", choices=tuple(WORKLOADS), action="append")
    ap.add_argument("--dir", help="an existing folder for the databases and journals (default: a new temp folder)")
    ap.add_argument(
        "--only",
        type=lambda text: [n for n in text.split(",") if n],
        default=[],
        help="comma-separated operations to run alone (to narrow a finding); default: all",
    )
    ap.add_argument(
        "--precreate",
        action="store_true",
        help="make every table and promote stage before the writers start "
        "(default: the first writes make them, and a writer promotes stage while the others run)",
    )
    ap.add_argument("--kill", action="store_true", help="kill a writer process 20 times in the run")
    ap.add_argument("--checkpoint", type=float, default=30.0, help="seconds between checks while running")
    ap.add_argument("--progress", type=float, default=30.0, help="seconds between progress lines (at most 60)")
    args = ap.parse_args(argv)
    if (args.minutes is None) == (args.ops is None):
        ap.error("give one of --minutes or --ops")
    if args.minutes is not None and not args.minutes > 0:
        ap.error("--minutes must be more than 0")
    if args.ops is not None and args.ops < 1:
        ap.error("--ops must be at least 1")
    if not 0 < args.progress <= 60 or not args.checkpoint > 0:
        ap.error("--progress must be in (0, 60] and --checkpoint more than 0")
    unknown = set(args.only) - set(Worker.WEIGHTS)
    if unknown:
        ap.error(f"--only: no such operation: {', '.join(sorted(unknown))}")
    names = args.workload or [n for n, (_k, _w, f) in WORKLOADS.items() if args.storage == "file" or not f]
    for n in names:
        if WORKLOADS[n][2] and args.storage == "memory":
            ap.error(f"workload {n} needs --storage file: two processes cannot share an in-memory database")
    if args.kill and not any(WORKLOADS[n][2] for n in names):
        ap.error("--kill needs a process workload (procs2 or procs4)")
    if args.kill and args.ops is not None:
        ap.error("--kill needs --minutes: the kills are placed in the run's time, and an --ops run has none to place them in")
    args.names = names
    return args


def main(argv: list[str]) -> int:
    if argv[:1] == ["--child"]:
        Child(json.loads(argv[1])).run()
        return 0
    args = parse(argv)
    # Ctrl-C stops the run and the checker still runs; also when a shell started us with
    # SIGINT ignored (a background job)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    out = sys.stdout
    if args.dir is None:
        root = tempfile.mkdtemp(prefix="sqler-soak-")
    else:
        root = os.path.abspath(args.dir)
        if not os.path.isdir(root):
            print(
                f"soak: refused: {args.dir} is not an existing folder "
                "(the harness makes its own database in a new folder inside it)",
                file=sys.stderr,
            )
            return 2
    seconds = args.minutes * 60 / len(args.names) if args.minutes else None
    proc_names = [n for n in args.names if WORKLOADS[n][2]]
    results = {}
    interrupted = False
    for name in args.names:
        kills = 0
        if args.kill and name in proc_names:
            kills = KILLS // len(proc_names) + (1 if proc_names.index(name) < KILLS % len(proc_names) else 0)
        run = Run(args, name, root, seconds, out)
        try:
            results[name] = (run, run.run(kills))
        except Refused as exc:
            print(f"soak: refused: {exc}", file=sys.stderr)
            return 2
        if results[name][1]["interrupted"]:
            interrupted = True
            break
    return summarize(args, root, results, interrupted, out)


def summarize(args: Any, root: str, results: dict, interrupted: bool, out: Any) -> int:
    total = Stats()
    violations = 0
    tolerated = 0
    rows = 0
    checks = 0
    kills = 0
    failed_lines: list[str] = []
    for name, (run, res) in results.items():
        t = run.totals()
        total.merge(t.as_dict())
        checks += run.checks
        kills += run.kills
        for rep in run.reports:
            violations += rep.get("violations", 0)
            for line in rep.get("lines", []):
                failed_lines.append(f"[{name}] {line}")
        final = res["report"]
        tolerated += final.get("tolerated", 0)
        rows += final.get("rows", 0)
        checked = final.get("checked", {})
        print(f"soak [{name}] final check: {final.get('summary', '')}", file=out)
        empty = [k for k, v in checked.items() if not v]
        if empty and not interrupted:
            print(f"soak [{name}] note: invariants that checked no row: {', '.join(empty)}", file=out)
    waited = {k: v for k, v in total.errors.items() if k in WAITED_OUT}
    failing = {k: v for k, v in total.errors.items() if k not in WAITED_OUT}
    errors = sum(failing.values())
    for item in total.first:
        failed_lines.append(
            f"{'WAITED' if item['error'] in WAITED_OUT else 'ERROR'} {item['error']} worker={item['worker']} seed={item['seed']} op#{item['op_no']} "
            f"{item['op']}: {item['text']}"
        )
    no_kills = bool(getattr(args, "kill", False)) and kills == 0 and not interrupted
    if no_kills:
        failed_lines.insert(0, "NO KILLS: --kill was given and no writer process was killed")
    for line in failed_lines[:40]:
        print(line, file=out)
    ok = violations == 0 and errors == 0 and not no_kills
    verdict = "PASS" if ok else "FAIL"
    if interrupted:
        verdict += " (interrupted)"
    how = f"{args.minutes:g} min" if args.minutes else f"{args.ops} ops/worker"
    print(
        f"SOAK {verdict} seed={args.seed} storage={args.storage} {how} "
        f"workloads={','.join(results)} ops={total.ops} conflicts={total.conflicts} "
        f"errors={errors}{(' ' + json.dumps(failing, sort_keys=True)) if errors else ''} "
        f"waited={sum(waited.values())}{(' ' + json.dumps(waited, sort_keys=True)) if waited else ''} "
        f"expected={sum(total.expected.values())} checks={checks} violations={violations} "
        f"tolerated={tolerated} kills={kills} rows={rows} dir={root}",
        file=out,
        flush=True,
    )
    if not ok:
        return 1
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
