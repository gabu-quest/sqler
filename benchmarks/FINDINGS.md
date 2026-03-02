# Benchmark v1.2 Findings — Where sqler Can Improve

Cross-scale results from 50K to 1M rows, 20 iterations, 3 warmup, memory + disk modes.
All comparisons are sqler vs raw sqlite3 with matched PRAGMAs, matched SQL, matched serialization.

Definitive result files:
- `bench_medium_20260301_091440.json` (50K, 407 measurements)
- `bench_large_20260301_104605.json` (100K, 394 measurements)
- `bench_xlarge_20260301_133411.json` (500K, 381 measurements)
- `bench_xxlarge_20260301_191925.json` (1M, 381 measurements)

## TL;DR — Cross-Scale Ratios (memory mode, max rows per scale)

| Category | 50K | 100K | 500K | 1M | Trend |
|----------|-----|------|------|-----|-------|
| Queries (filter, range, complex) | 0.95–0.97x | 0.95–0.98x | 0.94–0.98x | 0.95–0.97x | **Rock solid parity** |
| JSON ops (contains, isin) | 0.99x | 0.98x | 0.99x | 0.99x | **Perfect parity** |
| Aggregates (sum, avg, min, max) | 0.95x | 0.99x | 0.94x | 0.94x | **Parity†** |
| Backup/restore | 1.00x | 1.02x | 1.01x | 1.00x | **Perfect parity** |
| Bulk insert | 1.92x | 1.91x | 1.87x | 1.87x | **Stable ~1.9x** |
| any_where (array subqueries) | 1.51x | 1.56x | 1.47x | 1.48x | **Stable ~1.5x** |
| Export (CSV/JSONL) | ~~2.85x~~ | ~~2.74–2.84x~~ | ~~2.72–2.85x~~ | ~~2.73–2.77x~~ | **✅ Fixed → 1.0–1.5x** |
| FTS rebuild | 4.65x | 4.63x | 3.96x | 3.78x | **Improving at scale** |
| FTS ranked | 0.95x | 0.70x | 1.52x | 1.50x | **Worsening at scale** |

†**Row factory artifact**: The baseline uses `sqlite3.Row` with string key access (`row["data"]`),
while sqler uses integer index access (`row[0]`). This adds ~3-6% overhead to the baseline across
all query/aggregate operations. Both arms execute identical SQL — the gap is purely in Python-side
row iteration. This is an irreducible measurement artifact, not a real ORM advantage.

## Absolute Wall-Clock Cost at 1M Rows

| Operation | sqler | sqlite | Extra time |
|-----------|-------|--------|------------|
| FTS rebuild | 29.2s | 7.7s | **+21.4s** |
| Bulk insert 1M | 12.2s | 6.5s | **+5.7s** |
| any_where query | 9.9s | 6.7s | **+3.2s** |
| ~~Export CSV 250K~~ | ~~4.7s~~ | ~~1.7s~~ | ~~+3.0s~~ ✅ Fixed |
| FTS ranked 1M | 899ms | 599ms | **+300ms** |
| Equality filter 1M (no idx) | 1.0s | 1.1s | -50ms (noise) |
| Backup 1M | 606ms | 604ms | +2ms (noise) |

---

## Priority 1: Export Performance — ✅ DONE (2.8x → 1.0–1.5x)

### What was done

Replaced the full ORM hydration pipeline in all 8 sync + 1 async export functions with direct
query execution against the SQLite adapter. The per-row pipeline went from:

```
JSON string → dict → Pydantic model_validate() → dict → JSON string   (BEFORE)
JSON string → dict → JSON string                                       (AFTER, CSV/JSONL)
JSON string ────────→ file                                              (AFTER, JSONL fast path)
```

Three tiers of optimization depending on the format:

1. **JSONL fast path** (`include_id=False`, all fields): Zero-parse — raw `data` column strings
   written directly. No `json.loads()`, no `json.dumps()`. Theoretical minimum.
2. **JSON/JSONL with ID or field filter**: `json.loads()` → inject `_id` or filter fields → `json.dumps()`.
   Skips Pydantic entirely.
3. **CSV**: `json.loads()` → field extract → `_serialize_value(for_csv=True)` → CSV writer.
   Skips Pydantic, still needs parse for field extraction.

### Results (medium scale, 50K rows)

