"""Articles router with full-text search and writer attribution.

Demonstrates: SearchableMixin, FTSIndex, search_ranked, highlights, AuditLogMixin
日本語: 全文検索、ライター紐付け、監査ログ
"""

from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field

from fastapi import APIRouter, HTTPException, Query, status

from ..models import Article, Writer
from ..utils import db_call

router = APIRouter(prefix="/api/articles", tags=["Articles (FTS)"])


# =============================================================================
# Schemas
# =============================================================================


class ArticleCreate(BaseModel):
    title: str
    content: str
    tags: list[str] = []
    writer_id: Optional[int] = None


class ArticlePatch(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[list[str]] = None
    writer_id: Optional[int] = None


class ArticleOut(BaseModel):
    """Article output schema with proper field aliasing for Pydantic v2."""

    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(alias="_id")
    version: int = Field(alias="_version")
    title: str
    content: str
    tags: list[str]
    writer: Optional[dict] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None


class AuditLogEntry(BaseModel):
    timestamp: str
    action: str
    changes: Optional[dict] = None


# =============================================================================
# CRUD endpoints
# =============================================================================


@router.post("", response_model=ArticleOut, status_code=status.HTTP_201_CREATED)
async def create_article(payload: ArticleCreate):
    """Create a new article (auto-indexed for FTS).

    日本語: 新しい記事を作成（FTS用に自動インデックス）。
    """

    def _create():
        article = Article(
            title=payload.title,
            content=payload.content,
            tags=payload.tags,
        )

        if payload.writer_id:
            writer = Writer.from_id(payload.writer_id)
            if not writer:
                raise HTTPException(status_code=404, detail="Writer not found")
            article.set_writer(writer)

        article.save()
        return article

    article = await db_call(_create)
    return _article_to_out(article)


@router.get("", response_model=list[ArticleOut])
async def list_articles(
    writer_id: Annotated[Optional[int], Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """List articles with optional writer filter.

    日本語: 記事一覧（ライターフィルタオプション）。
    """
    from sqler.query import SQLerField as F

    def _list():
        q = Article.query()
        if writer_id:
            q = q.filter(F("writer._id") == writer_id)
        return q.limit(limit).offset(offset).all()

    articles = await db_call(_list)
    return [_article_to_out(a) for a in articles]


@router.get("/{article_id}", response_model=ArticleOut)
async def get_article(article_id: int):
    """Get article by ID.

    日本語: IDで記事を取得。
    """
    article = await db_call(lambda: Article.from_id(article_id))
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return _article_to_out(article)


@router.patch("/{article_id}", response_model=ArticleOut)
async def patch_article(article_id: int, patch: ArticlePatch):
    """Update article fields.

    日本語: 記事のフィールドを更新。
    """

    def _patch():
        article = Article.from_id(article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")

        data = patch.model_dump(exclude_unset=True)

        # Handle writer_id separately
        if "writer_id" in data:
            writer_id = data.pop("writer_id")
            if writer_id is None:
                article.writer = None
            else:
                writer = Writer.from_id(writer_id)
                if not writer:
                    raise HTTPException(status_code=404, detail="Writer not found")
                article.set_writer(writer)

        for key, value in data.items():
            setattr(article, key, value)

        article.save()
        return article

    article = await db_call(_patch)
    return _article_to_out(article)


@router.delete("/{article_id}")
async def delete_article(article_id: int):
    """Delete an article.

    日本語: 記事を削除。
    """

    def _delete():
        article = Article.from_id(article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        article.delete()

    await db_call(_delete)
    return {"success": True}


# =============================================================================
# Audit log endpoint
# =============================================================================


@router.get("/{article_id}/audit-log", response_model=list[AuditLogEntry])
async def get_article_audit_log(article_id: int):
    """Get audit log for an article.

    日本語: 記事の監査ログを取得。
    """

    def _get_log():
        article = Article.from_id(article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")

        log = article.get_audit_log()
        return [
            {
                "timestamp": entry.timestamp.isoformat(),
                "action": entry.action,
                "changes": entry.changes,
            }
            for entry in log
        ]

    return await db_call(_get_log)


# =============================================================================
# Full-text search endpoints
# =============================================================================


@router.get("/search/query")
async def search_articles(
    q: Annotated[str, Query(min_length=1, description="Search query")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    offset: Annotated[int, Query(ge=0)] = 0,
    highlight: Annotated[bool, Query()] = True,
):
    """Full-text search with optional highlights.

    日本語: ハイライト付き全文検索。
    """

    def _search():
        if highlight:
            results = Article.search_ranked(q, limit=limit, offset=offset)
            return [
                {
                    "article": _article_to_out(r.model),
                    "score": r.score,
                    "highlights": r.highlights,
                }
                for r in results
            ]
        else:
            results = Article.search(q, limit=limit, offset=offset)
            return [{"article": _article_to_out(a), "score": None, "highlights": None} for a in results]

    return await db_call(_search)


@router.get("/search/count")
async def search_count(
    q: Annotated[str, Query(min_length=1, description="Search query")],
):
    """Count search results.

    日本語: 検索結果の件数。
    """
    count = await db_call(lambda: Article.search_count(q))
    return {"query": q, "count": count}


# =============================================================================
# FTS management endpoints
# =============================================================================


@router.post("/fts/rebuild")
async def rebuild_fts_index():
    """Rebuild the FTS index from scratch.

    日本語: FTSインデックスを再構築。
    """
    await db_call(lambda: Article.rebuild_search_index())
    return {"success": True, "message": "FTS index rebuilt"}


@router.get("/fts/stats")
async def fts_stats():
    """Get FTS index statistics.

    日本語: FTSインデックスの統計。
    """
    from sqler.fts import FTSIndex

    def _stats():
        fts = FTSIndex(Article, fields=["title", "content"])
        stats = fts.stats()
        return {
            "table_name": stats.table_name,
            "indexed_rows": stats.indexed_rows,
            "total_tokens": stats.total_tokens,
            "fields": stats.fields,
        }

    return await db_call(_stats)


# =============================================================================
# Helpers
# =============================================================================


def _article_to_out(article: Article) -> dict:
    """Convert Article model to output dict."""
    return {
        "_id": article._id,
        "_version": getattr(article, "_version", 0),
        "title": article.title,
        "content": article.content,
        "tags": article.tags,
        "writer": article.writer,
        "created_at": article.created_at.isoformat() if article.created_at else None,
        "updated_at": article.updated_at.isoformat() if article.updated_at else None,
        "created_by": getattr(article, "created_by", None),
        "updated_by": getattr(article, "updated_by", None),
    }
