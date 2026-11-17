"""The soak's checker sees each kind of damage (the soak itself is ``tests/soak/soak.py``).

One short soak runs every workload for 60 operations a worker on seed 9, with every table made
and ``stage`` promoted before the writers start (``--precreate``: no schema change while they
write). The same seed gives every worker the same operations, so the number of rows each
invariant checks is the same in every run: the tests assert those numbers. Then, for each
invariant, a copy of the threads workload's database is damaged in one way the invariant names,
and the checker must fail, naming the invariant and the row.
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

SOAK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soak")
sys.path.insert(0, SOAK_DIR)

import soak_check  # noqa: E402

SEED = 9
OPS = 60
WORKLOADS = ("threads", "tasks", "procs2", "procs4")


@pytest.fixture(scope="module")
def soaked(tmp_path_factory):
    """The folder of one short soak of every workload, and the soak's own output."""
    root = tmp_path_factory.mktemp("soak")
    cmd = [sys.executable, os.path.join(SOAK_DIR, "soak.py"), "--ops", str(OPS), "--seed", str(SEED)]
    cmd += ["--precreate", "--storage", "file", "--dir", str(root)]
    for w in WORKLOADS:
        cmd += ["--workload", w]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    return root, done


def _check(folder, db_path=None, killed=()):
    conn = soak_check.connect(db_path or os.path.join(folder, "soak.db"))
    try:
        return soak_check.check(conn, str(folder), killed=killed)
    finally:
        conn.close()


def test_the_soak_passes_and_says_so_in_its_last_line(soaked):
    _root, done = soaked
    last = done.stdout.strip().splitlines()[-1]
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert last.startswith(f"SOAK PASS seed={SEED} storage=file {OPS} ops/worker workloads=threads,tasks,procs2,procs4 ops=1920 ")
    assert " errors=0 " in last and " violations=0 " in last


# What each invariant checked in the run of seed 9 (60 operations a worker, 8 workers a
# workload), in the order of ``soak_check.INVARIANTS``. Counted from that run; any change to
# the operations or their weights changes them.
CHECKED = {
    "threads": dict(zip(soak_check.INVARIANTS, [253, 253, 58, 253, 33, 4, 30, 6, 39, 38, 1, 26, 14, 2, 56, 2, 1, 1])),
    "tasks": dict(zip(soak_check.INVARIANTS, [260, 260, 69, 260, 29, 4, 37, 3, 31, 30, 1, 15, 16, 2, 48, 3, 1, 1])),
    "procs2": dict(zip(soak_check.INVARIANTS, [297, 297, 48, 297, 37, 4, 35, 5, 45, 44, 1, 35, 12, 2, 60, 2, 1, 1])),
    "procs4": dict(zip(soak_check.INVARIANTS, [269, 269, 53, 269, 26, 4, 33, 6, 51, 50, 1, 33, 11, 2, 56, 1, 1, 1])),
}


@pytest.mark.parametrize("workload", WORKLOADS)
def test_the_checker_passes_and_checks_rows_of_every_invariant(soaked, workload):
    root, _ = soaked
    report = _check(root / f"soak-{workload}")
    assert [v.line() for v in report.violations] == []
    assert report.tolerated == 0
    assert report.checked == CHECKED[workload]


@pytest.fixture
def damaged(soaked, tmp_path):
    """A copy of the threads workload's database, its journals' expectations, and a check."""
    root, _ = soaked
    folder = root / "soak-threads"
    copy = tmp_path / "copy.db"
    src = sqlite3.connect(folder / "soak.db")
    dst = sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    exp = soak_check.read_journals(str(folder))

    def check():
        dst.commit()
        return _check(folder, str(copy))

    yield dst, exp, check
    dst.close()


def _live(exp, table):
    return sorted(exp.live[table])[0]


def _gone(exp, table):
    return sorted(exp.deleted[table] - set(exp.live[table]))[0]


def _ghost_key(exp, table):
    return [key for t, key, _ in exp.ghosts if t == table][0]


