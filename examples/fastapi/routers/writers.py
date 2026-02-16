"""Writers router with audit logging.

Demonstrates: AuditLogMixin, RefField to City, dependency checks
日本語: ライター管理、監査ログ、都市参照、依存チェック
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from fastapi import APIRouter, HTTPException, status

from ..models import Article, City, Writer
from ..utils import db_call, hydrate_ref

router = APIRouter(prefix="/api/writers", tags=["Writers"])


# =============================================================================
# Schemas
# =============================================================================


class WriterCreate(BaseModel):
    name: str
    bio: str = ""
    city_id: Optional[int] = None


class WriterPatch(BaseModel):
    name: Optional[str] = None
    bio: Optional[str] = None
    city_id: Optional[int] = None


class WriterOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(alias="_id")
    version: int = Field(alias="_version")
    name: str
    bio: str
    city: Optional[dict] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AuditLogEntry(BaseModel):
    timestamp: str
    action: str
    changes: Optional[dict] = None


# =============================================================================
# CRUD endpoints
# =============================================================================


@router.get("", response_model=list[WriterOut])
async def list_writers():
    """List all writers.

    日本語: 全てのライターを一覧表示。
    """

    def _list():
        return Writer.query().all()

    writers = await db_call(_list)
    return [_writer_to_out(w) for w in writers]


@router.post("", response_model=WriterOut, status_code=status.HTTP_201_CREATED)
async def create_writer(payload: WriterCreate):
    """Create a new writer.

    日本語: 新しいライターを作成。
    """

    def _create():
        writer = Writer(name=payload.name, bio=payload.bio)

        if payload.city_id:
            city = City.from_id(payload.city_id)
            if not city:
                raise HTTPException(status_code=404, detail="City not found")
            writer.set_city(city)

        writer.save()
        # Re-fetch to hydrate RefFields
        return Writer.from_id(writer._id)

    writer = await db_call(_create)
    return _writer_to_out(writer)


@router.get("/{writer_id}", response_model=WriterOut)
async def get_writer(writer_id: int):
    """Get writer by ID.

    日本語: IDでライターを取得。
    """
    writer = await db_call(lambda: Writer.from_id(writer_id))
    if not writer:
        raise HTTPException(status_code=404, detail="Writer not found")
    return _writer_to_out(writer)


@router.patch("/{writer_id}", response_model=WriterOut)
async def patch_writer(writer_id: int, patch: WriterPatch):
    """Update writer fields.

    日本語: ライターのフィールドを更新。
    """

    def _patch():
        writer = Writer.from_id(writer_id)
        if not writer:
            raise HTTPException(status_code=404, detail="Writer not found")

        data = patch.model_dump(exclude_unset=True)

        # Handle city_id separately
        if "city_id" in data:
            city_id = data.pop("city_id")
            if city_id is None:
                writer.city = None
            else:
                city = City.from_id(city_id)
                if not city:
                    raise HTTPException(status_code=404, detail="City not found")
                writer.set_city(city)

        for key, value in data.items():
            setattr(writer, key, value)

        writer.save()
        # Re-fetch to hydrate RefFields
        return Writer.from_id(writer._id)

    writer = await db_call(_patch)
    return _writer_to_out(writer)


@router.delete("/{writer_id}")
async def delete_writer(writer_id: int):
    """Delete a writer (fails if articles depend on it).

    日本語: ライターを削除（依存する記事がある場合は失敗）。
    """
    from sqler.exceptions import ReferentialIntegrityError
    from sqler.query import SQLerField as F

    def _delete():
        writer = Writer.from_id(writer_id)
        if not writer:
            raise HTTPException(status_code=404, detail="Writer not found")

        # Check for dependent articles
        dependent_articles = Article.query().filter(F("writer._id") == writer_id).count()
        if dependent_articles > 0:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot delete: {dependent_articles} articles depend on this writer",
            )

        try:
            writer.delete()
        except ReferentialIntegrityError as e:
            raise HTTPException(status_code=409, detail=str(e))

    await db_call(_delete)
    return {"success": True}


# =============================================================================
# Audit log endpoint
# =============================================================================


@router.get("/{writer_id}/audit-log", response_model=list[AuditLogEntry])
async def get_writer_audit_log(writer_id: int):
    """Get audit log for a writer.

    日本語: ライターの監査ログを取得。
    """

    def _get_log():
        writer = Writer.from_id(writer_id)
        if not writer:
            raise HTTPException(status_code=404, detail="Writer not found")

        log = writer.get_audit_log()
        return [
            {
                "timestamp": entry["timestamp"],
                "action": entry["action"],
                "changes": entry.get("changes"),
            }
            for entry in log
        ]

    return await db_call(_get_log)


# =============================================================================
# Writer's articles
# =============================================================================


@router.get("/{writer_id}/articles")
async def get_writer_articles(writer_id: int):
    """Get articles written by this writer.

    日本語: このライターが書いた記事を取得。
    """
    from sqler.query import SQLerField as F

    def _get_articles():
        writer = Writer.from_id(writer_id)
        if not writer:
            raise HTTPException(status_code=404, detail="Writer not found")

        articles = Article.query().filter(F("writer._id") == writer_id).all()
        return [
            {
                "_id": a._id,
                "title": a.title,
                "tags": a.tags,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in articles
        ]

    return await db_call(_get_articles)


# =============================================================================
# Helpers
# =============================================================================


def _writer_to_out(writer: Writer) -> dict:
    """Convert Writer model to output dict with hydrated city."""
    city_data = hydrate_ref(writer.city, City)
    # Also hydrate the city's country if present
    if city_data and city_data.get("country"):
        from ..models import Country

        city_data["country"] = hydrate_ref(city_data["country"], Country)
    return {
        "_id": writer._id,
        "_version": getattr(writer, "_version", 0),
        "name": writer.name,
        "bio": writer.bio,
        "city": city_data,
        "created_at": writer.created_at.isoformat() if writer.created_at else None,
        "updated_at": writer.updated_at.isoformat() if writer.updated_at else None,
    }
