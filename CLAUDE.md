# sqler

Document-oriented JSON store on SQLite.

## Benchmark Hygiene (MANDATORY)

1. **Always run with `--storage both`** — never report memory-only results. Disk I/O can change ratios.
2. **Every sqler measurement needs a sqlite baseline** — no orphan measurements. If there's no natural baseline, document why.
3. **Both arms must do equivalent work** — matched PRAGMAs, matched SQL, matched serialization.
4. **Run at medium scale minimum** (50K rows) — small scale results are noisy and misleading.
5. **Document known caveats** — every benchmark has weaknesses. State them, don't hide them.

## Follow-up TODO

- [ ] Deprecate `set_db()` — soft deprecation with `warnings.warn()` added; full removal deferred
