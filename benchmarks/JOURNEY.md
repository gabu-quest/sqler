# The Benchmark Journey

How sqler's benchmark suite went from "looks fast" to "provably fair" — and what we
learned about where the real overhead lives.

## Chapter 1: v1.1 — The Naive Benchmarks

The first benchmark suite (v1.1) measured 22 scenarios across 5 suites: insert, query,
json, advanced, and ops. Every scenario had a sqler arm and a sqlite3 baseline. Results
looked good — sqler was within 1–2x of raw sqlite for most operations.

Then we ran an adversarial audit.

### 18 fairness issues found

The audit (`TODO-SCRUTINY.md`) found 8 HIGH, 10 MEDIUM, and 2 LOW severity fairness
issues. The benchmarks weren't measuring ORM overhead — they were measuring
"sqler-with-tuning vs naive-sqlite":

| Issue | What was unfair |
|-------|----------------|
| **PRAGMA mismatch** | sqler got 32 MB cache, WAL mode, synchronous=OFF. Baseline got 2 MB cache, rollback journal, synchronous=FULL. |
| **SQL mismatch** | sqler's `json_each(data, '$.path')` is faster than the baseline's `json_each(json_extract(data, '$.path'))` |
| **Missing deserialization** | Baseline returned raw tuples while sqler returned parsed dicts — not measuring equivalent work |
| **Input mutation** | `bulk_upsert()` mutated docs in-place, turning INSERT into UPDATE on subsequent iterations |
| **Fixed arm order** | sqler always ran first — CPU turbo boost, allocator state, cache warming all create order bias |
| **Asymmetric connections** | sqler pre-opened connections; baseline `connect()` was inside the timed window |
| **Missing baselines** | `search_with_highlights` had no sqlite counterpart (272x "gap" was meaningless) |
| **Commit semantics** | sqler autocommitted per row; baseline committed once at end |

Every one of these biased in sqler's favor. Not intentionally — just the natural result
of writing benchmarks that test "does my code work" instead of "is this comparison fair."

### The lesson

**The adversarial review question that matters: "If I wanted to make the other arm win,
what would I change?"** If the answer reveals an asymmetry, the benchmark is broken.

## Chapter 2: v1.2 — The Fair Rewrite

All 22 scenarios were rewritten from scratch with matched methodology:

- **Matched PRAGMAs**: Both arms get identical `cache_size`, `journal_mode`, `synchronous`, etc.
- **Matched SQL**: Both arms execute equivalent queries (same `json_each` form, same predicates)
- **Matched serialization**: Both arms do `json.loads()` on results
- **Matched connections**: `sqlite3.Row` factory on both arms, pre-opened connections
- **Arm alternation**: Deterministic hash-based order flip per scenario (no "sqler always first")
- **GC isolation**: `gc.collect()` between arms
- **Input protection**: Deep-copy mutable data between iterations
- **Result verification**: Assert both arms return the same row count
- **Dual storage**: Memory AND disk modes tested (`--storage both`)

### The new results told a different story

With fair baselines, most of the "optimism" disappeared. But the real numbers were more
useful — they pointed at exactly where the overhead actually lives:

| Category | v1.1 ratio | v1.2 ratio | What changed |
|----------|-----------|-----------|--------------|
| Queries | ~0.9x ("faster") | 0.95–0.98x | Was row factory artifact all along |
| Bulk insert | ~1.5x | 1.87–1.92x | Baseline was using `executemany()` vs sqler's per-row loop |
| Exports | ~2.0x | 2.72–2.85x | Baseline was skipping JSON parse |
| FTS rebuild | ~3.5x | 3.78–4.65x | Similar — configuration was masking ORM cost |

The v1.2 numbers are *worse* but *honest*. And honest numbers are what you need to
optimize the right things.

## Chapter 3: Cross-Scale Validation (50K → 1M)

Ran all 5 suites at 4 scales: medium (50K), large (100K), xlarge (500K), xxlarge (1M).
Both memory and disk. 20 iterations, 3 warmup. ~9 hours of total runtime. 1,563
measurements.

### What scales and what doesn't

The most important finding: **most ratios are remarkably stable across scale.**

