from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .db import close_db, init_db
from .errors import install_exception_handlers
from .routers import (
    articles_router,
    db_router,
    locations_router,
    writers_router,
)

"""
FastAPI demo using SQLer safely from async routes (threadpool handoff).

English: Lifespan startup/shutdown, ETag/If-Match, and WAL-friendly patterns.
日本語: lifespan での起動/終了、ETag/If-Match、WAL に配慮した実装例。
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup the demo database.

    日本語: デモ用 DB の初期化とクリーンアップを行います。
    """
    init_db(os.getenv("SQLER_DB_PATH"))
    yield
    close_db()


app = FastAPI(
    title="SQLer FastAPI Demo",
    version="1.0.0",
    summary="JSON-first micro-ORM on SQLite with WAL + optimistic locking",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """Attach a simple process-time header to every response.

    日本語: 各レスポンスに処理時間ヘッダーを付与します。
    """
    start = time.perf_counter()
    resp: Response = await call_next(request)
    resp.headers["X-Process-Time"] = f"{(time.perf_counter() - start):.6f}s"
    return resp


install_exception_handlers(app)

# UI directory for Vue SPA
UI_ROOT = Path(__file__).resolve().parent / "ui"


# Include API routers
app.include_router(db_router)
app.include_router(locations_router)
app.include_router(writers_router)
app.include_router(articles_router)


# Vue SPA serving (pre-built dist is committed to git)
VUE_DIST = UI_ROOT / "dist"
if VUE_DIST.exists():
    # Serve static assets from Vue build
    app.mount("/assets", StaticFiles(directory=str(VUE_DIST / "assets")), name="vue-assets")

    # SPA fallback: serve index.html for all non-API routes
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve Vue SPA for client-side routing.

        日本語: クライアントサイドルーティング用にVue SPAを配信。
        """
        from fastapi.responses import FileResponse

        # Don't intercept API routes
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")

        index_path = VUE_DIST / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        raise HTTPException(status_code=404, detail="SPA not built")


if __name__ == "__main__":
    import argparse
    import socket

    import uvicorn

    def find_open_port(start: int = 8000, max_attempts: int = 100) -> int:
        """Find an open port starting from `start`."""
        for port in range(start, start + max_attempts):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("localhost", port)) != 0:
                    return port
        raise RuntimeError(f"No open port found in range {start}-{start + max_attempts}")

    parser = argparse.ArgumentParser(description="SQLer FastAPI Demo")
    parser.add_argument("-p", "--port", type=int, default=8000, help="Port to run on (default: 8000)")
    parser.add_argument("--auto-port", action="store_true", help="Auto-find open port if default is busy")
    args = parser.parse_args()

    port = args.port
    if args.auto_port:
        port = find_open_port(args.port)

    print(f"Starting server at http://localhost:{port}")
    uvicorn.run("examples.fastapi.app:app", host="0.0.0.0", port=port, reload=True)
