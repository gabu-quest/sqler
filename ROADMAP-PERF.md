# Roadmap: sqler Performance Optimizations

Post-benchmark findings. Prioritized by impact and effort.

Branch: `feat/perf-optimizations`

## Milestones

### M-1: queryset.as_dicts() + FTS single-SQL rebuild 🔄

**as_dicts()** — Expose the existing `query.all_dicts()` through the queryset API.
Bypasses Pydantic hydration for bulk reads where callers want dicts, not model instances.
~2x faster for large result sets. Purely additive, no breaking changes.

- [ ] Add `as_dicts()` to `SQLerQuerySet`
- [ ] Add `as_dicts()` to `AsyncSQLerQuerySet`
- [ ] Tests for both sync and async
- [ ] Document tradeoffs (no validators, no type coercion, no schema drift protection)

**FTS rebuild** — Replace ORM-based rebuild (read all docs → extract fields → re-insert
row by row) with a single `INSERT INTO fts_table SELECT json_extract(data, ...) FROM source`
SQL statement. Currently 3.8–4.7x overhead, +21s at 1M rows. Biggest absolute cost.

- [ ] Rewrite `FTSIndex.rebuild()` to use single SQL INSERT...SELECT
- [ ] Rewrite `FTSIndex.create()` if it uses the same ORM pattern
- [ ] Async equivalents
- [ ] Tests — verify identical FTS content before/after
- [ ] Benchmark comparison against v1.2 baseline

### M-2: Query logger + any_where overhead ⬚

The query logger runs on every `adapter.execute()` call — two `perf_counter()` calls +
`query_logger.log()` per query. Adds ~30ms per complex query at 50K. Primary source of
the any_where 1.5x gap.

- [ ] Make query logger opt-in or lazy (only record when someone is listening)
- [ ] Profile any_where to confirm logger is the main cost
- [ ] Consider SQL compilation caching for repeated query structures
- [ ] Benchmark before/after

### M-3: Bulk insert fast path ⬚

Stable 1.87–1.92x overhead. Per-row Python cost in `bulk_upsert()`. Baseline uses
`executemany()` (C-level batch).

- [ ] Profile `bulk_upsert()` to identify top per-row costs
- [ ] Evaluate "trusted bulk" fast path that skips validation for pre-validated data
- [ ] Benchmark `executemany` vs chunked multi-row INSERT in sqler's adapter
- [ ] Consider batch-save API for `model.save()` amortization

### M-4: FTS ranked scale regression ⬚

Only scenario that worsens at scale: parity at 50K, 1.5x at 500K+. The inversion between
100K (0.70x) and 500K (1.52x) is dramatic.

- [ ] Profile ranked search at 500K to find the scaling bottleneck
- [ ] Investigate result processing path differences vs raw SQL
- [ ] Benchmark before/after

### M-5: msgspec prototype (SQLerLiteModel) ⬚

Prototype msgspec on the dataclass-based lite model first. It already uses
`object.__setattr__()` for `_id` — no PrivateAttr blocker. Proves the pattern before
touching Pydantic models.

- [ ] Prototype `SQLerLiteModel` backed by msgspec Struct
- [ ] Validate `_id` / `_snapshot` via `__setattr__` injection
- [ ] Benchmark against current dataclass implementation
- [ ] If viable: design `SQLerMsgspecModel` API for opt-in Pydantic replacement

See: `docs/HYDRATION-ALTERNATIVES.md`