| Category | 50K | 100K | 500K | 1M | Behavior |
|----------|-----|------|------|-----|----------|
| Queries | 0.95x | 0.95x | 0.95x | 0.95x | Flat — ORM overhead is invisible |
| Bulk insert | 1.92x | 1.91x | 1.87x | 1.87x | Flat — per-row cost, not algorithmic |
| any_where | 1.51x | 1.56x | 1.47x | 1.48x | Flat — fixed per-call overhead |
| FTS rebuild | 4.65x | 4.63x | 3.96x | 3.78x | **Improving** — fixed Python cost amortized |
| FTS ranked | 0.95x | 0.70x | 1.52x | 1.50x | **Worsening** — only regression at scale |
| Exports | 2.85x | 2.84x | 2.85x | 2.77x | Flat — then we fixed it (see Chapter 4) |

**Flat ratios mean per-row overhead.** The ORM adds a constant cost per row, which doesn't
change whether you process 50K or 1M. This is good news — it means the overhead is
addressable with per-row optimizations, not algorithmic redesigns.

**Improving ratios mean fixed overhead being amortized.** FTS rebuild's ratio drops from
4.65x to 3.78x because the Python-side cost is fixed while the SQLite work grows linearly.

**Worsening ratios are the real concern.** FTS ranked is the only scenario that gets
worse at scale. At 50K it's at parity; at 500K+ it's 1.5x. Something in sqler's ranked
search processing scales worse than the raw SQL path.

### Memory vs disk

At ≤500K rows, memory and disk results are nearly identical. At 1M rows in memory mode,
GC pressure on large Python string lists (~1–2GB) creates outliers. The same operations
on disk show clean ratios because the OS page cache manages memory differently.

**Rule: always benchmark both, report disk numbers as the authoritative result.** Memory
mode is useful for isolating compute overhead, but disk mode is what users actually run.

## Chapter 4: The Export Optimization

Exports were the lowest-hanging fruit: stable 2.8x overhead across all scales, all formats.

### Root cause

Every export function called `queryset.all()` which hydrates every row through Pydantic:

```
JSON string → dict → model_validate() → Model instance → _model_to_dict() → dict → output
```

The sqlite baseline does: `JSON string → dict → output`. That's the target.

Steps 2-3 (Pydantic hydration) and step 4 (`_model_to_dict()`) cancel each other out for
read-then-serialize paths. Pydantic parses ISO strings into `datetime` objects, then
`_serialize_value()` converts them back to ISO strings. The round-trip produces identical
output to just... not parsing.

### The fix

Rewrote all 8 sync + 1 async export functions to bypass Pydantic entirely. Three tiers:

1. **JSONL fast path** (`include_id=False`, all fields): Zero-parse — raw `data` column
   strings written directly to file. No `json.loads()`, no `json.dumps()`. This is the
   theoretical minimum — you can't go faster than not parsing.

2. **JSON/JSONL with ID or field filter**: `json.loads()` → inject `_id` or filter fields
   → `json.dumps()`. Skips Pydantic, still parses JSON.

3. **CSV**: `json.loads()` → per-field extraction → `_serialize_value(for_csv=True)` → CSV
   writer. Skips Pydantic, but needs parse for field extraction and nested type serialization.

### Results after optimization

| Format | Before | After (disk, 50K–1M) | Why the remaining gap |
|--------|--------|---------------------|----------------------|
| CSV | 2.8x | **1.33–1.37x** | Per-field extraction + `_serialize_value()` — irreducible |
| JSON | 2.8x | **0.88–1.00x** | At or below parity — `json.dump(list)` is efficient |
| JSONL | 2.8x | **1.03–1.11x** | Near parity |
| JSONL noid | N/A | **0.96–1.00x** | Zero-parse fast path — parity achieved |

JSON export is sometimes *faster* than raw sqlite3. The reason: sqler's `json.dump()` on a
pre-built list is more efficient than the baseline's per-row `json.dumps()` + write.

### The fairness gauntlet

The export benchmarks went through three rounds of fairness corrections before we trusted
the numbers:

1. **Missing JSON baseline** — First results had no sqlite JSON comparison. Added
   `do_sqlite_json()` with matched _id injection.

2. **Fast path not tested** — The benchmark used `include_id=True` (default), so the
   zero-parse JSONL fast path was never exercised. Added `jsonl_noid` variant.

3. **_id asymmetry** — All sqlite baselines were doing `SELECT data FROM` while sqler
   defaulted to `include_id=True` (SELECT _id, data + inject _id into dict). sqler was
   doing strictly more work than the baseline. Fixed all baselines to also SELECT _id
   and inject it.

