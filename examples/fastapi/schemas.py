from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

"""
Pydantic request/response schemas for the FastAPI example.

English: Keep API shapes explicit and OpenAPI-friendly.
日本語: API 形状を明示して OpenAPI に反映します。
"""


class AddressCreate(BaseModel):
    """Payload to create an Address.

    日本語: Address 作成ペイロード。
    """

    city: str = Field(..., examples=["Kyoto"])
    country: str = Field(..., examples=["JP"])


class AddressOut(AddressCreate):
    """Address response with id and version.

    日本語: id とバージョン付き Address レスポンス。
    """

    _id: int
    _version: int


class UserCreate(BaseModel):
    """Payload to create a User; optionally links an Address by id.

    日本語: User 作成（任意で Address を id で関連付け）。
    """

    name: str
    age: int = Field(ge=0)
    address_id: Optional[int] = Field(default=None, description="Existing address id to link")


class UserPatch(BaseModel):
    """Partial update for User (PATCH).

    日本語: User の部分更新（PATCH）。
    """

    name: Optional[str] = None
    age: Optional[int] = Field(default=None, ge=0)
    address_id: Optional[int] = Field(default=None, description="Set to null to unlink")


class UserOut(BaseModel):
    """User response including id/version and resolved refs.

    日本語: id/バージョンと解決済み参照を含む User レスポンス。
    """

    _id: int
    _version: int
    name: str
    age: int
    address: Optional[dict] = None
    orders: list[dict] = []


class OrderCreate(BaseModel):
    """Payload to create an Order.

    日本語: Order 作成ペイロード。
    """

    total: float = Field(ge=0)
    note: str = ""


class OrderOut(OrderCreate):
    """Order response with id/version.

    日本語: id/バージョン付き Order レスポンス。
    """

    _id: int
    _version: int


class OkOut(BaseModel):
    """Generic OK response.

    日本語: 汎用 OK レスポンス。
    """

    ok: bool = True