def _set(conn, table, _id, path, value):
    conn.execute(f"UPDATE {table} SET data = json_set(data, '{path}', ?) WHERE _id = ?", [value, _id])


def d_row_missing(conn, exp):
    i = _live(exp, "soak_plain_p")
    conn.execute("DELETE FROM soak_plain_p WHERE _id = ?", [i])
    return "row_missing", "soak_plain_p", i


def d_row_doc(conn, exp):
    i = _live(exp, "soak_plain_l")
    _set(conn, "soak_plain_l", i, "$.n", -7)
    return "row_doc", "soak_plain_l", i


def d_row_deleted_present(conn, exp):
    i = _gone(exp, "soak_plain_m")
    conn.execute("INSERT INTO soak_plain_m (_id, data) VALUES (?, ?)", [i, '{"owner":"t0","key":"back","n":0,"note":""}'])
    return "row_deleted_present", "soak_plain_m", i


def d_row_unknown(conn, exp):
    cur = conn.execute("INSERT INTO soak_plain_p (data) VALUES (?)", ['{"owner":"x","key":"stranger","n":0,"note":""}'])
    return "row_unknown", "soak_plain_p", cur.lastrowid


def d_version(conn, exp):
    i = _live(exp, "soak_safe_p")
    conn.execute("UPDATE soak_safe_p SET _version = _version - 1 WHERE _id = ?", [i])
    return "version", "soak_safe_p", i


def d_counter(conn, exp):
    i = sorted(exp.live["soak_counter"])[0]
    conn.execute(
        "UPDATE soak_counter SET data = json_set(data, '$.count', json_extract(data, '$.count') - 1) WHERE _id = ?",
        [i],
    )
    return "counter", "soak_counter", i


def d_audit_missing(conn, exp):
    i = _live(exp, "soak_audit")
    conn.execute("DELETE FROM soak_audit_audit WHERE _id = (SELECT max(_id) FROM soak_audit_audit WHERE record_id = ?)", [i])
    return "audit", "soak_audit", i


def d_audit_orphan(conn, exp):
    cur = conn.execute(
        "INSERT INTO soak_audit_audit (record_id, action, user, timestamp, changes, snapshot) "
        "VALUES (999999, 'update', NULL, 'now', NULL, '{\"key\": \"nobody\"}')"
    )
    return "audit", "soak_audit_audit", cur.lastrowid


def d_reference(conn, exp):
    i = [j for j, doc in sorted(exp.live["soak_job"].items()) if doc["crew"] is not None][0]
    _set(conn, "soak_job", i, "$.crew._id", 999999)
    return "reference", "soak_job", i


def d_promoted_column(conn, exp):
    i = _live(exp, "soak_promo")
    conn.execute("UPDATE soak_promo SET prio = prio + 1 WHERE _id = ?", [i])
    return "promoted_column", "soak_promo", i


def d_promoted_in_data(conn, exp):
    i = _live(exp, "soak_promo")
    conn.execute("UPDATE soak_promo SET data = json_set(data, '$.stage', stage) WHERE _id = ?", [i])
    return "promoted_in_data", "soak_promo", i


def d_index(conn, exp):
    (name,) = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'soak_promo'").fetchone()
    conn.execute(f"DROP INDEX {name}")
    return "index", "soak_promo", None


def d_index_on_the_path_of_a_promoted_field(conn, exp):
    """``stage`` is a column (promoted before the writers start); an index left on its JSON
    path can never serve a query by it."""
    (name,) = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'soak_promo'").fetchone()
    conn.execute(f"DROP INDEX {name}")
    conn.execute(f"CREATE INDEX {name} ON soak_promo (prio, json_extract(data, '$.stage'))")
    return "index", "soak_promo", None


def d_search_missing(conn, exp):
    i = _live(exp, "soak_search_s")
    conn.execute("DELETE FROM soak_search_s_fts WHERE rowid = ?", [i])
    return "search_missing", "soak_search_s_fts", i