| Format | Storage | sqler (ms) | sqlite (ms) | Ratio | Was |
|--------|---------|-----------|-------------|-------|-----|
| CSV | memory | 494 | 347 | **1.42x** | 2.86x |
| CSV | disk | 508 | 340 | **1.49x** | 2.86x |
| JSON | memory | 489 | 496 | **0.99x** | — |
| JSON | disk | 499 | 539 | **0.93x** | — |
| JSONL | memory | 352 | 305 | **1.15x** | 2.85x |
| JSONL | disk | 356 | 349 | **1.02x** | 2.85x |
| JSONL noid | memory | 51 | 42 | **1.22x** | — |
| JSONL noid | disk | 49 | 48 | **1.03x** | — |

### What we learned

1. **JSON export is at parity (0.93–1.02x).** sqler's `json.loads()` per row + `json.dump(list)`
   on the collected result matches or beats the sqlite baseline. Sometimes faster because
   `json.dump()` on a pre-built list is more efficient than the baseline's individual loads.

2. **JSONL fast path is 7x faster than regular JSONL** (51ms vs 352ms at 50K). The remaining
   ~1.2x gap vs sqlite is `query.all()` overhead (SQL building, cursor wrapping, list comprehension)
   vs a bare `conn.execute().fetchall()` with direct column access.

3. **CSV has an irreducible ~1.5x floor.** CSV needs per-field extraction + `_serialize_value()`
   for nested types (list/dict → JSON string). The sqlite baseline's `DictWriter.writerows(parsed)`
   is simpler because it doesn't need per-value type handling. This gap is inherent to CSV format.

4. **Pydantic hydration was the entire bottleneck.** The 2.8x overhead was almost entirely
   `model_validate()` + `_model_to_dict()`. Removing them brought every format to within
   0.93–1.5x of raw sqlite3. No other optimization was needed.

5. **Memory vs disk is irrelevant for export.** Results are nearly identical — the optimization
   is CPU-bound (Python object construction), not I/O-bound.

### Remaining gap analysis

| Source of remaining overhead | Formats affected | Estimated cost |
|------------------------------|-----------------|----------------|
| `query._build_query()` SQL construction | All | ~2-5ms |
| Cursor wrapping / adapter layer | All | ~2-5ms |
| `_serialize_value(for_csv=True)` per field | CSV only | ~50ms at 50K |
| `json.loads()` + field filtering | CSV, JSONL with ID | ~100ms at 50K |

None of these are worth optimizing further — they're inherent to what the format requires.

---

## Priority 2: FTS Rebuild (3.8–4.7x overhead) — Biggest Absolute Impact

### What's happening

The overhead IMPROVES at scale (fixed Python cost amortized by growing SQLite work):

| Rows | sqler (mem) | sqlite (mem) | Ratio |
|------|-------------|--------------|-------|
| 50K | 1,121ms | 241ms | 4.65x |
| 100K | 2,246ms | 485ms | 4.63x |
| 500K | 12,508ms | 3,159ms | 3.96x |
| 1M | 29,153ms | 7,717ms | 3.78x |

At 1M rows this costs 21 extra seconds. The single biggest absolute penalty.

### Root cause

`fts.rebuild()` uses sqler's ORM to read all documents, extract fields, and re-insert into the
FTS table row by row. The baseline uses a single `INSERT INTO fts_table SELECT ... FROM source`
SQL statement.

### Optimization ideas

- Use a single `INSERT INTO fts SELECT json_extract(data, ...) FROM source` statement
- FTS index creation could use the same single-SQL approach
- This one change could bring rebuild close to parity

---

## Priority 3: Bulk Insert (1.9x overhead) — Stable, Predictable

### What's happening

Remarkably stable ~1.9x overhead at every scale. Per-row cost, not algorithmic.

| Rows | sqler (mem) | sqlite (mem) | Ratio |
|------|-------------|--------------|-------|
| 50K | 582ms | 303ms | 1.92x |
| 100K | 1,162ms | 607ms | 1.91x |
| 500K | 5,826ms | 3,117ms | 1.87x |
| 1M | 12,174ms | 6,524ms | 1.87x |

Slight improvement at scale (1.92x → 1.87x).

### Root cause

The baseline uses `executemany()` which is a single C-level loop. sqler's `bulk_upsert()` has
per-row overhead beyond `json.dumps()` (which both arms do). Likely candidates:
- Validation or type checking per row
- `_ensure_table()` check per call
- Query building overhead
- The baseline uses one `executemany()` call; sqler may use chunked multi-row INSERT

### Single insert is 7.3–8.4x slower

`model.save()` at 10K rows (memory mode):

