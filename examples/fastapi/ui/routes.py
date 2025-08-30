from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..models import Address, Order, User
from ..services.users import query_users
from ..utils import db_call as _db_call
from ..utils import etag as _etag

router = APIRouter(prefix="/ui", tags=["UI"])

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

Sort = Annotated[Literal["id", "name", "age"], Query()]
Dir = Annotated[Literal["asc", "desc"], Query()]


def _pack_user(u: User) -> dict:
    return u.model_dump() | {"_id": u._id, "_version": getattr(u, "_version", 0)}


@router.get("/", response_class=HTMLResponse)
async def ui_home(request: Request):
    return templates.TemplateResponse("home.html", {"request": request})


@router.get("/users", response_class=HTMLResponse)
async def ui_users(
    request: Request,
    min_age: int | None = Query(default=None, ge=0),
    city: str | None = Query(default=None),
    q: str | None = Query(default=None, description="substring match on name"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: Sort = "age",
    dir: Dir = "asc",
    include: str | None = None,
):
    ctx = {
        "request": request,
        "params": {
            "min_age": min_age,
            "city": city,
            "q": q,
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "dir": dir,
            "include": include,
        },
    }
    return templates.TemplateResponse("users/index.html", ctx)


@router.get("/users/partial/table", response_class=HTMLResponse)
async def ui_users_table(
    request: Request,
    min_age: int | None = Query(default=None, ge=0),
    city: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    sort: Sort = "age",
    dir: Dir = "asc",
    include: str | None = None,
):
    users = await _db_call(lambda: query_users(min_age, city, q, limit, offset, sort, dir, include))
    users = [_pack_user(u) for u in users]
    return templates.TemplateResponse(
        "users/_table.html",
        {
            "request": request,
            "users": users,
            "params": {
                "min_age": min_age,
                "city": city,
                "q": q,
                "limit": limit,
                "offset": offset,
                "sort": sort,
                "dir": dir,
                "include": include,
            },
        },
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def ui_user_detail(request: Request, user_id: int, include: str | None = "address,orders"):
    def _load():
        u = User.from_id(user_id)
        if not u:
            return None
        inc = set((include or "").split(","))
        if u.address and isinstance(u.address, dict) and "address" in inc:
            addr = Address.from_id(int(u.address.get("_id")))
            if addr:
                u.address = _pack_user(addr)  # type: ignore[assignment]
        if "orders" in inc:
            resolved: list[dict] = []
            for ref in u.orders or []:
                oid = int(ref.get("_id"))
                o = Order.from_id(oid)
                if o:
                    resolved.append(_pack_user(o))
            u.orders = resolved  # type: ignore[assignment]
        return u

    u = await _db_call(_load)
    if not u:
        raise HTTPException(status_code=404)
    etag = _etag(u._id, getattr(u, "_version", 0))
    return templates.TemplateResponse(
        "users/detail.html",
        {"request": request, "user": _pack_user(u), "etag": etag, "include": include},
        headers={"ETag": etag},
    )


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def ui_user_edit(request: Request, user_id: int):
    def _load():
        u = User.from_id(user_id)
        if not u:
            return None, None
        etag = _etag(u._id, getattr(u, "_version", 0))
        addrs = Address.query().order_by("_id", False).limit(100).all()
        return u, etag, addrs

    res = await _db_call(_load)
    if not res or res[0] is None:
        raise HTTPException(status_code=404)
    u, etag, addresses = res
    return templates.TemplateResponse(
        "users/_edit_modal.html",
        {
            "request": request,
            "user": _pack_user(u),
            "etag": etag,
            "addresses": [_pack_user(a) for a in addresses],
        },
    )


@router.post("/users", response_class=HTMLResponse)
async def ui_user_create(
    request: Request,
    name: Annotated[str, Form(...)],
    age: Annotated[int, Form(...)],
    address_id: Annotated[int | None, Form()] = None,
):
    def _create():
        u = User(name=name, age=int(age))
        u.save()
        if address_id:
            addr = Address.from_id(int(address_id))
            if addr:
                u.set_address(addr)
                u.save()

    await _db_call(_create)
    return await ui_users_table(request)


@router.post("/users/{user_id}", response_class=HTMLResponse)
async def ui_user_update(
    request: Request,
    user_id: int,
    if_match: Annotated[str, Form(...)],
    name: Annotated[str | None, Form()] = None,
    age: Annotated[int | None, Form()] = None,
    address_id: Annotated[int | None, Form()] = None,
):
    def _update():
        u = User.from_id(user_id)
        if not u:
            return None, None
        current_etag = _etag(u._id, getattr(u, "_version", 0))
        if if_match != current_etag:
            return "precondition", None
        if name is not None:
            u.name = name
        if age is not None:
            u.age = int(age)
        if address_id is not None:
            addr = Address.from_id(int(address_id))
            if addr:
                u.set_address(addr)
        u.save()
        return u, _etag(u._id, getattr(u, "_version", 0))

    res = await _db_call(_update)
    if res is None:
        raise HTTPException(status_code=404)
    if res[0] == "precondition":
        return templates.TemplateResponse(
            "partials/_toast.html",
            {"request": request, "kind": "error", "msg": "Precondition failed. Reload and retry."},
            status_code=412,
        )
    u, etag = res
    return templates.TemplateResponse(
        "users/detail.html",
        {"request": request, "user": _pack_user(u), "etag": etag, "include": "address,orders"},
        headers={"ETag": etag},
    )


@router.get("/healthz")
async def ui_healthz():
    return {"status": "ok"}