def d_search_deleted(conn, exp):
    i = _gone(exp, "soak_search_m")
    conn.execute("INSERT INTO soak_search_m_fts (rowid, body) VALUES (?, 'soak gonev0 common')", [i])
    return "search_deleted", "soak_search_m_fts", i


def d_search_integrity(conn, exp):
    conn.execute("DELETE FROM soak_search_s_fts_docsize WHERE id = ?", [_live(exp, "soak_search_s")])
    return "search_integrity", "soak_search_s_fts", None


def d_rollback_left(conn, exp):
    key = _ghost_key(exp, "soak_plain_p")
    cur = conn.execute("INSERT INTO soak_plain_p (data) VALUES (json_object('owner', 't0', 'key', ?, 'n', 2, 'note', 'ghost'))", [key])
    return "rollback_left", "soak_plain_p", cur.lastrowid


def d_archive_in_both(conn, exp):
    i = sorted(exp.archived)[0]
    conn.execute("INSERT INTO soak_task (_id, data) SELECT _id, data FROM soak_task_archive WHERE _id = ?", [i])
    return "archive", "soak_task", i


def d_archive_in_neither(conn, exp):
    i = sorted(exp.archived)[0]
    conn.execute("DELETE FROM soak_task_archive WHERE _id = ?", [i])
    return "archive", "soak_task_archive", i


def d_integrity_check(conn, exp):
    conn.commit()
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute(
        "UPDATE sqlite_master SET sql = replace(sql, '(prio, stage)', '(stage, prio)') "
        "WHERE type = 'index' AND tbl_name = 'soak_promo'"
    )
    return "integrity_check", "*", None


def d_foreign_key_check(conn, exp):
    conn.execute("CREATE TABLE fk_parent (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE fk_child (pid INTEGER REFERENCES fk_parent(id))")
    cur = conn.execute("INSERT INTO fk_child (pid) VALUES (5)")
    return "foreign_key_check", "fk_child", cur.lastrowid


DAMAGES = [
    ("row_missing", d_row_missing),
    ("row_doc", d_row_doc),
    ("row_deleted_present", d_row_deleted_present),
    ("row_unknown", d_row_unknown),
    ("version", d_version),
    ("counter", d_counter),
    ("audit", d_audit_missing),
    ("audit", d_audit_orphan),
    ("reference", d_reference),
    ("promoted_column", d_promoted_column),
    ("promoted_in_data", d_promoted_in_data),
    ("index", d_index),
    ("index", d_index_on_the_path_of_a_promoted_field),
    ("search_missing", d_search_missing),
    ("search_deleted", d_search_deleted),
    ("search_integrity", d_search_integrity),
    ("rollback_left", d_rollback_left),
    ("archive", d_archive_in_both),
    ("archive", d_archive_in_neither),
    ("integrity_check", d_integrity_check),
    ("foreign_key_check", d_foreign_key_check),
]


def test_every_invariant_has_a_damage():
    assert {invariant for invariant, _ in DAMAGES} == set(soak_check.INVARIANTS)


@pytest.mark.parametrize("named, damage", DAMAGES, ids=[d.__name__[2:] for _, d in DAMAGES])
def test_the_checker_fails_on_damage_naming_the_invariant_and_row(damaged, named, damage):
    conn, exp, check = damaged
    assert check().ok  # the copy is clean before the damage
    invariant, table, row = damage(conn, exp)
    assert invariant == named
    report = check()
    assert report.ok is False
    assert (invariant, table, row) in report.names(), [v.line() for v in report.violations]


def test_a_killed_workers_call_in_flight_is_tolerated_and_only_for_that_worker(soaked, tmp_path):
    """A row a killed worker's last intent names, with no ``done`` line, is not a violation
    for that worker; for a worker that was not killed it is a row no journal knows."""
    root, _ = soaked
    folder = tmp_path / "soak-threads"
    shutil.copytree(root / "soak-threads", folder)
    conn = sqlite3.connect(folder / "soak.db")
    cur = conn.execute(
        "INSERT INTO soak_plain_p (data) VALUES ('{\"owner\":\"t0\",\"key\":\"t0x99999\",\"n\":1,\"note\":\"new\"}')"
    )
    conn.commit()
    conn.close()
    plan = ["put", "soak_plain_p", {"key": "t0x99999"}, {"owner": "t0", "key": "t0x99999", "n": 1, "note": "new"}, 0]
    _intent(folder, "t0", "new_plain", keys=["t0x99999"], fx=[plan])
    killed = _check(folder, killed=["t0"])
    assert [v.line() for v in killed.violations] == []
    assert killed.tolerated == 1
    alive = _check(folder)
    assert alive.names() == {("row_unknown", "soak_plain_p", cur.lastrowid)}


