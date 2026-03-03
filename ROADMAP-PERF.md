# Roadmap: sqler Performance Optimizations

Post-benchmark findings. Prioritized by impact and effort.

Branch: `feat/perf-optimizations`

## Milestones

### M-1: queryset.as_dicts() + FTS benchmark fix + optimize() 🔄

**as_dicts()** — Expose the existing `query.all_dicts()` through the queryset API.
Bypasses Pydantic hydration for bulk reads where callers want dicts, not model instances.
~2x faster for large result sets. Purely additive, no breaking changes.

- [x] Add `as_dicts()` to `SQLerQuerySet`
- [x] Add `as_dicts()` to `AsyncSQLerQuerySet`
- [x] Tests for both sync and async
- [ ] Document tradeoffs (no validators, no type coercion, no schema drift protection)

**FTS benchmark fix** — The 3.8–4.7x "rebuild gap" was a benchmark asymmetry, not a code
problem. sqler's `fts.rebuild()` already uses a single `INSERT...SELECT` (no ORM iteration).
The sqlite baseline was running FTS5's internal `VALUES('rebuild')` (segment merge) instead
of an equivalent DELETE + repopulate. Fixed the baseline to match.

- [x] Fix `SQLiteFTSBaseline.rebuild()` — DELETE + INSERT...SELECT from source JSON
- [x] Add `db` parameter to `FTSIndex.optimize()` (already existed, pattern parity)
- [ ] Benchmark comparison against v1.2 baseline — FTS rebuild ratio should drop to ~1.0x

### M-2: Query logger + any_where overhead 🔄

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

### M-5: msgspec prototype (SQLerLiteModel) ⬚

Prototype msgspec on the dataclass-based lite model first. It already uses
`object.__setattr__()` for `_id` — no PrivateAttr blocker. Proves the pattern before
touching Pydantic models.

- [ ] Prototype `SQLerLiteModel` backed by msgspec Struct
- [ ] Validate `_id` / `_snapshot` via `__setattr__` injection
- [ ] Benchmark against current dataclass implementation
- [ ] If viable: design `SQLerMsgspecModel` API for opt-in Pydantic replacement

See: `docs/HYDRATION-ALTERNATIVES.md`
