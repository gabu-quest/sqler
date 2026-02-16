"""Test fixtures for FastAPI example tests."""

import os
import tempfile
from typing import Generator

import pytest

from fastapi.testclient import TestClient

# Create single temp database for test session
_test_db_fd, _test_db_path = tempfile.mkstemp(suffix=".db")
os.environ["SQLER_DB_PATH"] = _test_db_path

# Import after setting env var
from examples.fastapi.app import app  # noqa: E402
from examples.fastapi.db import close_db, init_db  # noqa: E402


@pytest.fixture(scope="session")
def client() -> Generator[TestClient, None, None]:
    """Create test client once for entire test session."""
    init_db(_test_db_path)
    with TestClient(app) as c:
        yield c
    close_db()
    os.close(_test_db_fd)
    try:
        os.unlink(_test_db_path)
    except Exception:
        pass
    for suffix in ["-wal", "-shm"]:
        try:
            os.unlink(_test_db_path + suffix)
        except Exception:
            pass


@pytest.fixture
def country_japan(client: TestClient) -> dict:
    """Create Japan country fixture."""
    # Check if already exists first
    list_resp = client.get("/api/locations/countries")
    for c in list_resp.json():
        if c["code"] == "JP":
            return c
    resp = client.post("/api/locations/countries", json={"name": "Japan", "code": "JP"})
    assert resp.status_code == 201, f"Failed to create Japan: {resp.text}"
    return resp.json()


@pytest.fixture
def country_usa(client: TestClient) -> dict:
    """Create USA country fixture."""
    list_resp = client.get("/api/locations/countries")
    for c in list_resp.json():
        if c["code"] == "US":
            return c
    resp = client.post("/api/locations/countries", json={"name": "United States", "code": "US"})
    assert resp.status_code == 201, f"Failed to create USA: {resp.text}"
    return resp.json()


@pytest.fixture
def city_tokyo(client: TestClient, country_japan: dict) -> dict:
    """Create Tokyo city fixture."""
    list_resp = client.get("/api/locations/cities")
    for c in list_resp.json():
        if c["name"] == "Tokyo":
            return c
    resp = client.post(
        "/api/locations/cities",
        json={"name": "Tokyo", "country_id": country_japan["_id"]},
    )
    assert resp.status_code == 201, f"Failed to create Tokyo: {resp.text}"
    return resp.json()


@pytest.fixture
def city_kyoto(client: TestClient, country_japan: dict) -> dict:
    """Create Kyoto city fixture."""
    list_resp = client.get("/api/locations/cities")
    for c in list_resp.json():
        if c["name"] == "Kyoto":
            return c
    resp = client.post(
        "/api/locations/cities",
        json={"name": "Kyoto", "country_id": country_japan["_id"]},
    )
    assert resp.status_code == 201, f"Failed to create Kyoto: {resp.text}"
    return resp.json()


@pytest.fixture
def writer_haruki(client: TestClient, city_kyoto: dict) -> dict:
    """Create writer fixture in Kyoto."""
    list_resp = client.get("/api/writers")
    for w in list_resp.json():
        if w["name"] == "Haruki Tanaka":
            return w
    resp = client.post(
        "/api/writers",
        json={
            "name": "Haruki Tanaka",
            "bio": "Award-winning novelist",
            "city_id": city_kyoto["_id"],
        },
    )
    assert resp.status_code == 201, f"Failed to create Haruki: {resp.text}"
    return resp.json()


@pytest.fixture
def article_silence(client: TestClient, writer_haruki: dict) -> dict:
    """Create article fixture with writer_haruki as author."""
    list_resp = client.get("/api/articles")
    for a in list_resp.json():
        if a["title"] == "The Art of Silence":
            # Ensure article belongs to writer_haruki (might differ in shared DB)
            if a.get("writer") and a["writer"].get("_id") == writer_haruki["_id"]:
                return a
            # Article exists but with different writer - update it
            patch_resp = client.patch(
                f"/api/articles/{a['_id']}",
                json={"writer_id": writer_haruki["_id"]},
            )
            if patch_resp.status_code == 200:
                return patch_resp.json()
    resp = client.post(
        "/api/articles",
        json={
            "title": "The Art of Silence",
            "content": "The concept of ma (negative space) is fundamental to Japanese aesthetics.",
            "tags": ["culture", "japan", "philosophy"],
            "writer_id": writer_haruki["_id"],
        },
    )
    assert resp.status_code == 201, f"Failed to create article: {resp.text}"
    return resp.json()
