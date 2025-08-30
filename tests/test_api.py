import httpx
import pytest
from examples.fastapi.app import app
from examples.fastapi.db import get_db


@pytest.mark.anyio
async def test_fastapi_etag_and_paging_and_hydration():
    async with httpx.AsyncClient(app=app, base_url="http://test") as c:
        # seed: two addresses
        a1 = (await c.post("/addresses", json={"city": "Otsu", "country": "JP"})).json()
        a2 = (await c.post("/addresses", json={"city": "Kyoto", "country": "JP"})).json()

        # two users, map to addresses
        u1 = (
            await c.post(
                "/users",
                json={"name": "Gabe", "age": 33, "address_id": a1["_id"]},
            )
        ).json()
        u2 = (
            await c.post(
                "/users",
                json={"name": "Ada", "age": 36, "address_id": a2["_id"]},
            )
        ).json()

        # one order and attach to Ada
        o = (await c.post("/orders", json={"total": 42.0, "note": "gift"})).json()
        ok = await c.post(f"/users/{u2['_id']}/orders/{o['_id']}")
        assert ok.status_code == 200 and ok.json()["ok"] is True

        # ETag 304
        r1 = await c.get(f"/users/{u1['_id']}")
        assert r1.status_code == 200
        etag = r1.headers["etag"]
        r2 = await c.get(f"/users/{u1['_id']}", headers={"If-None-Match": etag})
        assert r2.status_code == 304
        assert "etag" in r2.headers

        # PATCH precondition 412
        bad = await c.patch(
            f"/users/{u1['_id']}",
            headers={"If-Match": '"0-0"'},
            json={"age": 34},
        )
        assert bad.status_code == 412

        # PATCH with correct If-Match 200
        rget = await c.get(f"/users/{u1['_id']}")
        good = await c.patch(
            f"/users/{u1['_id']}",
            headers={"If-Match": rget.headers["etag"]},
            json={"age": 34},
        )
        assert good.status_code == 200

        # StaleVersion -> 409 (bump version directly)
        db = get_db()
        db.adapter.execute("UPDATE users SET _version = _version + 1 WHERE _id = ?;", [u2["_id"]])
        db.adapter.commit()
        stale = await c.patch(
            f"/users/{u2['_id']}",
            headers={"If-Match": (await c.get(f"/users/{u2['_id']}")).headers["etag"]},
            json={"age": 37},
        )
        assert stale.status_code == 409

        # Filters
        got = (await c.get("/users", params={"city": "Otsu"})).json()
        assert all(u.get("address", {}).get("city") == "Otsu" for u in got)
        got = (await c.get("/users", params={"min_age": 34})).json()
        assert all(u["age"] >= 34 for u in got)
        got = (await c.get("/users", params={"q": "Ad"})).json()
        assert any(u["name"] == "Ada" for u in got)

        # Paging + sort
        page = (
            await c.get("/users", params={"sort": "name", "dir": "asc", "limit": 1, "offset": 1})
        ).json()
        assert len(page) == 1

        # include hydration
        hydrated = (
            await c.get("/users", params={"include": "address,orders", "sort": "id"})
        ).json()
        assert all("_id" in u.get("address", {}) or u.get("address") is None for u in hydrated)
        assert any(len(u.get("orders", [])) >= 1 for u in hydrated)
