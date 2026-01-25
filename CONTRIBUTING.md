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
