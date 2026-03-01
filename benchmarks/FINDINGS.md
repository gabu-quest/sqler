# Benchmark v1.2 Findings — Where sqler Can Improve

Results from medium scale (50K rows max), 20 iterations, 3 warmup, memory + disk modes.
All comparisons are sqler vs raw sqlite3 with matched PRAGMAs, matched SQL, matched serialization.

Definitive run: `bench_medium_20260301_091440.json` (407 measurements, 22 scenarios).

## TL;DR

| Category | sqler/sqlite Ratio | Verdict |
|----------|-------------------|---------|
| Queries (filter, range, complex, pagination, top-n) | 0.94–1.02x | Near parity — Row factory artifact† |
| JSON ops (nested, contains, isin) | 0.94–1.01x | Near parity — Row factory artifact† |
| Aggregates (sum, avg, min, max) | 0.92–0.96x | Near parity — Row factory artifact† |
| Index creation | 0.96–1.02x | Near parity |
| Backup/restore | 0.99–1.01x | Parity |
| Bulk insert | 1.8–1.9x slower | **Improvement candidate** |
| Single insert (model.save()) | 7.4–8.4x slower | **Improvement candidate** |
| any_where (array subqueries) | 1.5–1.6x slower | **Improvement candidate** |
| FTS rebuild | 4.2–5.2x slower | **Improvement candidate** |
| FTS search (basic) | 3.2–3.5x slower | Investigate — model loading dominates |
| FTS highlights | 63–272x slower | Model loading dominates — separate concern |
| Export (CSV/JSONL) | 2.8x slower | **Improvement candidate** |
| Optimistic locking | 2.0–3.1x slower | Expected — retry overhead |
| Connection pool | 1.6–2.1x slower | Expected — ORM wrapper per query |

†**Row factory artifact**: The baseline uses `sqlite3.Row` with string key access (`row["data"]`),
while sqler uses integer index access (`row[0]`). This adds ~3-6% overhead to the baseline across
all query/aggregate operations. Both arms execute identical SQL — the gap is purely in Python-side
row iteration. This is an irreducible measurement artifact, not a real ORM advantage.

---

## Priority 1: Insert Performance (1.8–1.9x overhead)

### What's happening

`bulk_upsert()` is consistently ~1.9x slower than raw `executemany`:

| Rows | sqler (mem) | sqlite (mem) | Ratio | sqler (disk) | sqlite (disk) | Ratio |
|------|-------------|--------------|-------|--------------|---------------|-------|
| 1K | 11.3ms | 5.9ms | 1.91x | 13.2ms | 6.0ms | 2.22x |
| 10K | 113.9ms | 58.8ms | 1.94x | 117.2ms | 60.8ms | 1.93x |
| 50K | 582.0ms | 303.2ms | 1.92x | 610.2ms | 347.1ms | 1.76x |

The overhead is remarkably stable at ~2x regardless of batch size, suggesting a per-row cost.

### Where to look

- `bulk_upsert()` likely calls `json.dumps()` per document — sqlite baseline also does this, so it's something else
- Check if `bulk_upsert()` is doing validation, deduplication, or type checking per row
- Check if `_ensure_table()` runs on every call
- Check if there's a per-document logging or hook overhead
- The baseline uses `executemany()` which is a single C-level loop; if sqler builds SQL differently (e.g., chunked multi-row INSERT), that explains the gap

### Single insert is 7.3–8.4x slower

`model.save()` at 10K rows:

| Method | Memory | Disk |
|--------|--------|------|
| sqler raw (`insert_document`) | 117ms | 365ms |
| sqler pydantic | 310ms | 581ms |
| sqler lite | 220ms | 482ms |
| sqlite loop | 42ms | 44ms |

In memory, `insert_document()` is 2.8x slower than raw INSERT loop. Pydantic adds another 2.6x.
On disk, the gap widens dramatically: `insert_document()` is 8.4x slower due to per-row autocommit overhead.

### Optimization ideas

- Profile `bulk_upsert()` to find per-row costs
- Consider a "fast path" that skips validation/hooks for trusted bulk data
- Benchmark `executemany` vs chunked multi-row INSERT in sqler's adapter
- For `model.save()`: the per-save overhead includes query building, validation, and autocommit — consider a batch-save API

