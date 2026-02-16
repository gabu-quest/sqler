# FastAPI + SQLer Demo

A full-stack web application demonstrating SQLer with a Vue 3 + Naive UI frontend.

## Features

- **REST API** with SQLer models (User, Address, Order, Article)
- **Vue 3 SPA** with Naive UI component library
- **Optimistic locking** via ETag/If-Match headers
- **Soft delete** with restoration support
- **Audit trails** and change tracking
- **Export/Import** endpoints (JSON, CSV)
- **Metrics** dashboard showing query statistics
- **i18n** support (English/Japanese)

## Quick Start

The Vue SPA is pre-built and committed to git, so you can run immediately:

```bash
# From project root
uv run python -m examples.fastapi.app

# Specify a port
uv run python -m examples.fastapi.app --port 3000

# Auto-find open port if default is busy
uv run python -m examples.fastapi.app --auto-port
```

The server starts at http://localhost:8000 (or the next available port with `--auto-port`)

## Database

The SQLite database file (`sqler_demo.db`) is gitignored and created locally.

### Seed / Reset the Database

To populate the database with sample data (or reset it to a fresh state):

```bash
uv run python -m examples.fastapi.seed
```

This will:
- Delete the existing database (if any)
- Create a fresh database with:
  - 5 countries (Japan, US, UK, Germany, Brazil)
  - 10 cities
  - 8 writers with bios
  - 20 articles with full-text search enabled

### Data Hierarchy

```
Country → City → Writer → Article
   ↓        ↓       ↓         ↓
 5 rows   10 rows  8 rows   20 rows
```

Each level references its parent via SQLer's `RefField`. The Schema page in the UI visualizes these relationships.

## Project Structure

```
fastapi/
├── app.py          # FastAPI application, lifespan, main routes
├── db.py           # Database initialization
├── models.py       # SQLer model definitions
├── schemas.py      # Pydantic request/response schemas
├── errors.py       # Exception handlers
├── utils.py        # Async helpers (threadpool handoff)
├── services/       # Business logic
│   └── users.py
├── routers/        # Feature-specific API routes
│   ├── articles.py # Full-text search demo
│   ├── audit.py    # Audit log viewer
│   ├── db.py       # Database operations
│   ├── export.py   # JSON/CSV export
│   ├── metrics.py  # Query statistics
│   ├── softdelete.py
│   └── tracking.py # Change tracking
├── ui/             # Built Vue SPA (committed to git)
│   └── dist/
└── ui-vue/         # Vue 3 source code
    ├── src/
    │   ├── views/  # Page components
    │   ├── router/ # Vue Router config
    │   ├── i18n/   # Translations
    │   └── design/ # Naive UI theming
    └── package.json
```

## Frontend Development

To modify the Vue frontend:

```bash
cd examples/fastapi/ui-vue

# Install dependencies
npm install

# Dev server with hot reload (proxies API to :8000)
npm run dev

# Build for production (outputs to ../ui/dist/)
npm run build
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/users` | List users with pagination |
| POST | `/users` | Create a user |
| GET | `/users/{id}` | Get user (returns ETag) |
| PATCH | `/users/{id}` | Update user (requires If-Match) |
| DELETE | `/users/{id}` | Delete user |
| GET | `/articles` | Full-text search articles |
| GET | `/audit/{table}` | View audit log |
| GET | `/export/{model}` | Export data (JSON/CSV) |
| GET | `/metrics` | Query statistics |

## Optimistic Locking

The demo uses ETags for conflict detection:

```bash
# Get a user (note the ETag header)
curl -i http://localhost:8000/users/1
# ETag: "v1"

# Update with version check
curl -X PATCH http://localhost:8000/users/1 \
  -H "Content-Type: application/json" \
  -H "If-Match: v1" \
  -d '{"name": "Updated Name"}'
```

If another client modified the user, you'll get `412 Precondition Failed`.

## Async Safety

SQLer is synchronous, but this demo shows the recommended pattern for async frameworks:

```python
from .utils import db_call

# Runs SQLer operations in threadpool
user = await db_call(lambda: User.get(user_id))
```

See `utils.py` for the threadpool handoff implementation.

## Tech Stack

- **Backend**: FastAPI, SQLer, Pydantic
- **Frontend**: Vue 3, Naive UI, Vue Router, Pinia, vue-i18n
- **Build**: Vite, TypeScript
