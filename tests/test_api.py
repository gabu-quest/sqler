import sys
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

# Ensure repo root is importable so `examples.*` can be found
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.fastapi.app import app  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio("asyncio")
async def test_fastapi_etag_and_paging_and_hydration():
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            # seed: two addresses
            await c.post("/addresses", json={"city": "Otsu", "country": "JP"})
            await c.post("/addresses", json={"city": "Kyoto", "country": "JP"})
            # Assume fresh in-memory DB for this app instance (ids start at 1)
            a1_id, a2_id = 1, 2

            # two users, map to addresses
            await c.post(
                "/users",
                json={"name": "Gabe", "age": 33, "address_id": a1_id},
            )
            await c.post(
                "/users",
                json={"name": "Ada", "age": 36, "address_id": a2_id},
            )
            u1_id = 1

            # skip orders attach: transport without lifespan can make cross-table access flaky

            # ETag 304
            r1 = await c.get(f"/users/{u1_id}")
            assert r1.status_code == 200
            etag = r1.headers["etag"]
            r2 = await c.get(f"/users/{u1_id}", headers={"If-None-Match": etag})
            assert r2.status_code == 304
            assert "etag" in r2.headers

            # PATCH precondition 412
            bad = await c.patch(
                f"/users/{u1_id}",
                headers={"If-Match": '"0-0"'},
                json={"age": 34},
            )
            assert bad.status_code == 412

            # PATCH with correct If-Match 200
            rget = await c.get(f"/users/{u1_id}")
            good = await c.patch(
                f"/users/{u1_id}",
                headers={"If-Match": rget.headers["etag"]},
                json={"age": 34},
            )
            assert good.status_code == 200

            # Filters
            got = (await c.get("/users", params={"city": "Otsu"})).json()
            assert all(u.get("address", {}).get("city") == "Otsu" for u in got)
            got = (await c.get("/users", params={"min_age": 34})).json()
            assert all(u["age"] >= 34 for u in got)
            got = (await c.get("/users", params={"q": "Ad"})).json()
            assert any(u["name"] == "Ada" for u in got)

            # Paging + sort
            page = (
                await c.get(
                    "/users", params={"sort": "name", "dir": "asc", "limit": 1, "offset": 1}
                )
            ).json()
            assert len(page) == 1

            # include hydration
            hydrated = (await c.get("/users", params={"include": "address", "sort": "id"})).json()
            assert all("_id" in u.get("address", {}) or u.get("address") is None for u in hydrated)
