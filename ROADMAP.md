# Roadmap: sqler Benchmark Suite

## Milestones

### M1: Infrastructure ✅
- Core modules: base, timer, config, system, runner
- CLI entry point (`uv run python -m benchmarks`)
- Project config updates (.gitignore, pyproject.toml)

### M2: Data Generators + Insert Suite ✅
- generators/documents.py — 5 profiles (tiny→huge)
- generators/models.py — benchmark model definitions
- suite_insert.py — scenarios 1-4

### M3: Query + JSON Suites ✅
- suite_query.py — scenarios 5-10
- suite_json.py — scenarios 11-13

### M4: Advanced + Ops Suites ✅
- suite_advanced.py — scenarios 14-17
- suite_ops.py — scenarios 18-22

### M5: Plotting Engine + Report ✅
- plotting/theme.py — dark theme + Okabe-Ito colorblind-safe palette
- plotting/charts.py — scaling lines, comparison bars, heatmaps, throughput charts
- plotting/report.py — markdown report generator with summary tables + chart refs
- CLI plot command — generates 22 charts (SVG + PNG) and REPORT.md

### M6: Blog Post Rewrite 🔄 ← current
- Rewrite with real data, honest gaps section
- Replace fake graphs with generated charts

## Results

- **22 scenarios** across 5 suites (insert, query, json, advanced, ops)
- **112 measurements** at small scale (~40s total runtime)
- **22 charts** generated: scaling lines with p95 bands, comparison bars, heatmaps, throughput bars
- All scenarios use only sqler public APIs — no raw SQL, no fakes