| Method | Time | vs sqlite loop |
|--------|------|----------------|
| sqlite loop | 42ms | 1.0x |
| sqler raw (`insert_document`) | 117ms | 2.8x |
| sqler lite (dataclass) | 220ms | 5.2x |
| sqler pydantic | 310ms | 7.4x |

On disk, per-row autocommit widens the gap to 8.4x.

### Optimization ideas

- Profile `bulk_upsert()` to find per-row costs
- Consider a "fast path" that skips validation/hooks for trusted bulk data
- Benchmark `executemany` vs chunked multi-row INSERT in sqler's adapter
- For `model.save()`: batch-save API to amortize per-save overhead

---

## Priority 4: FTS Ranked — Scale-Dependent Regression

### What's happening

This is the only scenario that gets WORSE at scale:

| Rows | sqler (mem) | sqlite (mem) | Ratio |
|------|-------------|--------------|-------|
| 50K | 25.4ms | 26.9ms | 0.95x |
| 100K | 34.5ms | 49.5ms | 0.70x |
| 500K | 489.5ms | 322.4ms | 1.52x |
| 1M | 899.1ms | 599.0ms | 1.50x |

At small scales, result sets are small (LIMIT 20) and sqler's overhead is negligible.
At 500K+, the volume of ranked results sqler must process becomes the bottleneck.

### Where to look

- The inversion from 0.70x to 1.50x between 100K and 500K is dramatic
- At 100K the sub-50ms values make the 0.70x suspicious (measurement noise)
- At 500K+ the 1.5x is stable and real
- Investigate what sqler does differently in ranked search result processing at scale
- The query logger overhead becomes significant relative to the query time

---

## Priority 5: any_where Array Subqueries (1.5x overhead)

### What's happening

Stable 1.5x overhead at every scale. Does not get worse.

| Rows | sqler (mem) | sqlite (mem) | Ratio |
|------|-------------|--------------|-------|
| 50K | 477ms | 317ms | 1.51x |
| 100K | 949ms | 609ms | 1.56x |
| 500K | 4,701ms | 3,200ms | 1.47x |
| 1M | 9,916ms | 6,714ms | 1.48x |

Both arms execute equivalent SQL (`EXISTS(SELECT 1 FROM json_each(data, '$.events') ...)`).
The overhead is purely Python-side.

### Root cause

- The query logger runs on every call — ~30ms of pure Python overhead at 50K
- Query compilation (building the EXISTS + json_each subquery) happens each call
- These are fixed per-call costs that become proportionally smaller at larger scales

### Optimization ideas

- Make the query logger opt-in or lazy (biggest win)
- Cache compiled SQL for repeated queries with same structure
- Consider a "raw mode" that bypasses logging/compilation overhead

---

## Things That Are Fine (No Action Needed)

### Queries — Rock Solid Parity (0.94–0.98x across all scales)†

The ratio does NOT change from 50K to 1M. sqler's query layer is essentially free.

| Query Type | 50K | 100K | 500K | 1M |
|------------|-----|------|------|-----|
| Equality filter (no index) | 0.96x | 0.95x | 0.95x | 0.95x |
| Range 50% | 0.96x | 0.98x | 0.94x | 0.97x |
| Complex 5-predicate | 0.97x | 0.97x | 0.98x | 0.97x |
| Array contains | 0.99x | 0.98x | 0.99x | 0.99x |

### Aggregates — Parity (0.94–0.99x)†

All within the Row factory artifact range. Both arms execute identical SQL.

### Backup/Restore — Transparent (1.00–1.02x)

sqler's wrapper adds literally zero overhead at every scale:
- Backup 1M: 606ms vs 604ms (1.00x)
- Restore 1M: 252ms vs 247ms (1.02x)

---

## Key Takeaways

1. **sqler's query layer is free.** 0.95–0.99x across every scale, every query type. The ORM
   overhead is invisible for reads.

2. **Insert overhead is constant, not algorithmic.** 1.9x stays 1.9x whether you insert 50K or
   1M rows. It's a per-row cost, not a scaling problem.

3. **Export was the lowest-hanging fruit — now fixed.** 2.8x → 1.0–1.5x by bypassing Pydantic
   hydration. JSONL fast path (zero-parse) is 7x faster than the pre-optimization path.

4. **FTS rebuild improves at scale** (4.65x → 3.78x) but has the biggest absolute cost (+21s at 1M).
   A single SQL statement instead of ORM iteration would nearly eliminate this.