Each correction was caught by reviewing the results with the adversarial question: "is
this comparison actually measuring the same thing?"

## Chapter 5: The Pydantic Discovery

The export optimization revealed something deeper: **Pydantic hydration is the dominant
cost in every bulk read path.**

### The numbers

| Step | Per-row cost | At 1M rows |
|------|-------------|-----------|
| `json.loads()` | ~200 ns | ~200ms |
| `model_validate()` | ~1,100 ns | ~1.1s |
| `_model_to_dict()` | ~500 ns | ~500ms |
| **Total hydration** | **~1,600 ns** | **~1.6s** |

Removing `model_validate()` + `_model_to_dict()` eliminated the entire 2.8x gap. No other
optimization was needed for exports.

### The surprise: model_construct() is SLOWER

The obvious next thought was "use `model_construct()` to skip validation." But in
Pydantic v2, `model_construct()` is **slower** than `model_validate()`:

```
model_validate():   ~1,100 ns  (Rust-compiled validation)
model_construct():  ~2,540 ns  (Python-side attribute setting)
```

Confirmed by Pydantic maintainers in [pydantic/pydantic#10536](https://github.com/pydantic/pydantic/issues/10536).
The v2 Rust core makes the validated path faster than the unvalidated Python path.
`model_construct()` is a dead end — slower AND less safe.

### Where hydration cost still hides

| Code path | Uses hydration? | Could skip it? |
|-----------|----------------|----------------|
| `export_csv/json/jsonl` | ~~Yes~~ **Fixed** | Done |
| `fts.rebuild()` | Yes — reads all docs via ORM | Yes — single SQL INSERT...SELECT |
| `queryset.all()` → API serialize | Yes | Maybe — `queryset.as_dicts()` |
| `model.save()` + re-read | Yes | Depends on use case |

## Chapter 6: The msgspec Question

With Pydantic identified as the bottleneck, we evaluated alternatives. msgspec was the
most promising:

| | Pydantic v2 | msgspec | Raw dicts |
|--|------------|---------|-----------|
| Per-row cost | ~1,100 ns | ~145 ns | ~200 ns |
| Speedup | 1x | **8x** | **5.5x** |
| Type safety | Full | Full | None |
| Validators | Per-field | `__post_init__` only | None |
| Schema drift protection | Yes | Yes | No |
| Memory (bulk) | Baseline | **25x less** | Minimal |

### Blockers for sqler

**1. PrivateAttr (HARD BLOCKER)** — sqler stores `_id` and `_snapshot` as Pydantic
`PrivateAttr`. msgspec Structs have no equivalent — every field must be declared. The
cleanest workaround is storing `_id` outside the model entirely (e.g., `(id, model)` tuples
from querysets), but that's a fundamental API change.

**2. Field validators (MEDIUM BLOCKER)** — msgspec only has `__post_init__`. No per-field
validators, no `mode='before'` preprocessing. sqler doesn't use them internally, but user
models might.

**3. model_fields introspection (MEDIUM BLOCKER)** — Used in ~20 call sites. All need
rewriting from `cls.model_fields` (dict of FieldInfo) to `msgspec.structs.fields(cls)`
(tuple).

**4. Computed fields** — msgspec has no `@computed_field`. Properties work but don't
serialize. User models might depend on this.

### Migration paths evaluated

| Path | Breaking? | Effort | Benefit |
|------|-----------|--------|---------|
| **Parallel model base** (`SQLerMsgspecModel`) | No | Medium | 8x for opt-in models |
| **Internal-only** (raw dicts in hot paths) | No | Low | Already done for exports |
| **Full migration** (replace Pydantic) | Yes — every user | Very high | 8x everywhere |
| **Wait for Pydantic v3** | No | Zero | Unknown timeline |

### Where it stands

**Not yet decided.** The analysis is in `docs/HYDRATION-ALTERNATIVES.md`. The open
questions:

1. **Is `queryset.as_dicts()` worth exposing?** The query layer already has
   `query.all_dicts()` internally. Making it a public queryset API would give users a
   ~2x speedup for bulk reads where they want dicts, not model instances. The tradeoff:
   no validators, no type coercion, no schema drift protection.

2. **Is `SQLerMsgspecModel` viable?** The PrivateAttr blocker is solvable — store `_id`
   outside the model as a `(id, struct)` tuple. But that changes the ergonomics
   (`user._id` becomes `result.id` or similar). Worth prototyping to see if the API feels
   right.

3. **Should we wait for Pydantic v3?** If v3 closes the gap with msgspec (plausible given
   the v1→v2 Rust rewrite trajectory), the migration becomes unnecessary. But the timeline
   is unknown.

4. **What about SQLerLiteModel?** The dataclass-based lite model doesn't use PrivateAttr.
   Swapping its internals for msgspec is lower risk and would prove the pattern before
   touching the Pydantic model.

## Chapter 7: What's Left to Optimize

### Tier 1: Low-hanging fruit

**FTS rebuild (3.8–4.7x, +21s at 1M)** — The biggest absolute cost. Currently reads all
docs through ORM, extracts fields in Python, re-inserts row by row. Replace with a single
`INSERT INTO fts_table SELECT json_extract(data, ...) FROM source` SQL statement. Should
bring it close to parity.

### ~~Tier 2: Per-row overhead~~ → FIXED

**~~Bulk insert (1.87–1.92x)~~** → **0.91–1.00x** — Chunked multi-row INSERT (M-3).
See Chapter 9.

**~~any_where (1.47–1.51x)~~** → **0.95–1.01x** — `json_each(data, path)` instead of
`json_each(json_extract(data, path))` (M-2). Logger guard also helps.

### Tier 3: Scale-dependent

**FTS ranked (1.50x at 500K+)** — The only scenario that worsens at scale. At 50K it's at
parity; at 500K+ it's 1.5x. The inversion between 100K (0.70x) and 500K (1.52x) is
dramatic. Something in sqler's ranked search result processing doesn't scale linearly.
Needs profiling.

### Tier 4: Architectural

**Pydantic hydration in `queryset.all()`** — The export fix proved the pattern works but
only for terminal operations. For the general read path, the options are:
- `queryset.as_dicts()` (easy, no breaking changes)
- `SQLerMsgspecModel` (8x faster, breaking for adopters)
- Wait for Pydantic v3 (free, uncertain timeline)

See `docs/HYDRATION-ALTERNATIVES.md` for the full analysis.

### Things that are done

| Category | Status | Ratio |
|----------|--------|-------|
| Queries (filter, range, complex) | **No action needed** | 0.95–0.98x (parity) |
| JSON ops (contains, isin) | **No action needed** | 0.98–0.99x (parity) |
| Aggregates | **No action needed** | 0.94–0.99x (parity) |
| Backup/restore | **No action needed** | 1.00–1.02x (transparent) |
| Exports (CSV/JSON/JSONL) | **Fixed** | 0.96–1.37x (was 2.8x) |
| Bulk insert | **Fixed** | 0.91–1.00x (was 1.87–1.92x) |
| any_where | **Fixed** | 0.95–1.01x (was 1.47–1.51x) |

---

## Chapter 8: The FTS Rebuild Correction (M-1)

The 3.8–4.7x FTS rebuild gap that looked like sqler's biggest performance problem turned
out to be a **benchmark asymmetry** — the 19th fairness issue, found during M-1 planning.

### What was wrong

| | sqler `fts.rebuild()` | sqlite baseline `fb.rebuild()` |
|--|----------------------|-------------------------------|
| Operation | DELETE all rows + INSERT...SELECT from source JSON | FTS5 internal `INSERT INTO fts(fts) VALUES('rebuild')` |
| What it does | Repopulates entire FTS table from JSON data — O(n rows) | Merges FTS5 index segments — O(segments), does NOT re-read source |
| Cost | Proportional to table size | Proportional to segment count (near-constant) |

sqler's `rebuild()` was already a single SQL statement — `INSERT INTO fts_table(rowid, ...)
SELECT _id, json_extract(data, ...) FROM source`. No ORM iteration, no per-row Python. The
code was never the problem. The baseline was simply doing less work.

### The fix

Made `SQLiteFTSBaseline.rebuild()` do equivalent work: DELETE all FTS rows, then
INSERT...SELECT with `json_extract()` from the source table. Same operation, same cost.

### Results — medium + large scale (10K–100K, 20 iterations, 3 warmup)

**First run** used the M-1 rebuild fix only. Ranked search showed sqler 3x faster than
raw sqlite3 at 100K — suspiciously good. Adversarial audit found **3 more asymmetries**
(fairness issues #20–22):

| # | Issue | Bias direction |
|---|-------|---------------|
| 20 | **Tokenizer mismatch** — sqler used `porter unicode61`, baseline used `unicode61` only | Different index structures, different match sets |
| 21 | **search/search_ranked work asymmetry** — baseline returned raw FTS tuples; sqler did FTS query → second SQL query → json.loads() → model_validate() → SearchResult | Baseline doing dramatically less work |
| 22 | **No result count verification** between arms | Could be matching different document counts |

Fixed all three: matched tokenizer, made baseline do two-query + `json.loads()` (matched
to sqler minus Pydantic), added result count awareness via consistent LIMIT.

**FTS Rebuild** — 4.65x → 1.03–1.07x (v1.3 fair baseline):

| Rows | sqler (mem) | sqlite (mem) | Ratio | sqler (disk) | sqlite (disk) | Ratio |
|------|-------------|--------------|-------|--------------|---------------|-------|
| 10K | 195ms | 183ms | 1.07x | 211ms | 207ms | 1.02x |
| 25K | 501ms | 473ms | 1.06x | 542ms | 524ms | 1.04x |
| 50K | 1,156ms | 1,087ms | **1.06x** | 1,231ms | 1,189ms | **1.04x** |
| 100K | 2,424ms | 2,271ms | **1.07x** | 2,480ms | 2,573ms | **0.96x** |

Rebuild is now at near-parity. The matched tokenizer tightened the ratio further
(v1.3 baseline builds a matched Porter index). On disk at 100K, sqler is actually
*faster* (0.96x) — likely within noise, but the overhead is negligible either way.

**FTS Ranked** — sqler 0.53–1.15x (v1.3 fair baseline):

| Rows | sqler (mem) | sqlite (mem) | Ratio | sqler (disk) | sqlite (disk) | Ratio |
|------|-------------|--------------|-------|--------------|---------------|-------|
| 10K | 4.2ms | 3.6ms | 1.15x | 4.0ms | 3.9ms | 1.02x |
| 25K | 14.8ms | 19.4ms | 0.76x | 15.0ms | 19.5ms | 0.77x |
| 50K | 26.5ms | 24.3ms | 1.09x | 27.5ms | 25.8ms | 1.07x |
| 100K | 40.1ms | 76.0ms | **0.53x** | 63.6ms | 78.7ms | **0.81x** |

With the v1.3 fair baseline (two-query + json.loads, matched tokenizer), the wild 0.33x
is gone. At 10K and 50K, ranked is near parity (1.02–1.15x). At 25K, sqler is faster
(0.76x). At 100K, sqler is still faster (0.53x mem, 0.81x disk) — this is more plausible
since sqler's FTS search reuses an already-open adapter and skips connection overhead,
but the remaining gap at 100K still deserves investigation at 500K+.

**Note:** The baseline still does less work than sqler — it skips Pydantic `model_validate()`
and `SearchResult` wrapping. A perfectly matched comparison would make the baseline even
slower. The current v1.3 baseline is conservative (biased against sqler), which is the
correct direction for credible results.

**FTS Highlights** — still shows a large gap (55–370x) but this is an apples-to-oranges
comparison. sqler's `search_with_highlights()` returns full model instances with
highlights attached (Pydantic hydration + model loading). The baseline returns raw tuples
with just the highlighted text. These are fundamentally different operations measuring
different things. Not a performance issue — an API design difference.

### Also shipped: queryset.as_dicts()

Added `as_dicts()` to both `SQLerQuerySet` and `AsyncSQLerQuerySet`. Exposes the
existing `query.all_dicts()` through the queryset API, bypassing Pydantic hydration for
bulk reads. ~2x faster for large result sets. Purely additive, no breaking changes.

Also added `db` parameter to `FTSIndex.optimize()` for API consistency with other methods.

### Updated summary table

| Category | v1.2 ratio | After M-1 (v1.3 fair, 10K–100K) | What changed |
|----------|-----------|--------------------------------|--------------|
| Queries | 0.95–0.98x | — | No change |
| Bulk insert | 1.87–1.92x | — | No change |
| Exports | 0.96–1.37x | — | No change (fixed in Ch.4) |
| FTS rebuild | 3.78–4.65x | **0.96–1.07x** | Baseline fix — was measuring different operations |
| FTS ranked | 0.70–1.52x | **0.53–1.15x** | Fair baseline (two-query + json.loads + matched tokenizer) |
| any_where | 1.47–1.51x | — | No change |

### The lesson (again, and again)

Three fairness lessons in one chapter:

1. **Rebuild (issue #19)**: FTS5's `VALUES('rebuild')` command *looks* like a rebuild but
   is actually segment optimization. The naming was misleading, and the benchmark inherited
   the confusion.

2. **Ranked search (issues #20–21)**: The baseline was returning raw tuples from the FTS
   table while sqler was doing a second SQL query, JSON deserialization, Pydantic validation,
   and result wrapping. Reporting sqler as 3x faster than raw sqlite3 would have been
   immediately falsifiable.

3. **Tokenizer (issue #22)**: Different tokenizers produce different index structures. Even
   if both arms run the same SQL, the underlying b-tree shapes differ, making query time
   comparisons invalid.

The adversarial question caught all three: **"Would a competitor accept these results?"**
No — they'd immediately spot that sqler can't be 3x faster than raw sqlite3 while doing
4x more work per call. The skepticism was correct.

Total fairness issues found across the benchmark journey: **22** (18 in v1.1, 4 in v1.2/v1.3).

---

## Chapter 9: The Bulk Insert Fix (M-3)

The second-biggest per-row overhead after Pydantic hydration: `bulk_upsert()` at a stable
1.87–1.92x across all scales. The baseline used `executemany()` (C-level batch loop), while
sqler called `cursor.execute()` once per document — N cursor creations, N parameter bindings,
N `lastrowid` reads.

### The overhead breakdown

| Source | % of gap | Fixable? |
|--------|----------|----------|
| Per-row `cursor()` + `execute()` | ~45% | Yes — batch into chunks |
| Per-row dict comprehension (filter `_id`) | ~25% | Yes — batch in list comp |
| Per-row `int(lastrowid)` + `append()` | ~15% | Yes — compute ID range |
| `json.dumps()` per row | ~15% | No — both arms pay this |

### The fix

Same pattern sqler already used in `_insert_many_chunked()`: split docs into inserts (no
`_id`) and updates (has `_id`), then build multi-row SQL for each batch. One `INSERT INTO t
(data) VALUES (json(?)), (json(?)), ...` per 999-row chunk. One `INSERT ... ON CONFLICT(_id)
DO UPDATE` per 499-row chunk (2 params per row, stays under sqlite's 999 param limit).

The multi-row INSERT pattern sends fewer SQL statements to sqlite's parser. At 50K rows,
that's ~50 SQL calls instead of 50,000. The parser overhead dominates at scale.

### Results — medium scale (50K rows, 20 iterations, 3 warmup)

| Rows | Storage | sqler | sqlite | Ratio | vs Pre-M3 |
|------|---------|-------|--------|-------|-----------|
| 1K | memory | 7.4ms | 5.9ms | 1.25x | was ~1.9x |
| 1K | disk | 9.4ms | 6.1ms | 1.54x | was ~1.9x |
| 5K | memory | 30.4ms | 30.3ms | 1.00x | was ~1.9x |
| 5K | disk | 32.8ms | 31.3ms | 1.05x | was ~1.9x |
| 10K | memory | 59.6ms | 61.6ms | **0.97x** | was ~1.9x |
| 10K | disk | 62.3ms | 65.1ms | **0.96x** | was ~1.9x |
| 25K | memory | 144.8ms | 155.8ms | **0.93x** | was ~1.9x |
| 25K | disk | 153.8ms | 158.5ms | **0.97x** | was ~1.9x |
| 50K | memory | 299.2ms | 315.5ms | **0.95x** | was ~1.9x |
| 50K | disk | 333.6ms | 368.1ms | **0.91x** | was ~1.9x |

At 5K+ rows, sqler is at parity or **faster** than the `executemany()` baseline.

### Why sqler can be faster than raw sqlite's executemany

This sounds surprising — how can an ORM beat the C-level `executemany()`? The answer is in
what SQLite's parser does:

- **`executemany()`** sends N separate `INSERT INTO t (data) VALUES (json(?))` statements.
  The SQL parser processes each one individually. That's N parse + N compile + N execute steps.
- **Multi-row INSERT** sends one `INSERT INTO t (data) VALUES (json(?)), (json(?)), ...` with
  up to 999 rows. The SQL parser processes it once. That's 1 parse + 1 compile + 1 execute
  step (with a larger parameter set).

The parser overhead per statement is small (~1–2µs), but at 50K rows it adds up. The multi-row
approach amortizes it away. The remaining Python overhead (json.dumps, list building) is offset
by the parser savings.

### Fairness note

The comparison is fair but the approaches differ: sqler uses chunked multi-row INSERT, while
the sqlite baseline uses `executemany()`. Both are valid bulk insert strategies. The multi-row
pattern happens to be faster for large batches because it sends fewer statements to the parser.

A truly apples-to-apples test would have both arms use the same strategy (either both
`executemany` or both multi-row). But `executemany` is what most developers use with raw
sqlite3, so it's the natural baseline.

### Doc size impact — parity across all sizes

The doc_size_impact scenario (fixed 10K rows, varying doc sizes) also showed near-parity:

| Size | sqler (disk) | sqlite (disk) | Ratio |
|------|-------------|--------------|-------|
| tiny | 46.7ms | 47.2ms | 0.99x |
| small | 63.4ms | 62.6ms | 1.01x |
| medium | 92.5ms | 94.7ms | 0.98x |
| large | 211.3ms | 202.2ms | 1.05x |
| huge | 706.7ms | 675.8ms | 1.05x |

For larger documents, `json.dumps()` dominates — both arms pay the same cost. The 1.05x at
large/huge is within noise.

### Updated summary table

| Category | Pre-M3 ratio | After M-3 (medium) | What changed |
|----------|-------------|-------------------|--------------|
| Queries | 0.95–0.98x | — | No change |
| Bulk insert | **1.87–1.92x** | **0.91–1.00x** | Chunked multi-row INSERT |
| Exports | 0.96–1.37x | — | No change (fixed in Ch.4) |
| FTS rebuild | 0.96–1.07x | — | No change (fixed in Ch.8) |
| FTS ranked | 0.53–1.15x | — | No change (Ch.8) |
| any_where | ~~1.47–1.51x~~ | — | Fixed in M-2 (0.95–1.01x) |

---

## Chapter 10: M-4 — FTS Ranked Search Single-JOIN Optimization

### The problem

Pre-v1.3 data showed FTS ranked search worsening at scale:

| Scale | Ratio | Note |
|-------|-------|------|
| 50K | 0.95x | Parity |
| 100K | 0.70x | Suspicious (sub-50ms noise) |
| 500K | 1.52x | Regression |
| 1M | 1.50x | Regression |

After v1.3 fairness fixes (issues #20–22), the 10K–100K data showed parity (0.53–1.15x),
but 500K+ hadn't been retested. The question: was the regression real, or a baseline artifact?

### Root cause: two-query pattern

Regardless of the regression, `search_ranked()` had an architectural inefficiency. It used
two separate SQL queries:

```
-- Query 1: FTS search for rowids + scores
SELECT rowid, bm25(fts_table) as score FROM fts_table WHERE MATCH ? ORDER BY score LIMIT ?;

-- Query 2: Fetch documents by ID list
SELECT _id, data FROM source_table WHERE _id IN (?, ?, ...);
```

Plus Python-side overhead: `from_ids()` → `find_documents()` → `_batch_resolve()` →
`model_validate()` per row → dict lookup + sort.

### The fix: single JOIN query

Replaced both queries with a single JOIN:

```sql
SELECT t._id, t.data, bm25(fts_table) as score
FROM fts_table f
JOIN source_table t ON t._id = f.rowid
WHERE fts_table MATCH ?
ORDER BY score
LIMIT ? OFFSET ?;
```

This eliminates:
- Second SQL query (IN-clause construction + execution)
- `from_ids()` → `find_documents()` path (queryset creation, `_batch_resolve()`)
- Python-side sort (SQL `ORDER BY` handles it)
- Dict lookup overhead (scores come in the same row as documents)

Still does `model_validate()` per row for type safety — the 20-row LIMIT makes this cost
negligible (~22µs total).

Updated the baseline to also use a single JOIN for fairness parity.

### Results — medium scale (50K rows, 20 iterations, 3 warmup)

| Rows | sqler (mem) | sqlite (mem) | Ratio | sqler (disk) | sqlite (disk) | Ratio |
|------|-------------|--------------|-------|--------------|---------------|-------|
| 10K | 5.0ms | 4.4ms | 1.13x† | 4.4ms | 6.5ms | 0.68x† |
| 25K | 17.0ms | 16.6ms | **1.03x** | 16.8ms | 16.8ms | **1.00x** |
| 50K | 31.4ms | 29.9ms | **1.05x** | 33.4ms | 31.5ms | **1.06x** |

†10K results are noise (sub-7ms). Trustworthy signal at 25K+: **1.00–1.06x**.

**v1.5 fairness fix**: The initial results showed 0.73–0.76x at 25K (sqler "faster") which
was suspicious — sqler does `model_validate()` per row, so it can't genuinely be faster.
Root cause: the baseline's `create()` populated the FTS table before the rebuild timer,
leaving extra tombstones in FTS5 shadow tables. After 23 rebuild cycles, these accumulated
and made the baseline ~15% slower. Fixed by making `create()` only create the virtual table.

### What we learned

1. **The two-query pattern was unnecessary.** FTS5's `rowid` maps directly to the source
   table's `_id`, making the JOIN natural and efficient. SQLite handles the rowid lookup
   at the storage engine level without a separate index scan.

2. **`from_ids()` was the hidden cost.** Even for 20 rows, it created a full queryset,
   ran `_batch_resolve()` (scanning for Ref fields that don't exist on typical FTS models),
   and built an intermediate dict for ordering. All unnecessary overhead.

3. **SQL-side sorting beats Python-side sorting.** The old code fetched scores in one query,
   documents in another, then Python-sorted the results. The JOIN returns pre-sorted results.

4. **Be skeptical of "faster than baseline" results.** The initial 0.76x at 25K was a
   baseline fairness bug, not real performance. sqler does `model_validate()` per row —
   it cannot genuinely be faster than raw sqlite doing the same SQL + json.loads(). Root
   cause: the baseline's `create()` left extra FTS5 tombstones that accumulated across
   rebuild cycles. Fix: make `create()` only create the virtual table (matching sqler).

5. **FTS5 tombstones accumulate across DELETE + INSERT cycles.** A fresh FTS5 index is ~2x
   faster to search than one that's been through 23 DELETE + INSERT rebuilds. Any benchmark
   that measures search performance after rebuild iterations must ensure both arms have the
   same tombstone history — even one extra population round creates measurable bias.

---

## Timeline

| Date | Milestone |
|------|-----------|
| Late Feb 2026 | v1.1 benchmark suite — 22 scenarios, first baselines |
| Late Feb 2026 | Memory profiler v2 — tracemalloc per-scenario, multi-run stats |
| Late Feb 2026 | Adversarial audit — 18 fairness issues found in v1.1 |
| Late Feb 2026 | v1.2 rewrite — all 18 issues fixed, matched methodology |
| Mar 1 2026 | Cross-scale validation — 50K→1M, memory+disk, 1,563 measurements |
| Mar 2 2026 | Export optimization — bypass Pydantic hydration (2.8x → 1.0–1.5x) |
| Mar 2 2026 | Fairness corrections — 3 rounds of baseline fixes for exports |
| Mar 2 2026 | Cross-scale export benchmarks — confirmed results hold to 1M |
| Mar 2 2026 | Pydantic/msgspec analysis — model_construct() dead end, msgspec blockers |
| Mar 3 2026 | M-1: FTS baseline fix (4.65x → 1.07x) + queryset.as_dicts() API |
| Mar 3 2026 | v1.3 fairness audit — 3 more asymmetries in FTS search/ranked (issues #20–22) |
| Mar 3 2026 | M-2: any_where fix — json_each(data, path) instead of json_each(json_extract()) (1.5x → 1.0x) |
| Mar 3 2026 | M-3: bulk_upsert() chunked multi-row INSERT (1.9x → 0.91–1.00x) |
| Mar 3 2026 | M-4: search_ranked() single-JOIN optimization (1.5x → 1.00–1.06x at 50K) |
| Mar 3 2026 | v1.5 fairness fix: baseline create() left extra FTS5 tombstones (issue #23) |

## Key Documents

| Document | What it covers |
|----------|---------------|
| `benchmarks/FINDINGS.md` | Definitive results, cross-scale tables, remaining gaps |
| `benchmarks/TODO-SCRUTINY.md` | The 18 fairness issues from the v1.1 audit |
| `benchmarks/MEMPROFILE.md` | Memory profiling methodology journey |
| `docs/HYDRATION-ALTERNATIVES.md` | Pydantic vs msgspec vs raw dicts analysis |
