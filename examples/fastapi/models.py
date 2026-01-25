from typing import List, Optional

from pydantic import Field
from sqler.fts import SearchableMixin
from sqler.models import SQLerSafeModel
from sqler.models.mixins import (
    AuditLogMixin,
    AuditMixin,
    TimestampMixin,
)
from sqler.models.ref import as_ref


# =============================================================================
# LOCATION MODELS (Country -> City hierarchy)
# =============================================================================


class Country(SQLerSafeModel):
    """Country model for location hierarchy.

    日本語: 国モデル（ロケーション階層用）。
    """

    _table = "countries"

    name: str
    code: str  # ISO 3166-1 alpha-2 code (e.g., "JP", "US")


class City(SQLerSafeModel):
    """City model with reference to Country.

    日本語: 国への参照を持つ都市モデル。
    """

    _table = "cities"

    name: str
    country: Optional[dict] = None  # RefField to Country

    def set_country(self, country: Country):
        """Attach a saved Country reference to this city.

        日本語: 保存済み Country への参照を都市に設定します。
        """
        if country._id is None:
            raise ValueError("Save country first")
        self.country = as_ref(country)


# =============================================================================
# WRITER MODEL (with AuditLogMixin for change tracking)
# =============================================================================


class Writer(AuditLogMixin, TimestampMixin, SQLerSafeModel):
    """Writer model with location reference and audit logging.

    Features: AuditLogMixin (change history), TimestampMixin, RefField to City
    日本語: ロケーション参照と監査ログ付きライターモデル。
    """

    _table = "writers"

    name: str
    bio: str = ""
    city: Optional[dict] = None  # RefField to City

    def set_city(self, city: City):
        """Attach a saved City reference to this writer.

        日本語: 保存済み City への参照をライターに設定します。
        """
        if city._id is None:
            raise ValueError("Save city first")
        self.city = as_ref(city)


# =============================================================================
# ARTICLE MODEL (FTS + Writer reference + AuditLogMixin)
# =============================================================================


class Article(SearchableMixin, AuditLogMixin, TimestampMixin, AuditMixin, SQLerSafeModel):
    """Article model with FTS, writer reference, and audit logging.

    Features: SearchableMixin (FTS5), AuditLogMixin, TimestampMixin, RefField to Writer
    日本語: 全文検索、ライター参照、監査ログ付き記事モデル。
    """

    _table = "articles"

    title: str
    content: str
    tags: List[str] = Field(default_factory=list)
    writer: Optional[dict] = None  # RefField to Writer

    def set_writer(self, writer: Writer):
        """Attach a saved Writer reference to this article.

        日本語: 保存済み Writer への参照を記事に設定します。
        """
        if writer._id is None:
            raise ValueError("Save writer first")
        self.writer = as_ref(writer)

    class FTS:
        """FTS5 configuration for Article search."""

        fields = ["title", "content"]
        tokenizer = "porter unicode61"
