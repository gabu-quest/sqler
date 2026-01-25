"""Locations router for Countries and Cities.

Demonstrates: RefField relationships, cascade dependency checks
日本語: 国と都市の管理、参照フィールド、カスケード依存チェック
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from fastapi import APIRouter, HTTPException, status

from ..models import City, Country, Writer
from ..utils import db_call

router = APIRouter(prefix="/api/locations", tags=["Locations"])


# =============================================================================
# Schemas
# =============================================================================


class CountryCreate(BaseModel):
    name: str
    code: str


class CountryOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(alias="_id")
    version: int = Field(alias="_version")
    name: str
    code: str


class CityCreate(BaseModel):
    name: str
    country_id: int


class CityOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(alias="_id")
    version: int = Field(alias="_version")
    name: str
    country: Optional[dict] = None


# =============================================================================
# Country endpoints
# =============================================================================


@router.get("/countries", response_model=list[CountryOut])
async def list_countries():
    """List all countries.

    日本語: 全ての国を一覧表示。
    """

    def _list():
        return Country.query().all()

    countries = await db_call(_list)
    return [_country_to_out(c) for c in countries]


@router.post("/countries", response_model=CountryOut, status_code=status.HTTP_201_CREATED)
async def create_country(payload: CountryCreate):
    """Create a new country.

    日本語: 新しい国を作成。
    """

    def _create():
        country = Country(**payload.model_dump())
        country.save()
        return country

    country = await db_call(_create)
    return _country_to_out(country)


@router.get("/countries/{country_id}", response_model=CountryOut)
async def get_country(country_id: int):
    """Get country by ID.

    日本語: IDで国を取得。
    """
    country = await db_call(lambda: Country.from_id(country_id))
    if not country:
        raise HTTPException(status_code=404, detail="Country not found")
    return _country_to_out(country)


@router.delete("/countries/{country_id}")
async def delete_country(country_id: int):
    """Delete a country (fails if cities depend on it).

    日本語: 国を削除（依存する都市がある場合は失敗）。
    """
    from sqler.query import SQLerField as F

    def _delete():
        country = Country.from_id(country_id)
        if not country:
            raise HTTPException(status_code=404, detail="Country not found")

        # Check for dependent cities
        dependent_cities = City.query().filter(F("country._id") == country_id).count()
        if dependent_cities > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot delete: {dependent_cities} cities depend on this country",
            )

        country.delete()

    await db_call(_delete)
    return {"success": True}


@router.get("/countries/{country_id}/cities", response_model=list[CityOut])
async def list_cities_by_country(country_id: int):
    """List cities in a country.

    日本語: 国内の都市を一覧表示。
    """
    from sqler.query import SQLerField as F

    def _list():
        country = Country.from_id(country_id)
        if not country:
            raise HTTPException(status_code=404, detail="Country not found")
        return City.query().filter(F("country._id") == country_id).all()

    cities = await db_call(_list)
    return [_city_to_out(c) for c in cities]


# =============================================================================
# City endpoints
# =============================================================================


@router.get("/cities", response_model=list[CityOut])
async def list_cities():
    """List all cities.

    日本語: 全ての都市を一覧表示。
    """

    def _list():
        return City.query().all()

    cities = await db_call(_list)
    return [_city_to_out(c) for c in cities]


@router.post("/cities", response_model=CityOut, status_code=status.HTTP_201_CREATED)
async def create_city(payload: CityCreate):
    """Create a new city.

    日本語: 新しい都市を作成。
    """

    def _create():
        country = Country.from_id(payload.country_id)
        if not country:
            raise HTTPException(status_code=404, detail="Country not found")

        city = City(name=payload.name)
        city.set_country(country)
        city.save()
        return city

    city = await db_call(_create)
    return _city_to_out(city)


@router.get("/cities/{city_id}", response_model=CityOut)
async def get_city(city_id: int):
    """Get city by ID.

    日本語: IDで都市を取得。
    """
    city = await db_call(lambda: City.from_id(city_id))
    if not city:
        raise HTTPException(status_code=404, detail="City not found")
    return _city_to_out(city)


@router.delete("/cities/{city_id}")
async def delete_city(city_id: int):
    """Delete a city (fails if writers depend on it).

    日本語: 都市を削除（依存するライターがある場合は失敗）。
    """
    from sqler.query import SQLerField as F

    def _delete():
        city = City.from_id(city_id)
        if not city:
            raise HTTPException(status_code=404, detail="City not found")

        # Check for dependent writers
        dependent_writers = Writer.query().filter(F("city._id") == city_id).count()
        if dependent_writers > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot delete: {dependent_writers} writers depend on this city",
            )

        city.delete()

    await db_call(_delete)
    return {"success": True}


# =============================================================================
# Helpers
# =============================================================================


def _country_to_out(country: Country) -> dict:
    """Convert Country model to output dict."""
    return {
        "_id": country._id,
        "_version": getattr(country, "_version", 0),
        "name": country.name,
        "code": country.code,
    }


def _city_to_out(city: City) -> dict:
    """Convert City model to output dict."""
    return {
        "_id": city._id,
        "_version": getattr(city, "_version", 0),
        "name": city.name,
        "country": city.country,
    }