def test_a_killed_workers_call_in_flight_covers_the_index_entries_of_the_rows_it_names(soaked, tmp_path):
    """A killed worker's last intent deleted a row that has an index entry, with no ``done``
    line: the row and its entry are gone together, which is tolerated for that worker."""
    root, _ = soaked
    folder = tmp_path / "soak-threads"
    shutil.copytree(root / "soak-threads", folder)
    exp = soak_check.read_journals(str(folder))
    owner, i = sorted((doc["owner"], j) for j, doc in exp.live["soak_search_s"].items())[0]
    conn = sqlite3.connect(folder / "soak.db")
    conn.execute("DELETE FROM soak_search_s WHERE _id = ?", [i])  # the trigger takes the entry
    conn.commit()
    conn.close()
    _intent(folder, owner, "delete_search", ids=[["soak_search_s", i]], fx=[["del", "soak_search_s", i, 0]])
    killed = _check(folder, killed=[owner])
    assert [v.line() for v in killed.violations] == []
    assert killed.tolerated == 1  # one call, wholly stored: the row and its index entry
    alive = _check(folder)
    assert alive.names() == {("row_missing", "soak_search_s", i), ("search_missing", "soak_search_s_fts", i)}


def _intent(folder, worker, op, ids=(), keys=(), fx=()):
    """Append the ``intent`` line of an unfinished call to a worker's journal."""
    line = {"i": 99999, "intent": op, "ids": list(ids), "keys": list(keys), "fx": list(fx)}
    with open(folder / f"journal-{worker}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


@pytest.fixture
def copied(soaked, tmp_path):
    """A copy of the threads workload's folder (database and journals), and its journals'
    expectations."""
    root, _ = soaked
    folder = tmp_path / "soak-threads"
    shutil.copytree(root / "soak-threads", folder)
    return folder, soak_check.read_journals(str(folder))


def _sqler_db(folder):
    import soak_models_sync as models

    from sqler import SQLerDB

    db = SQLerDB.on_disk(str(folder / "soak.db"))
    models.open_search_indexes(db)
    return models, db


# Each kind of call below is killed in flight in one of three states: "none" (nothing of it
# stored), "all" (all of it stored, as the package stores it) or "half" (the state a call that
# is not one unit could leave). It returns the worker, its intent and, for "half", the exact
# violations the checker must name: what breaks if none were stored, and what if all were.


def k_txn_commit(folder, exp, state):
    """A transaction saving a plain row and an audited row; half: the audited row is missing."""
    a = {"owner": "t0", "key": "t0x90001", "n": 1, "note": "txn"}
    b = {"owner": "t0", "key": "t0x90002", "n": 1}
    fx = [["put", "soak_plain_p", {"key": a["key"]}, a, 0], ["put", "soak_audit", {"key": b["key"]}, b, 1]]
    _intent(folder, "t0", "txn_commit", keys=[a["key"], b["key"]], fx=fx)
    if state == "none":
        return "t0", set()
    models, db = _sqler_db(folder)
    with db.transaction():
        pa = models.PlainP(**a).save(db=db)
        if state == "all":
            models.Audited(**b).save(db=db)
    db.close()
    return "t0", {("row_unknown", "soak_plain_p", pa._id), ("row_missing", "soak_audit", None)}


def k_archive(folder, exp, state):
    """``copy_to()`` then ``delete()`` of a task; half: the task is in the archive and still
    in its table."""
    owner, i = sorted((doc["owner"], j) for j, doc in exp.live["soak_task"].items())[0]
    doc = exp.live["soak_task"][i]
    _intent(folder, owner, "archive", ids=[["soak_task", i], ["soak_task_archive", i]], fx=[["arch", "soak_task", "soak_task_archive", i, doc]])
    conn = sqlite3.connect(folder / "soak.db")
    if state != "none":
        conn.execute("INSERT INTO soak_task_archive (_id, data) SELECT _id, data FROM soak_task WHERE _id = ?", [i])
    if state == "all":
        conn.execute("DELETE FROM soak_task WHERE _id = ?", [i])
    conn.commit()
    conn.close()
    return owner, {("row_unknown", "soak_task_archive", i), ("archive", "soak_task", i)}


def k_new_audit(folder, exp, state):
    """A save of a new audited row; half: the row is saved, its audit row is not."""
    b = {"owner": "t0", "key": "t0x90003", "n": 0}
    _intent(folder, "t0", "new_audit", keys=[b["key"]], fx=[["put", "soak_audit", {"key": b["key"]}, b, 1]])
    if state == "none":
        return "t0", set()
    models, db = _sqler_db(folder)
    row = models.Audited(**b).save(db=db)
    db.close()
    if state == "half":
        conn = sqlite3.connect(folder / "soak.db")
        conn.execute("DELETE FROM soak_audit_audit WHERE record_id = ?", [row._id])
        conn.commit()
        conn.close()
    return "t0", {("row_unknown", "soak_audit", row._id), ("audit", "soak_audit", row._id)}


def k_new_search_s(folder, exp, state):
    """A save of a new row of the class whose triggers keep its index; half: the row is
    saved, its index entry is not."""
    s = {"owner": "t0", "key": "t0x90004", "n": 0, "body": "soak t0x90004v0 common"}
    _intent(folder, "t0", "new_search", keys=[s["key"]], fx=[["put", "soak_search_s", {"key": s["key"]}, s, 0]])
    if state == "none":
        return "t0", set()
    models, db = _sqler_db(folder)
    row = models.SearchS(**s).save(db=db)
    db.close()
    if state == "half":
        conn = sqlite3.connect(folder / "soak.db")
        conn.execute("DELETE FROM soak_search_s_fts WHERE rowid = ?", [row._id])
        conn.commit()
        conn.close()
    return "t0", {("row_unknown", "soak_search_s", row._id), ("search_missing", "soak_search_s_fts", row._id)}


KILLED_CALLS = [k_txn_commit, k_archive, k_new_audit, k_new_search_s]


@pytest.mark.parametrize("state, tolerated", [("none", 0), ("all", 1)])
@pytest.mark.parametrize("call", KILLED_CALLS, ids=[k.__name__[2:] for k in KILLED_CALLS])
def test_a_killed_call_wholly_stored_or_not_stored_passes(copied, call, state, tolerated):
    folder, exp = copied
    worker, _ = call(folder, exp, state)
    report = _check(folder, killed=[worker])
    assert [v.line() for v in report.violations] == []
    assert report.tolerated == tolerated


@pytest.mark.parametrize("call", KILLED_CALLS, ids=[k.__name__[2:] for k in KILLED_CALLS])
def test_a_killed_call_half_stored_fails_naming_both_states(copied, call):
    folder, exp = copied
    worker, names = call(folder, exp, "half")
    report = _check(folder, killed=[worker])
    assert report.names() == names, [v.line() for v in report.violations]
    assert len(report.violations) == 2
    assert all(v.text.startswith("half-applied call ") for v in report.violations)
    assert report.tolerated == 0


def test_two_killed_increments_of_one_counter_pass_only_as_whole_increments(copied):
    """Two killed workers each had an increment of the same counter in flight: 0, 1 or 2
    more is a mix of whole calls; 3 more is not."""
    folder, exp = copied
    c = sorted(exp.live["soak_counter"])[0]
    want = exp.incs.get(c, 0)
    for w in ("t0", "t1"):
        _intent(folder, w, "counter_inc", ids=[["soak_counter", c]], fx=[["inc", "soak_counter", c]])
    conn = sqlite3.connect(folder / "soak.db")
    for more, ok in [(0, True), (1, True), (2, True), (3, False)]:
        conn.execute(
            "UPDATE soak_counter SET data = json_set(data, '$.count', ?), _version = ? WHERE _id = ?",
            [want + more, want + more, c],
        )
        conn.commit()
        report = _check(folder, killed=["t0", "t1"])
        assert report.ok is ok, (more, [v.line() for v in report.violations])
        assert report.tolerated == (more if ok else 0)
    conn.close()


# ---- a killed process's errors, and kills that did not happen

import soak  # noqa: E402
from soak_ops import Journal, Stats, Worker, errors_path, read_errors  # noqa: E402


def _summary(stats, kill=True, kills=1):
    args = argparse.Namespace(seed=1, storage="file", minutes=1.0, ops=None, kill=kill)
    run = SimpleNamespace(totals=lambda: stats, checks=1, kills=kills, reports=[{"violations": 0, "lines": []}])
    res = {"interrupted": False, "report": {"summary": "", "checked": {}, "rows": 0, "tolerated": 0}}
    lines = []
    out = SimpleNamespace(write=lines.append, flush=lambda: None)
    code = soak.summarize(args, "/x", {"procs2": (run, res)}, False, out)
    return code, "".join(lines).strip().splitlines()


def test_a_worker_puts_each_error_on_disk_before_it_goes_on(tmp_path):
    journal = Journal(str(tmp_path / "journal-p0g0w0.jsonl"))
    w = Worker("p0g0w0", 1, {}, None, journal, Stats(), [])
    w.error("OperationalError locked at once", "database is locked", op="new_search")
    # read before the journal is closed: the line is on disk already
    got = read_errors(errors_path(journal.path))
    journal.close()
    assert [(e["worker"], e["error"], e["op"]) for e in got] == [("p0g0w0", "OperationalError locked at once", "new_search")]


def test_a_killed_process_errors_after_its_last_poll_fail_the_run(tmp_path):
    """The parent last polled the process before the error: its polled stats have none. The
    error file has one, and a line the kill cut short: both are counted, and the run fails."""
    with open(tmp_path / "errors-p0g0w0.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"worker": "p0g0w0", "seed": 1, "op_no": 7, "op": "new_search", "error": "OperationalError locked at once", "text": "database is locked"}) + "\n")
    with open(tmp_path / "errors-p0g0w1.jsonl", "w", encoding="utf-8") as f:
        f.write('{"worker": "p0g0w1", "se')
    polled = {"ops": 40, "conflicts": 0, "expected": {}, "errors": {}, "first": []}
    dead = soak.dead_stats(str(tmp_path), ["p0g0w0", "p0g0w1"], polled)
    assert dead["ops"] == 40
    assert dead["errors"] == {"OperationalError locked at once": 1, "unreadable error line": 1}
    total = Stats()
    total.merge(dead)
    code, lines = _summary(total)
    assert code == 1
    assert lines[-1].startswith("SOAK FAIL seed=1 storage=file 1 min workloads=procs2 ops=40 conflicts=0 errors=2 ")


def test_a_run_asked_to_kill_that_killed_nothing_fails():
    code, lines = _summary(Stats(), kill=True, kills=0)
    assert code == 1
    assert lines[1:-1] == ["NO KILLS: --kill was given and no writer process was killed"]
    assert lines[-1].startswith("SOAK FAIL ") and " kills=0 " in lines[-1]
    code, lines = _summary(Stats(), kill=True, kills=3)
    assert code == 0
    assert lines[-1].startswith("SOAK PASS ") and " kills=3 " in lines[-1]


def test_kill_with_ops_is_refused(capsys):
    with pytest.raises(SystemExit) as stop:
        soak.parse(["--ops", "5", "--seed", "1", "--kill", "--workload", "procs2"])
    assert stop.value.code == 2
    assert "--kill needs --minutes: the kills are placed in the run's time" in capsys.readouterr().err
