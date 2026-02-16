from __future__ import annotations

from typing import Any, Optional

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


def hydrate_ref(ref: Optional[dict], model_class) -> Optional[dict]:
    """Hydrate a RefField dict to include all model fields.

    RefFields are stored as minimal dicts like {"_id": 1, "_table": "cities"}.
    This function fetches the full model data for display.

    Args:
        ref: The RefField dict or None
        model_class: The model class to hydrate from (e.g., City, Country)

    Returns:
        Full dict with all model fields, or None if ref is None or not found

    日本語: RefFieldの最小辞書を全フィールドを含む辞書に展開します。
    """
    if ref is None:
        return None
    if not isinstance(ref, dict):
        return None
    ref_id = ref.get("_id")
    if ref_id is None:
        return None
    obj = model_class.from_id(ref_id)
    if obj is None:
        return None
    result = obj.model_dump()
    result["_id"] = obj._id
    if hasattr(obj, "_version"):
        result["_version"] = obj._version
    return result
