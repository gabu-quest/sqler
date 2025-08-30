from typing import List, Optional

from pydantic import Field
from sqler.models import SQLerSafeModel
from sqler.models.ref import as_ref


class Address(SQLerSafeModel):
    """Simple address model persisted as JSON with optimistic versioning.

    日本語: 楽観的バージョン管理付きの住所モデル（JSON 保持）。
    """

    city: str
    country: str


class Order(SQLerSafeModel):
    """Order model with total and optional note.

    日本語: 合計金額とメモを持つ注文モデル。
    """

    total: float
    note: str = ""


class User(SQLerSafeModel):
    """User model referencing Address and Orders via lightweight ref dicts.

    日本語: 参照辞書で Address/Order を関連付けるユーザモデル。
    """

    name: str
    age: int
    # reference to Address and list of references to Orders
    address: Optional[dict] = None
    orders: List[dict] = Field(default_factory=list)

    def set_address(self, addr: Address):
        """Attach a saved Address reference to this user.

        日本語: 保存済み Address への参照をユーザに設定します。
        """
        if addr._id is None:
            raise ValueError("Save address first")
        self.address = as_ref(addr)

    def add_order(self, order: Order):
        """Append a saved Order reference to the user's orders list.

        日本語: 保存済み Order の参照をユーザの注文一覧に追加します。
        """
        if order._id is None:
            raise ValueError("Save order first")
        self.orders.append(as_ref(order))
