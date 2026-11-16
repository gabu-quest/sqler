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

## Layout

`src/sqler/` (most things come as a sync class and an async twin; a change to one needs the
same change in the other, and `tests/test_public_names.py` fails when an async class lacks a
public name its sync twin has):

- `adapter/`: the SQLite adapters, `synchronous.py` (`SQLiteAdapter`) and `asynchronous.py`
  (`AsyncSQLiteAdapter`, with its connection pool), and the interfaces in `abstract.py`.
- `db/`: `SQLerDB` (`sqler_db.py`) and `AsyncSQLerDB` (`async_db.py`), the document and table
  calls on top of an adapter. What both share is in files by subject: `_table_memory.py` (what a
  database object remembers of a table), `_guard.py` (the guard a statement carries against a
  changed table), `_rows.py` (the column list and the row-to-document rule for a read by id),
  `_writes.py` (the write of one row, the version check, a write of some fields), `_promotion.py`
  (the statements that make a promoted column, move values into it and record it),
  `_raw_read.py` (`execute_sql`: which statements it takes and how its rows become results),
  `_statements.py` (the indexes, a delete by id, the checks of an ensure).
- `models/`: the model bases (pydantic ones here), the querysets, the mixins (`mixins.py`),
  references and delete policies (`_refs.py`, `integrity.py`), optimistic-lock retry
  (`save_retry.py`, `utils.py`).
  - `models/lite/`: the dataclass models, no pydantic needed.
  - `models/msgspec/`: the msgspec model.
- `query/`: `SQLerQuery` (`query.py`), `AsyncSQLerQuery` (`async_query.py`), `SQLerField`/`F`
  (`field.py`) and `SQLerExpression` (`expression.py`).
- Top-level modules: `cache.py` (query cache), `export.py` (CSV, JSON and JSONL import and
  export), `fts.py` (FTS5 search), `migrations.py`, `ops.py` (backup, health, vacuum,
  checkpoint), `logging.py` and `metrics.py`, `tracking.py` (change tracking), `registry.py`
  (table to class), `exceptions.py`, `_stored.py` (the stored form of a value), `utils.py`.
- `__init__.py` holds `__all__`; `docs/API.md` must name every name in it.

## Model matrix

| Backend | Plain, sync | Safe, sync | Plain, async | Safe, async |
|---------|-------------|------------|--------------|-------------|
| pydantic | `SQLerModel` | `SQLerSafeModel` | `AsyncSQLerModel` | `AsyncSQLerSafeModel` |
| lite | `SQLerLiteModel` | `SQLerLiteSafeModel` | `AsyncSQLerLiteModel` | `AsyncSQLerLiteSafeModel` |
| msgspec | `SQLerMsgspecModel` | | | |

A safe model checks and bumps a row version on every save. `SQLerLiteModelBase` and
`SQLerMsgspecModelBase` are the shared parents of the lite and msgspec models.

## Running things

- Whole suite (what CI runs): `uv run pytest -q -m "not perf"`. About 13,900 tests; it takes many
  minutes. `pytest.ini` already adds `-m "not perf"` and sets `testpaths` to `tests` and
  `examples/fastapi/tests`.
- On a slow disk most of a run is commits to the file databases the tests make in the temp
  folder; `TMPDIR=/dev/shm uv run pytest -q` (Linux) keeps them in memory and more than halves it.
- One file: `uv run pytest tests/<file>`; one test: `uv run pytest tests/<file>::<test>`.
- Lint: `uv run ruff check .` (CI runs it before the tests).
- Type check: `uv run mypy` (its settings are `[tool.mypy]` in `pyproject.toml`).
- Performance tests (marker `perf`, opt-in): `uv run pytest -m perf`.

## Test folders

- `tests/`: one file per feature or bug (`test_*.py`), plus shared helpers (`conftest.py`,
  `exception_points.py`, `pairwise.py`).
- `tests/sync/`: tests of the sync classes (adapter, db, models, queries, relationships).
- `tests/async/`: tests of the async classes.
- `tests/lite/`: lite models.
- `tests/msgspec_/`: msgspec models.
- `tests/perf/`: benchmarks marked `perf`.
- `examples/fastapi/tests/`: tests of the FastAPI example app.
