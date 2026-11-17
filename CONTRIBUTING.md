# Contributing to SQLer

Thank you for your interest in contributing to SQLer! This document provides guidelines for contributing to the project.

## Development Setup

### Prerequisites
- Python 3.12 or higher
- [uv](https://github.com/astral-sh/uv) for dependency management

### Getting Started

1. **Fork and clone the repository**
   ```bash
   git clone https://github.com/YOUR-USERNAME/sqler.git
   cd sqler
   ```

2. **Install dependencies**
   ```bash
   uv sync --dev
   ```

3. **Run tests**
   ```bash
   uv run pytest
   ```

4. **Run linter**
   ```bash
   uv run ruff check .
   uv run ruff format .
   ```

## Commit Message Guidelines

We follow the [Conventional Commits](https://www.conventionalcommits.org/) specification for commit messages. This leads to **more readable messages** and enables **automatic changelog generation**.

### Format

Each commit message consists of a **header**, an optional **body**, and an optional **footer**:

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Type

Must be one of the following:

- **feat**: A new feature
- **fix**: A bug fix
- **docs**: Documentation only changes
- **style**: Changes that do not affect the meaning of the code (white-space, formatting, etc)
- **refactor**: A code change that neither fixes a bug nor adds a feature
- **perf**: A code change that improves performance
- **test**: Adding missing tests or correcting existing tests
- **chore**: Changes to the build process or auxiliary tools and libraries

### Scope (Optional)

The scope specifies the place of the commit change:
- `db` - Database layer
- `models` - Model classes
- `query` - Query builder
- `adapter` - Database adapters
- `ci` - CI/CD configuration
- `docs` - Documentation

### Subject

The subject contains a succinct description of the change:
- Use the imperative, present tense: "change" not "changed" nor "changes"
- Don't capitalize the first letter
- No period (.) at the end
- Limit to 72 characters or less

### Examples

#### Good commit messages:

```
feat(query): add support for OR conditions in query builder

Allow users to combine multiple conditions with OR logic using the
| operator between SQLerExpression instances.

Closes #42
```

```
fix(models): prevent duplicate registry entries on model reload

When reloading modules in development, models were being registered
multiple times. This adds a check to prevent duplicate entries.
```

```
docs: update README with async query examples

Add comprehensive examples showing how to use async queries with
AsyncSQLerModel, including error handling patterns.
```

```
refactor: extract referential integrity code to separate module

Move _find_referrers, _set_null_referrers, _cascade_delete, and
validate_references to models/integrity.py for better code organization.
Reduces model.py from 474 to 300 lines.
```

```
perf(registry): add LRU caching for table name resolution

Cache the results of resolve() and get_allowed_tables() to avoid
repeated dictionary lookups during referential integrity scans.
```

#### Bad commit messages:

❌ `Fixed bug` - Too vague, no context
❌ `Updated code.` - What was updated? Why?
❌ `WIP` - Work in progress commits should be squashed before merging
❌ `fix: Fixed the thing that was broken.` - Redundant wording
❌ `feat: Added new feature for users to be able to do X` - Too verbose in subject

### Body (Optional but Recommended)

The body should include:
- **Motivation** for the change
- **Contrast** with previous behavior
- **Implementation** details if not obvious

Use the body to explain **what** and **why** vs. **how**.

### Footer (Optional)

The footer should contain:
- **Breaking Changes**: Start with `BREAKING CHANGE:` followed by a description
- **Issue References**: Reference GitHub issues that this commit closes/fixes

```
BREAKING CHANGE: rename SQLerField.in() to SQLerField.isin()

The old method name conflicted with Python's `in` keyword.

Closes #123
Fixes #456
```

## Pull Request Process

1. **Create a feature branch**
   ```bash
   git checkout -b feat/your-feature-name
   # or
   git checkout -b fix/your-bug-fix
   ```

2. **Make your changes**
   - Write tests for new features
   - Update documentation if needed
   - Follow the existing code style

3. **Run tests and linting**
   ```bash
   uv run pytest
   uv run ruff check .
   uv run ruff format --check .
   ```

4. **Commit your changes** following the commit message guidelines above

5. **Push to your fork**
   ```bash
   git push origin feat/your-feature-name
   ```

6. **Create a Pull Request**
   - Provide a clear description of the changes
   - Reference any related issues
   - Ensure CI passes

## Code Style

- Follow PEP 8
- Use type hints (PEP 585 style for Python 3.12+: `list`, `dict`, `tuple` instead of `List`, `Dict`, `Tuple`)
- Use descriptive variable names
- Add docstrings to public functions and classes
- Keep functions focused and small
- Prefer explicit over implicit

## Testing

- Write tests for all new features
- Maintain or improve code coverage
- Test both sync and async variants when applicable
- Include edge cases and error conditions

### Running Tests

```bash
# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_models.py

# Run with coverage
uv run pytest --cov=sqler --cov-report=html

# Run only non-performance tests (default in CI)
uv run pytest -m "not perf"
```

### The soak (concurrent writers for minutes or hours)

`tests/soak/` holds a soak that is not part of the default test run. It runs writers at the
same time for as long as you ask and then checks every row of the database against what the
writers were told they had written:

```bash
uv run --frozen python tests/soak/soak.py --minutes 2 --seed 7 --storage file
```

- Workloads (`--workload NAME` runs one, and may be given more than once): `threads` (8
  threads on one `SQLerDB`), `tasks` (8 asyncio tasks on one `AsyncSQLerDB`), `procs2` and
  `procs4` (2 or 4 processes on one file, sync and async database objects mixed). By default
  every workload the storage allows runs, one after another, sharing `--minutes`.
- `--storage memory` runs the thread and task workloads on an in-memory database;
  `--storage file` runs them all on a file in a new folder under `--dir` (default: the
  system's temp folder). The harness only opens a database it made, and refuses a folder
  another run is using.
- `--kill` (process workloads, with `--minutes`) kills a writer process with SIGKILL 20 times
  in the run and starts another in its place; `kills=` in the summary is the number made, and
  a run that made none fails. A killed worker's unfinished call must be stored wholly or not
  at all: every call the soak makes is one unit by the package's documents (one statement, or
  one transaction: `docs/API.md` on delete policies, promotion, the audit log and
  `content_sync`), so the checker accepts no third state. `--precreate` makes every table
  before the writers start. `--ops N` runs N operations a worker instead of minutes; `--only
  a,b` runs only those operations, to narrow a finding.
- The operations are the ordinary ones over every model base: saves, partial saves,
  `save_many()`, deletes under each delete policy, queryset `update()` and `delete()`,
  references, promoted columns (one promoted while writers run), both search index settings,
  the audit log, transactions that commit and that roll back, a shared counter incremented
  with the retry helper, and `copy_to()` into an archive table.
- Every few seconds (`--checkpoint`) the writers pause and the checker runs; it runs once
  more at the end. It fails on a missing, changed, deleted or unknown row, a wrong version,
  a lost counter increment, a missing or extra audit row, a dangling reference, a promoted
  value in the wrong place, an index over two fields that is not on where the table stores
  them, a search index that misses a row or finds a deleted one, anything
  left by a rolled-back write, an archived row in both tables or in neither, and an unclean
  `PRAGMA integrity_check` or `foreign_key_check`.
- Exit status 0 with a `SOAK PASS` line; 1 with `SOAK FAIL`, after at most 40 lines of
  detail (the first 20 violations of each check, then the first 20 errors in full: worker,
  operation, seed); every error a
  caller should not see is counted by type in the summary line, also those of a killed
  process (each worker writes its errors beside its journal before it goes on). 2 for a
  refused invocation. Ctrl-C stops the writers and still runs the checker.

To re-run a failing seed, run the same command with the same `--seed`, `--workload` and
`--ops` or `--minutes`: each worker started at the beginning gets the same operations again,
though the interleaving between workers (and so the row ids) differs from run to run. Two
things do not repeat: a worker started in place of a killed one has a new name and so other
operations, and in a timed run without `--precreate` the promotion comes at a time, not at an
operation number. `tests/test_soak_checker.py`
(in the default run) proves that the checker sees each kind of damage.

## Documentation

- Update README.md if you change the public API
- Add docstrings following NumPy style
- Include code examples in docstrings where helpful
- Update CHANGELOG.md (if we add one) for significant changes

## Questions?

Feel free to open an issue for:
- Questions about contributing
- Clarification on guidelines
- Discussion of new features

Thank you for contributing to SQLer! 🎉
