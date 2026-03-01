# Benchmark Scrutiny Fixes

> **v1.1 data has systematic biases.** Multiple unfair advantages (PRAGMA tuning, array SQL,
> missing deserialization, order bias, etc.) mean the v1.1 results do NOT measure pure ORM overhead.
> They show "sqler-with-tuning vs naive-sqlite" which is interesting but not the question we want
> answered. This doc tracks every known issue and the fixes needed for a fair v1.2 comparison.

Do these AFTER the current overnight run completes (that run finishes the unpragma'd data collection).

## Status of existing data

The v1.1 overnight run produces `sqler` vs `sqlite_default` (no PRAGMAs) results for:
- small/medium/large: already have timing benchmarks
- large: 3-run memprofile (in progress)
- xlarge/xxlarge: 1-run memprofile + timing (in progress)

This data is kept as-is — it shows "sqler with tuning vs naive sqlite" which is still interesting. But it's not apples-to-apples for measuring ORM overhead because sqler gets a 32 MB cache and the baseline gets 2 MB.

---

## Fairness issues (full audit)

### HIGH severity

**H-1: PRAGMA mismatch — sqler gets tuned PRAGMAs, baseline gets defaults**
- sqler `in_memory()` sets: 32 MB cache, `synchronous=OFF`, `journal_mode=MEMORY`, `locking_mode=EXCLUSIVE`
- sqlite baseline: 2 MB cache, `synchronous=FULL`, default everything
- Fix: matched PRAGMAs on baseline (see "New matrix" below)

**H-2: Array SQL mismatch — sqler uses faster `json_each` form**
- sqler `contains()`/`isin()`: `json_each(data, '$.path')` — direct path, faster
- baseline: `json_each(json_extract(data, '$.path'))` — extract then iterate, slower
- `any_where` and nested access are already fair (same SQL both sides)
- Fix: update `array_contains()` and `array_isin()` in `sqlite_baseline.py`

**H-3: Scenario 1 (BulkInsertScaling) — different insert APIs**
- sqler uses `bulk_upsert()` which does one `cursor.execute()` per row in a transaction
- baseline uses `executemany()` which does one C-level batch call
- These are structurally different operations, not a fair ORM overhead comparison
- Fix: use `insert_many()` vs `executemany()`, or loop-insert both sides
- File: `suite_insert.py:43-80`

**H-4: Scenario 4 (ModelOverhead) — different commit semantics**
- sqler `insert_document()` calls `auto_commit()` per document (N commits)
- baseline `insert_loop()` commits once at end (1 commit)
- Fix: wrap sqler arm in transaction, or make baseline commit per doc
- File: `suite_insert.py:269-312`

**H-5: Scenario 7 (ComplexFilterChains) — baseline skips JSON deserialization**
- sqler `.all()` returns deserialized `list[dict]` (JSON parsed)
- baseline `fetchall()` returns raw `sqlite3.Row` tuples (no `json.loads()`)
- Fix: add `json.loads(r[0])` to baseline results
- File: `suite_query.py:219-247`

**H-6: Scenario 16 (QueryCacheImpact) — cold cache arm bypasses PrecisionTimer**
- Cold cache measurement has no warmup, no GC isolation, redefines decorated function inside loop
- Every other scenario uses `PrecisionTimer.measure()` with warmup + GC disable
- Fix: use `PrecisionTimer.measure()` for all arms, or document the methodology difference
- File: `suite_advanced.py:342-369`

**H-7: Scenario 20 (ExportPerformance) — baseline skips JSON round-trip**
- baseline writes `r[0]` directly (already a JSON string from SQLite)
- sqler does `json.loads()` → Python dict → `json.dumps()` per row
- Fix: baseline must also `json.loads()` + `json.dumps()`, or sqler must stream raw data
- File: `suite_ops.py:273-278`

**H-8: Scenario 1 (BulkInsertScaling) — `bulk_upsert` mutates input docs**
- `bulk_upsert` sets `doc["_id"] = new_id` in-place on every document
- After first iteration, all subsequent iterations measure UPDATE (upsert) not INSERT
- Baseline always measures INSERT (no `_id` in documents)
- Fix: deep-copy docs per iteration, or use `insert_many()` instead
- File: `sqler_db.py:476-478`, `suite_insert.py:43-44`

### MEDIUM severity

**M-1: Order bias — sqler always measured first**
- In all 22 scenarios, sqler arm runs before sqlite arm in every iteration
- First arm may benefit from cold CPU turbo, second arm benefits from warmer allocator/cache
- Fix: alternate arm order per iteration (even=sqler-first, odd=sqlite-first)
- File: all suite files

**M-2: `row_factory = sqlite3.Row` on sqler connections only**
- sqler pays Row object construction overhead per row fetched
- baseline returns cheap tuples
- Fix: set `conn.row_factory = sqlite3.Row` on baseline connections too
- File: `synchronous.py:65,75`

**M-3: Per-query logger overhead inside timed section**
- sqler's `adapter.execute()` calls `time.perf_counter()` twice + `query_logger.log()` on every SQL
- baseline has no such overhead
- Fix: disable query logging during benchmarks, or document as legitimate ORM overhead
- File: `synchronous.py:103-131`

**M-4: No GC reset between arms in sequential measurement**
- Arms measured sequentially share accumulated heap state
- Later arms run on more fragmented heap
- Fix: explicit `gc.collect()` between arm setups
- File: `suite_insert.py:108-172`

**M-5: Scenario 15 (OptimisticLocking) — WAL vs rollback journal**
- sqler uses WAL mode (readers don't block writers)
- baseline uses default rollback journal (write lock blocks all readers)
- Contention characteristics are structurally different
- Fix: set WAL mode on baseline, or document as intentional feature comparison
- File: `suite_advanced.py:211-283`

**M-6: Scenario 17 (ConnectionPool) — baseline opens connections inside timed window**
- sqler pre-opens connections outside measurement
- baseline `sqlite3.connect()` + `close()` happens inside timed section
- Fix: pre-open baseline connections outside the timed window
- File: `suite_advanced.py:443,509-521`

**M-7: Scenario 19 (ColdVsWarm) — sqler cold query includes `_ensure_table` DDL check**
- First `db.query("bench")` call runs `CREATE TABLE IF NOT EXISTS` inside timed window
- baseline has no such overhead
- Fix: prime `_ensure_table` cache before cold timing, or document as ORM overhead
- File: `suite_ops.py:143-168`

**M-8: Scenario 21 (BackupRestore) — sqler restore opens connection + 8 PRAGMAs inside timed window**
- `SQLerDB.on_disk(rst)` inside timed closure = 8 PRAGMA roundtrips
- baseline `sqlite3.connect()` has zero PRAGMAs
- Fix: pre-open target connection, or add matched PRAGMAs to baseline
- File: `suite_ops.py:338-344`

**M-9: Scenario 14 (FTS) — no baseline for `search_with_highlights`**
- sqler measures `search_with_highlights` but there's no `sqlite_highlights_*` counterpart
- Fix: add baseline using `fts5_highlight()` SQL function
- File: `suite_advanced.py:112-128`

**M-10: Scenario 10 (CountVsMaterialize) — `bool(first())` vs `SELECT 1 LIMIT 1`**
- sqler's `.first()` fetches full document + `json.loads()` just to call `bool()`
- baseline selects constant `1` — no data column, no JSON
- Fix: use `.exists()` on sqler side, or `SELECT data` + `json.loads()` on baseline side
- File: `suite_query.py:418-441`

### LOW severity

**L-1: Query scenarios reuse single connection across selectivities**
- Not independent observations — second selectivity benefits from first's page cache
- Affects both arms equally, so not biased, but weakens statistical independence
- File: `suite_query.py:132-168`

**L-2: Scenario 22 (Aggregates) — `db.query()` calls `_ensure_table` inside every timed aggregate**
- 240 extra `CREATE TABLE IF NOT EXISTS` checks the baseline never pays
- Fix: build query object once outside timed closure
- File: `suite_ops.py:416-430`

---

## What we do next

Replace the `sqlite_*` arm with `sqlite_tuned_*` (matched PRAGMAs). No more unpragma'd runs — that data is captured, we move on.

### New matrix (4 arms)

| Arm | Storage | PRAGMAs |
|-----|---------|---------|
| `sqler_mem_*` | :memory: | sqler in_memory defaults |
| `sqlite_mem_*` | :memory: | matched to sqler in_memory |
| `sqler_disk_*` | tmpfile | sqler on_disk defaults |
| `sqlite_disk_*` | tmpfile | matched to sqler on_disk |

This answers the one question that matters: **how much overhead does the ORM add?**
Both in-memory (pure compute) and on-disk (with I/O).

### sqler in_memory PRAGMAs (to match)
```
PRAGMA foreign_keys = ON
PRAGMA synchronous = OFF
PRAGMA journal_mode = MEMORY
PRAGMA temp_store = MEMORY
PRAGMA cache_size = -32000
PRAGMA locking_mode = EXCLUSIVE
```

### sqler on_disk PRAGMAs (to match)
```
PRAGMA foreign_keys = ON
PRAGMA busy_timeout = 5000
PRAGMA journal_mode = WAL
PRAGMA synchronous = NORMAL
PRAGMA cache_size = -64000
PRAGMA wal_autocheckpoint = 1000
PRAGMA mmap_size = 268435456
PRAGMA temp_store = MEMORY
```

## Disk cleanup discipline

On-disk benchmarks create temporary DB files. Rules to prevent filling the disk:

1. **Use `tempfile.TemporaryDirectory()` as context manager** — auto-deletes on exit AND on crash. Never use `NamedTemporaryFile(delete=False)` without cleanup.

2. **One tmpdir per scenario, cleaned up in teardown** — each scenario gets a single tmpdir in `setup()`, all DB files go inside it, `teardown()` deletes it.

3. **Don't pre-create all DBs upfront for disk mode** — unlike in-memory mode where pre-creating is cheap, pre-creating 23 on-disk DBs of 500K rows would write ~50 GB. Instead, create-and-destroy per iteration for disk mode.

4. **Size estimation before running:**
   - 1 row ≈ 200 bytes on disk (JSON + overhead)
   - 100K rows ≈ 20 MB per DB file
   - 500K rows ≈ 100 MB per DB file
   - 1M rows ≈ 200 MB per DB file
   - With 3 arms × 1 file at a time = max ~600 MB on disk at any moment for xxlarge
   - We have 536 GB free. Not a concern, but cleanup is still hygiene.

5. **Verify cleanup** — add a post-run check that no temp files leaked:
   ```python
   import glob
   leaked = glob.glob("/tmp/tmp*bench*.db")
   if leaked:
       print(f"WARNING: {len(leaked)} temp DB files leaked")
   ```

## Result count verification

After each paired measurement, assert sqler and sqlite return the same row count:
```python
assert len(sqler_results) == len(sqlite_results), \
    f"Result mismatch: sqler={len(sqler_results)}, sqlite={len(sqlite_results)}"
```

Applies to query/json/advanced suites (not aggregates/counts/index creation).

## Implementation plan

1. Wait for overnight run to finish
2. Fix all HIGH issues (H-1 through H-8) — these invalidate results
3. Fix all MEDIUM issues (M-1 through M-10) — these bias results
4. Fix `array_contains()` and `array_isin()` in `sqlite_baseline.py` to use `json_each(data, '$.path')`
5. Add `apply_in_memory_pragmas(conn)` + `apply_on_disk_pragmas(conn)` to `sqlite_baseline.py`
6. Add `conn.row_factory = sqlite3.Row` to all baseline connections
7. Add `--storage disk|memory|both` flag to config/CLI
8. Refactor all 5 suite files:
   a. Replace `sqlite_*` with `sqlite_tuned_*` (matched PRAGMAs for in-memory)
   b. Add disk mode: sqler `on_disk(tmpfile)` + sqlite `connect(tmpfile)` with matched PRAGMAs
   c. Disk mode uses tmpdir context manager with cleanup
   d. Add result count assertions to query scenarios
   e. Alternate arm order per iteration
   f. Add `json.loads()` to all baseline query results
9. Update `charts.py` for the new naming scheme
10. Run: small/medium/large × memory + disk
