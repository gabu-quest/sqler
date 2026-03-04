# Roadmap: sqler Performance Optimizations

Post-benchmark findings. Prioritized by impact and effort.

Branch: `feat/perf-optimizations`

## Milestones

### M-1: queryset.as_dicts() + FTS benchmark fix + optimize() ✅

**as_dicts()** — Expose the existing `query.all_dicts()` through the queryset API.
Bypasses Pydantic hydration for bulk reads where callers want dicts, not model instances.
~2x faster for large result sets. Purely additive, no breaking changes.

- [x] Add `as_dicts()` to `SQLerQuerySet`
- [x] Add `as_dicts()` to `AsyncSQLerQuerySet`
- [x] Tests for both sync and async
- [x] Document tradeoffs (docstring + FINDINGS.md "when skipping is safe/dangerous" section)

**FTS benchmark fix** — The 3.8–4.7x "rebuild gap" was a benchmark asymmetry, not a code
problem. sqler's `fts.rebuild()` already uses a single `INSERT...SELECT` (no ORM iteration).
The sqlite baseline was running FTS5's internal `VALUES('rebuild')` (segment merge) instead
of an equivalent DELETE + repopulate. Fixed the baseline to match.

- [x] Fix `SQLiteFTSBaseline.rebuild()` — DELETE + INSERT...SELECT from source JSON
- [x] Add `db` parameter to `FTSIndex.optimize()` (already existed, pattern parity)
- [x] Benchmark comparison against v1.2 baseline — FTS rebuild 1.02–1.07x across 50K–1M

### M-2: Query logger + any_where overhead ✅

The query logger runs on every `adapter.execute()` call — two `perf_counter()` calls +
`query_logger.log()` per query. Adds ~30ms per complex query at 50K. Primary source of
the any_where 1.5x gap.

- [x] Guard timing behind `query_logger.enabled` check (sync + async adapters)
- [x] Profile any_where — logger was NOT the main cost; redundant `json_extract()` in SQL was
- [x] Fix `json_each(json_extract(data, path))` → `json_each(data, path)` — eliminates 46% overhead
- [x] Benchmark: any_where 1.44–1.67x → 0.95–1.01x (parity)

### M-3: Bulk insert fast path ✅

Stable 1.87–1.92x overhead. Per-row Python cost in `bulk_upsert()`. Baseline uses
`executemany()` (C-level batch).

- [x] Profile `bulk_upsert()` to identify top per-row costs
- [x] Rewrite `bulk_upsert()` to chunked multi-row INSERT (same pattern as `_insert_many_chunked`)
- [x] Benchmark before/after — 1.87–1.92x → 0.91–1.00x at 5K+ rows (medium scale)

### M-4: FTS ranked search optimization ✅

Pre-v1.3 data showed 1.5x regression at 500K+, but the two-query pattern
(FTS rowid lookup + separate SELECT...WHERE IN) was the real bottleneck.

- [x] Rewrite `search_ranked()` to single JOIN query (eliminates second query + `from_ids()` overhead)
- [x] Update baseline to single JOIN for fairness parity
- [x] Benchmark: ranked search at 50K now 1.03–1.07x (was 1.50x at 500K+ pre-M4)

### M-5: msgspec prototype ✅

New `SQLerMsgspecModel` backed by `msgspec.Struct` with `kw_only=True`.
Declared `_id`/`_snapshot` as actual Struct fields with defaults (no `dict=True`).
Full API compatibility with `SQLerLiteModel` — same persistence, queryset,
dirty tracking, and relationship encoding interfaces.

- [x] Add `msgspec>=0.19.0` optional dependency (`pip install sqler[msgspec]`)
- [x] Extend `_compat.py` — `MSGSPEC_AVAILABLE`, `is_msgspec_model()`
- [x] `SQLerMsgspecModelBase` (Struct-based `model_validate`/`model_dump`)
- [x] `SQLerMsgspecModel` (persistence: save/delete/query/filter/all/from_id/etc.)
- [x] Conditional import guard in `models/msgspec/__init__.py`
- [x] 38 tests covering base, CRUD, queryset compat, dirty tracking, error paths
- [x] Hydration benchmark suite (pure + queryset end-to-end)
- [x] Optimization: eliminate dict filtering in model_validate(), cache fields()

**Benchmark results (50K rows, medium scale, post-optimization):**

| Scenario | Lite (dataclass) | Msgspec (Struct) | Speedup |
|----------|-----------------|-----------------|---------|
| Pure `model_validate()` | 151.6ms | 29.7ms | **5.1x** |
| `model_dump()` | 256ms | 189ms | **1.4x** |
| `queryset.all()` mem | 473.7ms | 324.4ms | **1.46x** end-to-end |
| `queryset.all()` disk | 454.8ms | 311.1ms | **1.46x** end-to-end |
| Hydration-only (mem) | ~303ms | ~153ms | **~2.0x** |
| Hydration-only (disk) | ~302ms | ~159ms | **~1.9x** |

Key optimizations applied:
- Removed dict filtering in `model_validate()` — `_id` is a declared Struct
  field, `strict=False` ignores unknowns. Saved ~40ms/50K rows.
- Cached `msgspec.structs.fields()` — uncached reflection was 70µs/call,
  dominating `model_dump()` (3.6s→189ms for 50K calls).

Prototype passes viability: 5.1x pure hydration, 1.46x end-to-end.

See: `docs/HYDRATION-ALTERNATIVES.md`
