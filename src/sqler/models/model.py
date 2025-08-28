from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Optional, Type, TypeVar

from pydantic import BaseModel, PrivateAttr

from sqler import registry
from sqler.db.sqler_db import SQLerDB
from sqler.models.queryset import SQLerQuerySet
from sqler.query import SQLerExpression

if TYPE_CHECKING:
    from . import BrokenRef  # for type checking

TModel = TypeVar("TModel", bound="SQLerModel")


def _default_table_name(name: str) -> str:
    base = name.lower()
    if not base.endswith("s"):
        base = base + "s"
    # avoid SQL reserved words like 'as'
    if base in {"as"}:
        base = base + "_tbl"
    return base


class SQLerModel(BaseModel):
    """Pydantic-based model with persistence helpers for SQLerDB.

    Define subclasses to model your domain. Bind the class to a database via
    :meth:`set_db`, optionally overriding the table name. Instances persist as
    JSON (excluding the private ``_id`` attribute) into a table with schema
    ``(_id INTEGER PRIMARY KEY AUTOINCREMENT, data JSON NOT NULL)``.
    """

    # internal id stored outside the JSON blob
    _id: Optional[int] = PrivateAttr(default=None)
    # snapshot of last-loaded document (for merge strategies in perf mode)
    _snapshot: Optional[dict] = PrivateAttr(default=None)

    # class-bound db + table metadata
    _db: ClassVar[Optional[SQLerDB]] = None
    _table: ClassVar[Optional[str]] = None

    # ----- class config -----
    model_config = {
        "extra": "ignore",
        "frozen": False,
    }

    # ----- class methods -----
    @classmethod
    def set_db(cls: Type[TModel], db: SQLerDB, table: Optional[str] = None) -> None:
        """Bind this model class to a database and table.

        Args:
            db: Database instance to use for persistence.
            table: Optional table name. Defaults to lowercase plural of the
                class name (e.g., ``User`` → ``users``).
        """
        cls._db = db
        cls._table = table or _default_table_name(cls.__name__)
        cls._db._ensure_table(cls._table)
        registry.register(cls._table, cls)

    # ergonomic relation field builder
    @classmethod
    def ref(cls, name: str):
        """Return a model-aware field builder for a related field name.

        Usage: User.ref("address").field("city") == "Kyoto"
        """
        from .model_field import SQLerModelField

        class _RefBuilder:
            def __init__(self, model_cls, base: str):
                self.model_cls = model_cls
                self.path = [base]

            def field(self, *parts: str) -> SQLerModelField:
                return SQLerModelField(self.model_cls, self.path + list(parts))

            def any(self) -> "_RefAnyBuilder":
                return _RefAnyBuilder(self.model_cls, self.path)

        class _RefAnyBuilder(_RefBuilder):
            def field(self, *parts: str) -> SQLerModelField:
                return SQLerModelField(self.model_cls, self.path + list(parts), array_any=True)

        return _RefBuilder(cls, name)

    @classmethod
    def _require_binding(cls) -> tuple[SQLerDB, str]:
        """Return the bound DB and table or raise if unbound.

        Raises:
            RuntimeError: If :meth:`set_db` has not been called.
        """
        if cls._db is None or cls._table is None:
            raise RuntimeError("Model is not bound. Call set_db(db, table?) first.")
        return cls._db, cls._table

    @classmethod
    def from_id(cls: Type[TModel], id_: int) -> Optional[TModel]:
        """Hydrate an instance by ``_id``.

        Args:
            id_: Row id to load.

        Returns:
            Model instance when found, else ``None``.
        """
        db, table = cls._require_binding()
        doc = db.find_document(table, id_)
        if doc is None:
            return None
        doc = cls._resolve_relations(doc)
        inst = cls.model_validate(doc)  # type: ignore[call-arg]
        # attach db id stored outside the json payload
        inst._id = doc.get("_id")
        return inst  # type: ignore[return-value]

    @classmethod
    def query(cls: Type[TModel]) -> SQLerQuerySet[TModel]:
        """Return a queryset for chaining and execution."""
        db, table = cls._require_binding()
        q = db.query(table)
        return SQLerQuerySet[TModel](cls, q)

    @classmethod
    def filter(cls: Type[TModel], expression: SQLerExpression) -> SQLerQuerySet[TModel]:
        """Shorthand for ``cls.query().filter(expression)``."""
        return cls.query().filter(expression)

    @classmethod
    def add_index(
        cls,
        field: str,
        *,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
    ) -> None:
        """Create an index on a JSON field via the model class.

        Args:
            field: Dotted JSON path or literal column.
            unique: Enforce uniqueness.
            name: Optional index name.
            where: Optional partial-index WHERE clause.
        """
        db, table = cls._require_binding()
        db.create_index(table, field, unique=unique, name=name, where=where)

    @classmethod
    def ensure_index(
        cls,
        field: str,
        *,
        unique: bool = False,
        name: Optional[str] = None,
        where: Optional[str] = None,
    ) -> None:
        """Ensure an index on a JSON path or literal column exists (idempotent)."""
        cls.add_index(field, unique=unique, name=name, where=where)

    # ----- instance methods -----
    def save(self: TModel) -> TModel:
        """Insert or update this instance and update ``_id``.

        Returns:
            self: The same instance (for chaining).
        """
        cls = self.__class__
        db, table = cls._require_binding()
        payload = self._dump_with_relations()
        new_id = db.upsert_document(table, self._id, payload)
        self._id = new_id
        return self

    def delete(self) -> None:
        """Delete this instance by ``_id`` and unset it.

        Deprecated: prefer delete(on_delete=...) to control integrity behavior.
        """
        self.delete_with_policy()

    def delete_with_policy(self, *, on_delete: str = "restrict") -> None:
        """Delete this instance with a specified integrity policy.

        Args:
            on_delete: One of "restrict", "set_null", or "cascade".
        """
        cls = self.__class__
        db, table = cls._require_binding()
        if self._id is None:
            raise ValueError("Cannot delete unsaved model (missing _id)")
        target = (table, int(self._id))
        if on_delete not in {"restrict", "set_null", "cascade"}:
            raise ValueError("on_delete must be 'restrict','set_null', or 'cascade'")

        # find referrers
        referrers = self._find_referrers(db, table, int(self._id))
        if on_delete == "restrict" and referrers:
            from . import ReferentialIntegrityError as _RefErr

            raise _RefErr(
                f"Cannot delete {table}:{self._id}; referenced by {len(referrers)} row(s)"
            )
        if on_delete == "set_null":
            self._set_null_referrers(db, table, int(self._id), referrers)
        elif on_delete == "cascade":
            visited: set[tuple[str, int]] = {target}
            self._cascade_delete(db, referrers, visited)

        db.delete_document(table, self._id)
        self._id = None

    def refresh(self: TModel) -> TModel:
        """Reload this instance's fields from the database.

        Raises:
            ValueError: If the instance has not been saved.
            LookupError: If the row no longer exists.

        Returns:
            self: The same instance (for chaining).
        """
        cls = self.__class__
        db, table = cls._require_binding()
        if self._id is None:
            raise ValueError("Cannot refresh unsaved model (missing _id)")
        doc = db.find_document(table, self._id)
        if doc is None:
            raise LookupError(f"Row {self._id} not found for refresh")
        doc = cls._resolve_relations(doc)
        fresh = cls.model_validate(doc)  # type: ignore[call-arg]
        # update fields in-place (excluding _id which is also present)
        for fname in self.__class__.model_fields:
            if fname == "_id":
                continue
            setattr(self, fname, getattr(fresh, fname))
        # set db id explicitly
        self._id = doc.get("_id")
        return self

    # ----- ref integrity utils -----
    @classmethod
    def _find_referrers(
        cls, db: SQLerDB, target_table: str, target_id: int
    ) -> list[tuple[str, int, dict]]:
        candidates: list[tuple[str, int, dict]] = []
        like1 = f'%"_table":"{target_table}"%'
        like2 = f'%"_id":{target_id}%'
        for table in registry.tables().keys():
            # skip tables not present in this DB
            exists = db.adapter.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?;", [table]
            ).fetchone()
            if not exists:
                continue
            cur = db.adapter.execute(
                f"SELECT _id, data FROM {table} WHERE data LIKE ? AND data LIKE ?;",
                [like1, like2],
            )
            rows = cur.fetchall()
            import json

            for _id, data_json in rows:
                try:
                    data = json.loads(data_json)
                except Exception:
                    continue
                paths = cls._find_ref_paths(data, target_table, target_id)
                if paths:
                    candidates.append((table, int(_id), {"paths": paths}))
        return candidates

    @classmethod
    def _find_ref_paths(cls, data: dict, target_table: str, target_id: int) -> list[str]:
        paths: list[str] = []

        def walk(value, path: str):
            if isinstance(value, dict):
                if value.get("_table") == target_table and int(value.get("_id", -1)) == target_id:
                    paths.append(path or "$")
                for k, v in value.items():
                    walk(v, f"{path}.{k}" if path else k)
            elif isinstance(value, list):
                for i, v in enumerate(value):
                    walk(v, f"{path}[{i}]")

        walk(data, "")
        return paths

    @classmethod
    def _set_null_referrers(
        cls, db: SQLerDB, target_table: str, target_id: int, referrers: list[tuple[str, int, dict]]
    ) -> None:
        import json

        for table, row_id, meta in referrers:
            cur = db.adapter.execute(f"SELECT _id, data FROM {table} WHERE _id = ?;", [row_id])
            row = cur.fetchone()
            if not row:
                continue
            obj = json.loads(row[1])

            def replace(value):
                if (
                    isinstance(value, dict)
                    and value.get("_table") == target_table
                    and int(value.get("_id", -1)) == target_id
                ):
                    return None
                if isinstance(value, dict):
                    return {k: replace(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [replace(v) for v in value]
                return value

            new_obj = replace(obj)
            payload = json.dumps(new_obj)
            db.adapter.execute(
                f"UPDATE {table} SET data = json(?) WHERE _id = ?;", [payload, row_id]
            )
            db.adapter.commit()

    @classmethod
    def _cascade_delete(
        cls, db: SQLerDB, referrers: list[tuple[str, int, dict]], visited: set[tuple[str, int]]
    ) -> None:
        for table, row_id, _ in referrers:
            key = (table, row_id)
            if key in visited:
                continue
            visited.add(key)
            # recurse
            kid_refs = cls._find_referrers(db, table, row_id)
            cls._cascade_delete(db, kid_refs, visited)
            db.delete_document(table, row_id)

    # ----- reference validation -----
    @classmethod
    def validate_references(cls):
        db, _ = cls._require_binding()
        from . import BrokenRef as _BrokenRef

        broken: list[_BrokenRef] = []
        import json

        for table in registry.tables().keys():
            exists = db.adapter.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?;", [table]
            ).fetchone()
            if not exists:
                continue
            cur = db.adapter.execute(f"SELECT _id, data FROM {table};")
            for _id, data_json in cur.fetchall():
                try:
                    data = json.loads(data_json)
                except Exception:
                    continue

                def walk(value, path: str):
                    if isinstance(value, dict) and "_table" in value and "_id" in value:
                        t = value.get("_table")
                        i = int(value.get("_id", -1))
                        # check existence
                        cur2 = db.adapter.execute(f"SELECT 1 FROM {t} WHERE _id = ?;", [i])
                        if not cur2.fetchone():
                            broken.append(
                                BrokenRef(
                                    table=table,
                                    row_id=int(_id),
                                    path=path or "$",
                                    target_table=t,
                                    target_id=i,
                                )
                            )
                        return
                    if isinstance(value, dict):
                        for k, v in value.items():
                            walk(v, f"{path}.{k}" if path else k)
                    elif isinstance(value, list):
                        for idx, v in enumerate(value):
                            walk(v, f"{path}[{idx}]")

                walk(data, "")

        return broken

    # ----- relationship encoding/decoding -----
    @classmethod
    def _is_ref_dict(cls, value: object) -> bool:
        return isinstance(value, dict) and "_table" in value and "_id" in value

    @classmethod
    def _resolve_relations(cls, data: dict) -> dict:
        def decode(value: object):
            if isinstance(value, dict):
                if cls._is_ref_dict(value):
                    table = value.get("_table")
                    rid = value.get("_id")
                    mdl = registry.resolve(table) if isinstance(table, str) else None
                    if mdl is not None and hasattr(mdl, "from_id"):
                        try:
                            return mdl.from_id(rid)
                        except Exception:
                            return value
                return {k: decode(v) for k, v in value.items()}
            if isinstance(value, list):
                return [decode(v) for v in value]
            return value

        return {k: decode(v) for k, v in data.items()}

    def _dump_with_relations(self) -> dict:
        visited: set[tuple[str, int]] = set()

        def encode(value: object):
            from sqler.models.model import SQLerModel
            from sqler.models.ref import as_ref

            if isinstance(value, SQLerModel):
                # avoid recursion: require saved child
                if value._id is None:
                    raise ValueError("Related model must be saved before saving parent")
                if (value.__class__._table, int(value._id)) not in visited:  # type: ignore[attr-defined]
                    visited.add((value.__class__._table, int(value._id)))  # type: ignore[attr-defined]
                return as_ref(value)
            # already a ref dict: validate minimally
            if isinstance(value, dict) and "_table" in value and "_id" in value:
                return {"_table": value["_table"], "_id": value["_id"]}
            if isinstance(value, list):
                return [encode(v) for v in value]
            if isinstance(value, dict):
                return {k: encode(v) for k, v in value.items()}
            if isinstance(value, BaseModel):
                return value.model_dump()
            return value

        payload: dict = {}
        for name in self.__class__.model_fields:
            if name == "_id":
                continue
            payload[name] = encode(getattr(self, name))
        return payload
