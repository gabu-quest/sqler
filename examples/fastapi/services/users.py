from __future__ import annotations

from typing import Optional

from sqler.query import SQLerField as F

from ..models import User


def query_users(
    min_age: Optional[int] = None,
    city: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "age",
    dir: str = "asc",
    include: Optional[str] = None,
):
    qs = User.query()
    if min_age is not None:
        qs = qs.filter(F("age") >= min_age)
    if city:
        qs = qs.filter(User.ref("address").field("city") == city)
    if q:
        qs = qs.filter(F("name").like(f"%{q}%"))

    desc = dir == "desc"
    inc = set((include or "").split(",")) & {"address", "orders"}
    qs = qs.resolve(bool(inc))

    if sort in {"name", "age"}:
        arr = qs.order_by(sort, desc).limit(limit + offset).all()
        items = arr[offset : offset + limit]
    else:
        arr = qs.all()
        arr.sort(key=lambda u: int(u._id or 0), reverse=desc)
        items = arr[offset : offset + limit]

    return items
