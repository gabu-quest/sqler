import threading
import time

import pytest
from sqler import SQLerDB
from sqler.models import SQLerSafeModel, StaleVersionError
from sqler.query import SQLerField as F


class Counter(SQLerSafeModel):
    name: str
    count: int


def _worker(increment_times, errors):
    for _ in range(increment_times):
        # optimistic retry loop
        for _try in range(20):
            obj = Counter.query().filter(F("name") == "global").first()
            if obj is None:
                time.sleep(0.001)
                continue
            obj.count += 1
            try:
                obj.save()
                break
            except StaleVersionError:
                time.sleep(0.001)
        else:
            errors.append("max_retries")


@pytest.mark.perf
def test_concurrent_increments(tmp_path):
    db = SQLerDB.on_disk(tmp_path / "wal.db")
    # ensure WAL
    db.adapter.execute("PRAGMA journal_mode=WAL;")
    db.adapter.commit()

    Counter.set_db(db)
    Counter(name="global", count=0).save()

    threads = []
    errors: list[str] = []

    N_THREADS = 8
    INCR = 200

    for _ in range(N_THREADS):
        t = threading.Thread(target=_worker, args=(INCR, errors))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    obj = Counter.query().filter(F("name") == "global").first()
    assert obj.count == N_THREADS * INCR
    assert not errors
    db.close()
