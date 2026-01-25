"""API routers for SQLer FastAPI demo.

Each router demonstrates specific SQLer features.
"""

from .articles import router as articles_router
from .db import router as db_router
from .locations import router as locations_router
from .writers import router as writers_router

__all__ = [
    "articles_router",
    "db_router",
    "locations_router",
    "writers_router",
]
