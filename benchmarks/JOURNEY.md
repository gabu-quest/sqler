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

### Tier 2: Per-row overhead

**Bulk insert (1.87–1.92x, +5.7s at 1M)** — Stable per-row overhead. The baseline uses
`executemany()` (C-level batch). sqler's `bulk_upsert()` has per-row Python overhead
(validation, `_ensure_table()`, query building). Profiling needed to find the top costs.
A "fast path" that skips validation for trusted bulk data could halve the gap.

**any_where (1.47–1.51x, +3.2s at 1M)** — The query logger adds ~30ms per call. Making
it opt-in or lazy would reduce all query-path overhead. SQL compilation caching would help
repeated queries with the same structure.

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

## Key Documents

| Document | What it covers |
|----------|---------------|
| `benchmarks/FINDINGS.md` | Definitive results, cross-scale tables, remaining gaps |
| `benchmarks/TODO-SCRUTINY.md` | The 18 fairness issues from the v1.1 audit |
| `benchmarks/MEMPROFILE.md` | Memory profiling methodology journey |
| `docs/HYDRATION-ALTERNATIVES.md` | Pydantic vs msgspec vs raw dicts analysis |
