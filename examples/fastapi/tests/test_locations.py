"""Tests for Countries and Cities API.

Tests RefField relationships and dependency checks.
"""

from fastapi.testclient import TestClient


class TestCountries:
    """Country CRUD tests."""

    def test_create_country(self, client: TestClient):
        """Create a country with name and code."""
        resp = client.post("/api/locations/countries", json={"name": "Germany", "code": "DE"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Germany"
        assert data["code"] == "DE"
        assert "_id" in data
        assert "_version" in data

    def test_list_countries(self, client: TestClient, country_japan: dict, country_usa: dict):
        """List all countries."""
        resp = client.get("/api/locations/countries")
        assert resp.status_code == 200
        countries = resp.json()
        assert len(countries) >= 2
        names = [c["name"] for c in countries]
        assert "Japan" in names
        assert "United States" in names

    def test_get_country_by_id(self, client: TestClient, country_japan: dict):
        """Get a specific country by ID."""
        resp = client.get(f"/api/locations/countries/{country_japan['_id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Japan"
        assert data["code"] == "JP"

    def test_get_country_not_found(self, client: TestClient):
        """Get non-existent country returns 404."""
        resp = client.get("/api/locations/countries/99999")
        assert resp.status_code == 404

    def test_delete_country_no_dependencies(self, client: TestClient):
        """Delete a country with no cities."""
        # Create a country with no cities
        create_resp = client.post("/api/locations/countries", json={"name": "Brazil", "code": "BR"})
        assert create_resp.status_code == 201
        country_id = create_resp.json()["_id"]

        # Delete should succeed
        del_resp = client.delete(f"/api/locations/countries/{country_id}")
        assert del_resp.status_code == 200

        # Verify deleted
        get_resp = client.get(f"/api/locations/countries/{country_id}")
        assert get_resp.status_code == 404

    def test_delete_country_with_cities_fails(self, client: TestClient, city_tokyo: dict):
        """Cannot delete a country that has cities (dependency check)."""
        country_id = city_tokyo["country"]["_id"]
        resp = client.delete(f"/api/locations/countries/{country_id}")
        assert resp.status_code == 409
        # Accept either custom message or SQLer's built-in message
        detail = resp.json()["detail"].lower()
        assert "cities depend" in detail or "referenced by" in detail


class TestCities:
    """City CRUD tests."""

    def test_create_city(self, client: TestClient, country_japan: dict):
        """Create a city with country reference."""
        resp = client.post(
            "/api/locations/cities",
            json={"name": "Osaka", "country_id": country_japan["_id"]},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Osaka"
        assert data["country"]["_id"] == country_japan["_id"]
        assert data["country"]["name"] == "Japan"

    def test_create_city_invalid_country(self, client: TestClient):
        """Create city with non-existent country fails."""
        resp = client.post(
            "/api/locations/cities",
            json={"name": "Nowhere", "country_id": 99999},
        )
        assert resp.status_code == 404
        assert "country not found" in resp.json()["detail"].lower()

    def test_list_cities(self, client: TestClient, city_tokyo: dict, city_kyoto: dict):
        """List all cities."""
        resp = client.get("/api/locations/cities")
        assert resp.status_code == 200
        cities = resp.json()
        assert len(cities) >= 2
        names = [c["name"] for c in cities]
        assert "Tokyo" in names
        assert "Kyoto" in names

    def test_list_cities_by_country(self, client: TestClient, country_japan: dict, city_tokyo: dict, city_kyoto: dict):
        """List cities filtered by country."""
        resp = client.get(f"/api/locations/countries/{country_japan['_id']}/cities")
        assert resp.status_code == 200
        cities = resp.json()
        assert len(cities) >= 2
        # All cities should belong to Japan
        for city in cities:
            assert city["country"]["_id"] == country_japan["_id"]

    def test_get_city_by_id(self, client: TestClient, city_tokyo: dict):
        """Get a specific city by ID."""
        resp = client.get(f"/api/locations/cities/{city_tokyo['_id']}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Tokyo"
        assert data["country"]["name"] == "Japan"

    def test_delete_city_no_dependencies(self, client: TestClient, country_usa: dict):
        """Delete a city with no writers."""
        # Create a city with no writers
        create_resp = client.post(
            "/api/locations/cities",
            json={"name": "Austin", "country_id": country_usa["_id"]},
        )
        assert create_resp.status_code == 201
        city_id = create_resp.json()["_id"]

        # Delete should succeed
        del_resp = client.delete(f"/api/locations/cities/{city_id}")
        assert del_resp.status_code == 200

    def test_delete_city_with_writers_fails(self, client: TestClient, writer_haruki: dict):
        """Cannot delete a city that has writers (dependency check)."""
        city_id = writer_haruki["city"]["_id"]
        resp = client.delete(f"/api/locations/cities/{city_id}")
        assert resp.status_code == 409
        # Accept either custom message or SQLer's built-in message
        detail = resp.json()["detail"].lower()
        assert "writers depend" in detail or "referenced by" in detail
