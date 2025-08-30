from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from sqler.models import StaleVersionError

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .db import close_db, init_db
from .errors import install_exception_handlers
from .models import Address, Order, User
from .schemas import (
    AddressCreate,
    AddressOut,
    OkOut,
    OrderCreate,
    OrderOut,
    UserCreate,
    UserOut,
    UserPatch,
)
from .services.users import query_users
from .utils import db_call as _db_call
from .utils import etag as _etag

"""
FastAPI demo using SQLer safely from async routes (threadpool handoff).

English: Lifespan startup/shutdown, ETag/If-Match, and WAL-friendly patterns.
日本語: lifespan での起動/終了、ETag/If-Match、WAL に配慮した実装例。
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and cleanup the demo database.

    日本語: デモ用 DB の初期化とクリーンアップを行います。
    """
    init_db(os.getenv("SQLER_DB_PATH"))
    yield
    close_db()


app = FastAPI(
    title="SQLer FastAPI Demo",
    version="1.0.0",
    summary="JSON-first micro-ORM on SQLite with WAL + optimistic locking",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """Attach a simple process-time header to every response.

    日本語: 各レスポンスに処理時間ヘッダーを付与します。
    """
    start = time.perf_counter()
    resp: Response = await call_next(request)
    resp.headers["X-Process-Time"] = f"{(time.perf_counter() - start):.6f}s"
    return resp


install_exception_handlers(app)

# UI mounting configuration
UI_ENABLED = os.getenv("UI_ENABLED", "1") not in {"0", "false", "False"}
UI_ROOT = Path(__file__).resolve().parent / "ui"


router_users = APIRouter(prefix="/users", tags=["Users"])
router_addresses = APIRouter(prefix="/addresses", tags=["Addresses"])
router_orders = APIRouter(prefix="/orders", tags=["Orders"])


@router_addresses.post("", response_model=AddressOut, status_code=status.HTTP_201_CREATED)
async def create_address(payload: AddressCreate):
    """Create an address document.

    日本語: 住所ドキュメントを作成します。
    """
    a = await _db_call(lambda: Address(**payload.model_dump()).save())
    return AddressOut.model_validate(
        a.model_dump() | {"_id": a._id, "_version": getattr(a, "_version", 0)}
    )


@router_addresses.get("/{address_id}", response_model=AddressOut)
async def get_address(address_id: int, request: Request, response: Response):
    """Get an address by id with ETag support (304 on If-None-Match).

    日本語: ETag 対応の id 取得（If-None-Match なら 304）。
    """
    a = await _db_call(lambda: Address.from_id(address_id))
    if not a:
        raise HTTPException(status_code=404, detail="address not found")
    etag = _etag(a._id, getattr(a, "_version", 0))
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return AddressOut.model_validate(
        a.model_dump() | {"_id": a._id, "_version": getattr(a, "_version", 0)}
    )


@router_users.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate):
    """Create a user and optionally link an existing address.

    日本語: ユーザを作成し、必要なら既存住所を関連付けます。
    """

    def _create():
        u = User(**payload.model_dump(exclude={"address_id"}))
        if payload.address_id is not None:
            addr = Address.from_id(payload.address_id)
            if not addr:
                raise HTTPException(status_code=404, detail="address not found")
            u.set_address(addr)
        u.save()
        return u

    u = await _db_call(_create)
    return UserOut.model_validate(
        u.model_dump() | {"_id": u._id, "_version": getattr(u, "_version", 0)}
    )


@router_users.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: int, request: Request, response: Response):
    """Get a user by id with ETag support.

    日本語: ETag 対応の id 取得。
    """
    u = await _db_call(lambda: User.from_id(user_id))
    if not u:
        raise HTTPException(status_code=404, detail="user not found")
    etag = _etag(u._id, getattr(u, "_version", 0))
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return UserOut.model_validate(
        u.model_dump() | {"_id": u._id, "_version": getattr(u, "_version", 0)}
    )


@router_users.get("", response_model=list[UserOut])
async def list_users(
    min_age: Annotated[int | None, Query(ge=0)] = None,
    city: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(description="substring match on name")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    sort: Annotated[Literal["id", "name", "age"], Query()] = "age",
    dir: Annotated[Literal["asc", "desc"], Query()] = "asc",
    include: Annotated[str | None, Query(description="comma list: address,orders")] = None,
):
    """List users with filters and pagination.

    日本語: フィルタとページング付きユーザ一覧。
    """

    def _list():
        users = query_users(min_age, city, q, limit, offset, sort, dir, include)
        return [
            UserOut.model_validate(
                u.model_dump() | {"_id": u._id, "_version": getattr(u, "_version", 0)}
            )
            for u in users
        ]

    return await _db_call(_list)


@router_users.patch("/{user_id}", response_model=UserOut)
async def patch_user(user_id: int, patch: UserPatch, request: Request, response: Response):
    """Apply a partial update with If-Match and optimistic locking.

    日本語: If-Match と楽観的ロックで部分更新。
    """

    def _patch():
        u = User.from_id(user_id)
        if not u:
            raise HTTPException(status_code=404, detail="user not found")
        current_etag = _etag(u._id, getattr(u, "_version", 0))
        if (if_match := request.headers.get("if-match")) and if_match != current_etag:
            raise HTTPException(status_code=412, detail="If-Match precondition failed")

        data = patch.model_dump(exclude_unset=True)
        if "address_id" in data:
            if data["address_id"] is None:
                u.address = None
            else:
                addr = Address.from_id(data["address_id"])
                if not addr:
                    raise HTTPException(status_code=404, detail="address not found")
                u.set_address(addr)
            data.pop("address_id")

        for k, v in data.items():
            setattr(u, k, v)

        try:
            u.save()
        except StaleVersionError:
            raise HTTPException(status_code=409, detail="version conflict")
        return u

    u = await _db_call(_patch)
    etag = _etag(u._id, getattr(u, "_version", 0))
    response.headers["ETag"] = etag
    return UserOut.model_validate(
        u.model_dump() | {"_id": u._id, "_version": getattr(u, "_version", 0)}
    )


@router_orders.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def create_order(payload: OrderCreate):
    """Create an order document.

    日本語: 注文ドキュメントを作成します。
    """
    o = await _db_call(lambda: Order(**payload.model_dump()).save())
    return OrderOut.model_validate(
        o.model_dump() | {"_id": o._id, "_version": getattr(o, "_version", 0)}
    )


@router_users.post("/{user_id}/orders/{order_id}", response_model=OkOut)
async def attach_order(user_id: int, order_id: int):
    """Attach an existing order to a user.

    日本語: 既存の注文をユーザに関連付けます。
    """

    def _attach():
        u = User.from_id(user_id)
        o = Order.from_id(order_id)
        if not u or not o:
            raise HTTPException(status_code=404, detail="user or order not found")
        u.add_order(o)
        u.save()
        return {"ok": True}

    return await _db_call(_attach)


app.include_router(router_addresses)
app.include_router(router_users)
app.include_router(router_orders)

if UI_ENABLED:
    STATIC_DIR = UI_ROOT / "static"
    TEMPLATES_DIR = UI_ROOT / "templates"
    if not TEMPLATES_DIR.exists():
        raise RuntimeError(f"UI_ENABLED=1 but templates not found: {TEMPLATES_DIR}")
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    from .ui.routes import router as ui_router

    app.include_router(ui_router)