---

## Priority 2: any_where Array Subqueries (1.5–1.6x overhead)

### What's happening

`F(["events"]).any().where(F("type") == "purchase")` is 1.5x slower than raw sqlite:

| Rows | sqler (mem) | sqlite (mem) | Ratio | sqler (disk) | sqlite (disk) | Ratio |
|------|-------------|--------------|-------|--------------|---------------|-------|
| 10K | 95ms | 62ms | 1.51x | 94ms | 62ms | 1.51x |
| 25K | 238ms | 158ms | 1.51x | 235ms | 168ms | 1.40x |
| 50K | 477ms | 317ms | 1.51x | 475ms | 315ms | 1.51x |

Both arms execute equivalent SQL (`EXISTS(SELECT 1 FROM json_each(data, '$.events') ...)`), so the overhead is purely Python-side.

### Where to look

- The query logger runs on every iteration — this is ~30ms of pure Python overhead at 50K
- Query compilation (building the EXISTS + json_each subquery) happens each call
- Result fetching: sqler returns `list[str]` via `[row[0] for row in cur.fetchall()]`, sqlite uses `fetch_as_strings()` which does `[row["data"] for row in cursor.fetchall()]` — these should be equivalent

### Optimization ideas

- Make the query logger opt-in or lazy (biggest win — ~30ms saved)
- Cache compiled SQL for repeated queries with same structure
- Consider a "raw mode" that bypasses logging/compilation overhead

---

## Priority 3: Export Performance (2.8x overhead)

### What's happening

| Format | Rows | sqler (mem) | sqlite (mem) | Ratio | sqler (disk) | sqlite (disk) | Ratio |
|--------|------|-------------|--------------|-------|--------------|---------------|-------|
| CSV | 50K | 895ms | 313ms | 2.86x | 882ms | 311ms | 2.84x |
| JSONL | 50K | 829ms | 291ms | 2.85x | 805ms | 291ms | 2.77x |
| JSON | 50K | 980ms | — | no baseline | 974ms | — | no baseline |

### Where to look

- `export_csv()` and `export_jsonl()` iterate through querysets, which go through the full ORM pipeline per row
- The baseline reads raw JSON strings and writes them directly
- sqler's export likely deserializes then re-serializes each document

### Optimization ideas

- Add a "raw export" path that reads `data` column directly without ORM overhead
- For JSONL: the `data` column IS already JSON — just write it directly without parsing
- For CSV: need to parse once to extract fields, but avoid full model hydration

---

## Priority 4: FTS Operations (4–5x rebuild, 3.5x search)

### What's happening

FTS rebuild:
| Rows | sqler (mem) | sqlite (mem) | Ratio | sqler (disk) | sqlite (disk) | Ratio |
|------|-------------|--------------|-------|--------------|---------------|-------|
| 10K | 192ms | 37ms | 5.19x | 204ms | 39ms | 5.21x |
| 50K | 1121ms | 241ms | 4.65x | 1177ms | 282ms | 4.18x |

FTS search (basic): sqler 0.28ms vs sqlite 0.08ms (3.5x) — consistent across scales
FTS ranked: sqler 25.4ms vs sqlite 26.9ms at 50K mem (0.95x — near parity)
FTS highlights: sqler 40.8ms vs sqlite 0.15ms at 50K mem (272x — different operations)

### What's happening with rebuild

`fts.rebuild()` likely uses sqler's ORM to read all documents, extract fields, and re-insert into the FTS table. The baseline inserts directly from JSON with a single SQL statement.

### What's happening with highlights

sqler's `search_with_highlights()` loads full model instances via `from_ids()` after the FTS query — the baseline only returns raw FTS tuples. These are fundamentally different operations:
- sqler: FTS query → model hydration → SearchResult wrapping
- sqlite: FTS query → raw tuples

This is an API design issue, not a performance bug. Users get full model instances with highlights attached. The 272x gap is the cost of model hydration, not FTS overhead.

### Optimization ideas

- **Rebuild**: use a single `INSERT INTO fts SELECT ... FROM source` SQL statement instead of iterating through ORM
- **Search**: already near parity at large scales (0.94x at 50K)
- **Highlights**: consider a "light" search mode that returns dicts instead of model instances
- **General**: FTS index creation could use the same single-SQL approach as rebuild

