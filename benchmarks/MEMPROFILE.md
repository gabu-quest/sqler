# Benchmark Memory Profiling

## Journey

### Attempt 1: Full tracemalloc (FAILED)
- Ran `tracemalloc.start()` at process start, never stopped
- Tracked every allocation across all 22 scenarios cumulatively
- Result: process hung at 0% CPU on S1 (bulk insert 100K × 23 pre-created DBs)
- Root cause: tracemalloc bookkeeping grew unbounded, likely thrashing

### Attempt 2: RSS watcher (REJECTED)
- Sampled `/proc/PID/status` VmRSS every 5s from a shell script
- Works but only measures resident set size — includes shared libs, mapped files, kernel buffers
- Tells you "will this OOM?" but not "where is sqler allocating memory?"
- User correctly rejected: RSS doesn't attribute memory to code paths

### Attempt 3: Per-scenario tracemalloc (WORKS)
- Start/stop tracemalloc fresh for each scenario
- Take snapshot before stopping to extract top allocators
- No cumulative overhead — each scenario gets a clean measurement
- Added: top-N allocators with file:line, system memory context, JSON report

### v2: Scrutiny-proof hardening
- Multi-run support (default 3 runs per scenario, report min/max/median)
- Setup vs measurement split (snapshot after `setup()`, before `run()`)
- Snapshot diffs (only show allocations NEW during `run()` phase)
- Overhead estimation (`--estimate-overhead` calibrates tracemalloc inflation via RSS)
- Memory budget enforcement (`--budget MB` exits 1 on violations)
- Per-scenario complexity-class extrapolation (O(1)/O(n)/O(n log n) instead of naive linear)
- Baseline management (`--save-baseline` + `memcompare.py` regression tool)

## Results: 100K rows (large scale, 2026-02-28)

System: 16 GB RAM | Single run (pre-v2)

| Scenario | Peak MB | Time | Complexity |
|----------|---------|------|------------|
| json/any_where | **480.7** | 538.2s | O(n) |
| json/array_contains_isin | 320.7 | 210.2s | O(n) |
| json/nested_field_access | 172.3 | 96.3s | O(n) |
| ops/export_performance | 140.9 | 586.8s | O(n) |
| insert/single_vs_bulk | 122.2 | 82.4s | O(n) |
| advanced/full_text_search | 121.4 | 164.4s | O(n) |
| query/complex_filters | 118.0 | 44.5s | O(n) |
| advanced/query_cache | 117.9 | 38.8s | O(n) |
| insert/doc_size_impact | 110.9 | 209.7s | O(n) |
| query/range_queries | 103.5 | 51.7s | O(n) |
| insert/bulk_insert_scaling | 72.7 | 364.3s | O(n) |
| advanced/connection_pool | 70.5 | 1100.3s | O(1) |
| ops/index_creation | 69.3 | 337.9s | O(n log n) |
| query/equality_filter | 69.0 | 40.4s | O(n) |
| query/top_n | 69.0 | 26.0s | O(n) |
| query/pagination_depth | 69.0 | 40.4s | O(n) |
| query/count_vs_materialize | 69.0 | 26.3s | O(n) |
| ops/aggregates | 69.0 | 53.0s | O(n) |
| insert/model_overhead | 61.1 | 50.8s | O(n) |
| ops/backup_restore | 60.0 | 19.6s | O(n) |
| ops/cold_vs_warm | 48.4 | 96.0s | O(1) |
| advanced/optimistic_locking | 1.9 | 186.0s | O(1) |

**Max peak: 480.7 MB** | Total time: 4,364s (72.7 min)

### Extrapolation (linear upper bound)

| Scale | Rows | Estimated Peak | Fits 16 GB? |
|-------|------|---------------|-------------|
| xlarge | 500K | ~2,403 MB | YES (13.6 GB headroom) |
| xxlarge | 1M | ~4,807 MB | YES (11.2 GB headroom) |

Linear extrapolation overpredicts because O(1) scenarios don't scale and O(n log n) scenarios grow sub-linearly. Actual peaks will be lower.

## Current Design (v2)

