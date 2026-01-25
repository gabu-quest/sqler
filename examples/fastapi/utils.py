from __future__ import annotations

from typing import Any

from starlette.concurrency import run_in_threadpool


def etag(obj_id: int, version: int | None) -> str:
    """Build a strong ETag from id and version.

    日本語: id と _version 由来の強い ETag を組み立てる。
    """
    v = 0 if version is None else int(version)
    return f'"{obj_id}-{v}"'


async def db_call(fn, *args: Any, **kwargs: Any):
    """Run a blocking function in the threadpool.

    日本語: ブロッキング処理をスレッドプールで実行する。
    """
    return await run_in_threadpool(fn, *args, **kwargs)
