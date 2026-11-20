"""What a model mixin calls on the model class it is mixed into, for the type checker.

A mixin such as ``SoftDeleteMixin`` or ``TrackedModel`` works on any model base (pydantic,
lite, msgspec, sync or async), so it names none of them. Under ``TYPE_CHECKING`` it derives
from :class:`ModelHost`, which declares the names it uses; at run time it derives from
``object``, so this module is never imported when the package runs.
"""

from typing import Any, ClassVar


class ModelHost:
    """The names a mixin uses from its model; the model's own methods answer at run time.

    A sync model returns the result and an async model returns an awaitable, so the results
    are ``Any``.
    """

    model_fields: ClassVar[dict[str, Any]]
    _id: int | None

    def __init__(self, **data: Any) -> None: ...

    def save(self, *args: Any, **kwargs: Any) -> Any: ...

    @classmethod
    def save_many(cls, instances: Any, *args: Any, **kwargs: Any) -> Any: ...

    def delete(self, *args: Any, **kwargs: Any) -> Any: ...

    @classmethod
    def query(cls, *args: Any, **kwargs: Any) -> Any: ...

    @classmethod
    def using(cls, db: Any, *args: Any, **kwargs: Any) -> Any: ...
