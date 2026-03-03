"""SQLer Msgspec — high-performance Struct-based models.

This module provides msgspec-backed alternatives using C-level JSON
decode+validate for maximum hydration speed.

Usage::

    from sqler.models.msgspec import SQLerMsgspecModel, SQLerMsgspecModelBase
    from sqler import SQLerDB

    class User(SQLerMsgspecModel):
        __tablename__ = "users"
        name: str
        email: str

    db = SQLerDB(":memory:")
    User.set_db(db)

    user = User(name="Alice", email="alice@example.com")
    user.save()
"""

from sqler.models._compat import MSGSPEC_AVAILABLE

if MSGSPEC_AVAILABLE:
    from sqler.models.msgspec.base import SQLerMsgspecModelBase
    from sqler.models.msgspec.model import SQLerMsgspecModel

    __all__ = [
        "SQLerMsgspecModelBase",
        "SQLerMsgspecModel",
    ]
else:
    from sqler.models._compat import require_msgspec

    def _msgspec_required_class(name: str):
        class _Stub:
            def __init__(self, *args, **kwargs):
                require_msgspec(f"{name}")

            def __init_subclass__(cls, **kwargs):
                require_msgspec(f"{name}")

        _Stub.__name__ = name
        _Stub.__qualname__ = name
        return _Stub

    SQLerMsgspecModelBase = _msgspec_required_class("SQLerMsgspecModelBase")
    SQLerMsgspecModel = _msgspec_required_class("SQLerMsgspecModel")

    __all__ = [
        "SQLerMsgspecModelBase",
        "SQLerMsgspecModel",
    ]