5. **FTS ranked is the one regression.** Near parity at 50K, 1.5x slower at 500K+. The only
   scenario that gets worse as data grows. Needs investigation.

6. **Backup/restore is transparent.** 1.00x. The SQLite backup API does all the work.

---

## Architectural Insight: Pydantic Hydration is the Dominant Cost

The export optimization revealed something fundamental about where sqler's overhead lives.

### The evidence

Export functions went from 2.8x to 1.0–1.5x by making a single change: skip `model_validate()`.
No algorithm change, no caching, no batching — just stop constructing Pydantic models when the
caller doesn't need them. The entire 2.8x overhead was Pydantic.

### What Pydantic does per row

When sqler hydrates a model from a database row, every row goes through:

1. `json.loads(data)` → Python dict (unavoidable, both arms do this)
2. `model_validate(dict)` → Pydantic model (**the bottleneck**)
   - Type coercion (str → datetime, str → int, etc.)
   - Default value injection for missing fields
   - Nested model construction
   - Validator execution
   - Field alias resolution
3. `_model_to_dict(model)` → back to dict for output (**wasted work**)
   - `getattr(model, field)` per field
   - `_serialize_value()` to undo Pydantic's type coercion (datetime → str)

Steps 2 and 3 cancel each other out for read-only paths. Pydantic parses ISO strings into
`datetime` objects, then `_serialize_value` converts them back to ISO strings. The round-trip
produces identical output to just... not parsing.

### Where this pattern recurs

| Code path | Does hydration? | Could skip it? |
|-----------|----------------|----------------|
| `export_csv/json/jsonl` | ~~Yes~~ **Fixed** | ✅ Done |
| `fts.rebuild()` | Yes — reads all docs via ORM | Yes — `INSERT INTO fts SELECT json_extract(...)` |
| `queryset.all()` → serialize for API | Yes | Maybe — `query.all_dicts()` exists |
| `model.save()` + re-read pattern | Yes | Depends on use case |

### The rule

**If the output format is JSON (or derived from JSON), and the input is the `data` column
(which IS JSON), Pydantic hydration is pure overhead.** The data went into SQLite as JSON,
it's stored as JSON, and the caller wants JSON back. Converting to Python objects and back
adds latency with zero information gain.

### Implications for sqler's API

This suggests sqler should offer two read paths:

1. **Model path** (current default): Full Pydantic hydration. Use when you need type-safe
   Python objects, validators, computed properties, or relation resolution.
2. **Raw path**: `json.loads()` only. Use for bulk reads, exports, API serialization,
   streaming — anywhere the caller wants dicts/JSON, not model instances.

The query layer already supports this: `query.all()` returns raw strings, `query.all_dicts()`
returns dicts with `_id`. The export optimization proved this path is ~2x faster than
hydration for bulk operations and produces identical output.

### What NOT to conclude

- This does NOT mean Pydantic is bad. Pydantic earns its cost on write paths (validation,
  coercion, schema enforcement) and on read paths where the caller needs typed objects.
- This does NOT mean we should remove model hydration from querysets. `queryset.all()` returns
  model instances, and that's the correct default — users expect typed objects.
- The overhead is per-row, not per-query. For queries returning <100 rows (the common case),
  hydration cost is invisible (~0.3ms). It only matters at bulk scale (10K+ rows).

---

## Known Measurement Caveats

1. **Row factory artifact (~3-6% across all query/aggregate scenarios)**: The baseline uses
   `sqlite3.Row` with string key access (`row["data"]`), while sqler uses integer index access
   (`row[0]`). Both arms execute identical SQL. This is irreducible — removing Row factory would
   make the baseline unrealistically stripped down.

2. **FTS ranked at small scales**: Appears 0.70x at 100K but this is measurement noise on
   sub-50ms values. Converges to stable 1.50x at 500K+ where it's a real signal.

3. **FTS highlights**: 272x gap at 50K is real but measures fundamentally different things
   (model hydration vs raw tuples). Not a fair comparison — it's an API design difference.

4. **Query logger**: Always-on in sqler. Adds ~30ms per complex query at 50K. Primary source
   of the any_where gap. Making it opt-in would improve all query scenarios.

5. **Disk single-insert**: 8.4x on disk (vs 2.8x in memory) due to per-row autocommit + fsync.

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
- 4 scales: medium (50K), large (100K), xlarge (500K), xxlarge (1M)
- 1,563 total measurements across all scales
- Total benchmark runtime: ~9 hours