```
For each scenario, repeat N runs:
  1. gc.collect() — clear garbage from previous
  2. tracemalloc.start() — fresh tracking
  3. Run setup() — pre-create DBs, load data
  4. take_snapshot() — capture setup memory baseline
  5. get_traced_memory() — read setup peak
  6. Run run() — execute benchmark operations
  7. Run teardown() — cleanup
  8. take_snapshot() — capture final state
  9. get_traced_memory() — read total peak
  10. tracemalloc.stop() — release bookkeeping
  11. Compute: run_peak = total_peak - setup_peak
  12. Diff snapshots: final.compare_to(setup, 'lineno') — new allocations only

Aggregate N runs:
  - peak_mb = median(total_peaks)
  - peak_min/max = min/max(total_peaks)
  - setup_peak_mb = median(setup_peaks)
  - run_peak_mb = median(run_peaks)
  - top_allocators from median run's snapshot
  - diff_allocators from median run's snapshot diff
```

Output: per-scenario median peak (with min/max spread), setup/run split, diff allocators, complexity class, JSON report.

## Known Limitations

| Limitation | Impact | Mitigation |
|-----------|--------|------------|
| tracemalloc overhead (~30%) | Inflates measured peaks | `--estimate-overhead` calibrates via RSS |
| Setup memory conflated | Pre-created DBs inflate total peak | Setup/run split shows separated values |
| Python allocator pools | Small objects reuse pools, not freed to OS | tracemalloc tracks Python heap accurately |
| No cross-scenario leak detection | Can't see memory growing across full suite | Would need cumulative mode (the one that hung) |
| Snapshot diff is additive only | Can't detect reuse of freed memory during run | Shows worst-case new allocations |

## Improvement Status

### Tier 1: Quick Wins
- [x] **Multi-scale curve fitting** — per-scenario complexity class extrapolation (O(1)/O(n)/O(n log n))
- [x] **Setup vs measurement split** — snapshot after `setup()`, compute `run_peak = total - setup`
- [x] **Snapshot diff** — `final.compare_to(setup, 'lineno')` shows only new run-phase allocations

### Tier 2: Serious Rigor
- [x] **Multiple runs** — `--runs N` (default 3), report min/max/median peak
- [x] **Overhead compensation** — `--estimate-overhead` runs without tracemalloc, reports delta
- [ ] **Component attribution** — separate sqler allocations from sqlite3's own memory (C-level)
- [ ] **Leak detection mode** — cumulative tracking across scenarios with periodic snapshots

### Tier 3: Production-Grade
- [x] **Memory budget enforcement** — `--budget MB` flag, exit 1 on violations
- [x] **Regression detection** — `memcompare.py` compares against saved baselines, flags > threshold
- [ ] **CI integration** — run memprofile on every PR, block merge on memory regression
- [ ] **Flamegraph output** — generate memory flamegraphs from snapshot data for visual analysis

## Pre-Created DB Problem

The v1.1 benchmark fix moved DB creation outside timed closures by pre-creating `warmup + iterations` DBs upfront. At large scale:

```
S1 bulk_insert_scaling @ large:
  bulk_sizes = [1K, 10K, 25K, 50K, 100K]
  For 100K: 23 in-memory DBs × 100K rows × ~200B/doc ≈ 460 MB just for one bulk size
```

This is correct for timing (DB creation shouldn't leak into the measurement) but makes memory profiling show inflated numbers. The v2 setup/run split addresses this by showing how much is setup vs actual run.

## Usage

```bash
# Profile at large scale with 3 runs per scenario
uv run --group benchmarks python -u -m benchmarks.memprofile --scale large

# Single run (faster, less rigorous)
uv run --group benchmarks python -u -m benchmarks.memprofile --scale large --runs 1

# Profile with overhead estimation
uv run --group benchmarks python -u -m benchmarks.memprofile --scale large --estimate-overhead

# Enforce memory budget (exit 1 if any scenario > 500 MB)
uv run --group benchmarks python -u -m benchmarks.memprofile --scale large --budget 500

# Save as baseline for regression detection
uv run --group benchmarks python -u -m benchmarks.memprofile --scale large --save-baseline

# Compare against baseline
uv run --group benchmarks python -m benchmarks.memcompare \
    benchmarks/results/baselines/memprofile_large_baseline.json \
    benchmarks/results/memprofile_large_memcheck.json \
    --threshold 10

# Profile specific suite with extra allocator detail
uv run --group benchmarks python -u -m benchmarks.memprofile --scale xlarge --suite insert --top 20
```
