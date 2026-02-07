# Roadmap: sqler Benchmark Suite

## Milestones

### M1: Infrastructure 🔄 ← current
- Core modules: base, timer, config, system, runner
- CLI entry point (`uv run python -m benchmarks`)
- One dummy scenario proving pipeline end-to-end
- Project config updates (.gitignore, pyproject.toml)

### M2: Data Generators + Insert Suite ⬚
- generators/documents.py — 5 profiles (tiny→huge)
- generators/models.py — benchmark model definitions
- suite_insert.py — scenarios 1-4

### M3: Query + JSON Suites ⬚
- suite_query.py — scenarios 5-10
- suite_json.py — scenarios 11-13

### M4: Advanced + Ops Suites ⬚
- suite_advanced.py — scenarios 14-17
- suite_ops.py — scenarios 18-22

### M5: Plotting Engine + Report ⬚
- plotting/theme.py — dark theme + colorblind-safe palette
- plotting/charts.py — scaling lines, comparison bars, heatmaps
- plotting/report.py — markdown report generator
- CLI plot command

### M6: Blog Post Rewrite ⬚
- Rewrite with real data, honest gaps section
- Replace fake graphs with generated charts