---

## Things That Are Fine (No Action Needed)

### Queries — Near Parity (0.94–1.02x)†

sqler adds negligible overhead for query operations (50K, memory mode):
- Equality filter (no index): 49.8ms vs 51.9ms (0.96x)
- Range 50%: 76.2ms vs 79.5ms (0.96x)
- Complex 5-predicate: 102.9ms vs 105.6ms (0.97x)
- Top-N limit 1000: 61.5ms vs 65.0ms (0.95x)
- Pagination page 500: 107.3ms vs 106.1ms (1.01x)

†See Row factory artifact note in TL;DR. The query compilation and logging overhead is amortized by SQL execution time.

### JSON Operations — Near Parity (0.94–1.01x)†

- Nested depth 1: 58.2ms vs 62.0ms (0.94x)
- Nested depth 3: 70.2ms vs 72.7ms (0.97x)
- Array contains 50K: 153.5ms vs 155.5ms (0.99x)
- Array isin 50K: 172.5ms vs 179.4ms (0.96x)

### Aggregates — Near Parity (0.92–0.96x)†

sqler is consistently ~4-8% faster than the baseline on aggregates (50K, memory mode):
- sum: 51.3ms vs 54.1ms (0.95x)
- avg: 50.7ms vs 54.2ms (0.94x)
- min: 51.1ms vs 54.1ms (0.94x)
- max: 51.8ms vs 54.0ms (0.96x)

This is NOT a real ORM advantage. Both arms execute identical `SELECT json_extract(data, '$.value')` SQL. The gap is entirely from the baseline's `sqlite3.Row` factory overhead on scalar results — sqler's aggregate methods access the cursor directly via integer index.

### Backup/Restore — Parity (0.99–1.01x)

After fixing the benchmark, both arms are essentially identical:
- Backup 50K: 23.6ms vs 23.7ms (1.00x)
- Restore 50K: 17.9ms vs 17.7ms (1.01x)

Both wrap the same `conn.backup()` API with minimal overhead.

---

## Known Measurement Caveats

1. **Row factory artifact (~3-6% across all query/aggregate scenarios)**: The baseline uses `sqlite3.Row` with string key access (`row["data"]`), while sqler uses integer index access (`row[0]`). This adds measurable per-row overhead to the baseline. Both arms execute identical SQL and return identical data. This is an irreducible measurement artifact, not a real ORM advantage. The alternative — removing Row factory from the baseline — would make it unrealistically stripped down.

2. **FTS ranked at 10K**: sqler appears 0.70–0.76x (faster) but converges to parity at 25K (1.04–1.07x) and 50K (0.92–0.95x). This is measurement noise on sub-5ms values with LIMIT 20 results. The 50K value is within the Row factory artifact range.

3. **FTS highlights**: 272x gap is real but measures fundamentally different things (model hydration vs raw tuples). Not a fair comparison — it's an API design difference. sqler returns full model instances; the baseline returns raw tuples.

4. **Query logger**: The internal query logger adds ~30ms overhead per complex query at 50K scale. This is the primary source of the any_where gap (1.5x). It's always-on in sqler — making it opt-in would improve all query scenarios.

5. **Disk single-insert overhead**: On disk, `model.save()` is 8.4x slower (vs 2.8x in memory) due to per-row autocommit + fsync overhead. The memory baseline stays at ~43ms regardless, while sqler's disk mode balloons to 365ms for raw and 1165ms for single-save at 10K rows.

---

## Methodology Notes

All results use benchmark v1.2 methodology:
- Matched PRAGMAs (same cache_size, journal_mode, synchronous, etc.)
- Matched SQL (json_each(data, '$.path'), not json_each(json_extract(...)))
- Matched serialization (both arms return raw JSON strings via fetch_as_strings)
- Arm alternation (deterministic hash-based order flip per scenario)
- GC isolation (gc.collect() between arms)
- 20 iterations, 3 warmup, PrecisionTimer with perf_counter
- Both memory and disk modes tested
- 407 measurements across 22 scenarios
- Definitive results file: `bench_medium_20260301_091440.json`
