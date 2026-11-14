from __future__ import annotations

from typing import Any, List, Sequence, Type, Union

from sqler.models.model import SQLerModel
from sqler.query.expression import SQLerExpression


class SQLerModelField:
    """Model-aware field that compiles relationship-crossing predicates.

    Example:
      SQLerModelField(User, ["address", "city"]) == "Kyoto"
      -> EXISTS (
           SELECT 1 FROM addresses r
           WHERE r._id = json_extract(data, '$.address._id')
             AND json_extract(r.data, '$.city') = ?
         )
    """

    def __init__(
        self, model: Type[SQLerModel], path: Sequence[Union[str, int]], array_any: bool = False
    ):
        self.model = model
        self.path: List[Union[str, int]] = list(path)
        self.array_any = array_any

    def any(self) -> "SQLerModelField":
        """For list-of-refs fields: iterate over the array of refs."""
        return SQLerModelField(self.model, self.path, array_any=True)

    def _build_exists(self, op: str, val: Any) -> SQLerExpression:
        if not self.path:
            raise ValueError("Empty path for model field")
        from sqler.exceptions import InvalidFieldNameError
        from sqler.query.field import _json_path_literal
        from sqler.utils import validate_table_name

        for seg in self.path:
            if isinstance(seg, str) and not seg:
                raise InvalidFieldNameError(
                    "Invalid field name: ''. A key in a model field path cannot be empty.",
                    field=seg,
                )
        first_raw = str(self.path[0])
        if first_raw.startswith("['") and first_raw.endswith("']"):
            first = first_raw[2:-2]
        else:
            first = first_raw
        rest = self.path[1:]
        # the key escape is the owner's: it raises before any SQL exists for a key it cannot carry
        first_literal = _json_path_literal([first])
        ref_literal = _json_path_literal([first, "_id"])
        # related table name is default-plural of field type's class; we rely on registry-set table on model
        # find table by inspecting registry mapping; use child model's _table
        # fall back to pluralized field name if unknown
        try:
            # prefer registry-known names
            from sqler import registry as _reg

            keys = set(_reg.tables().keys())
            if first in keys:
                table = first
            elif f"{first}s" in keys:
                table = f"{first}s"
            elif first.lower().endswith("s"):
                table = first.lower()
            else:
                table = f"{first.lower()}s"
        except Exception:
            table = first.lower() if first.lower().endswith("s") else f"{first.lower()}s"
        # the table name goes into the statement as an identifier: the owner's check
        validate_table_name(table)

        outer_table = validate_table_name(
            getattr(self.model, "_table", None) or self.model.__name__.lower() + "s"
        )

        # Check if querying for _id specifically (it's a column, not in JSON)
        is_id_field = rest == ["_id"]
        rest_literal = _json_path_literal(list(rest)) if rest and not is_id_field else None

        if self.array_any:
            # iterate refs array: json_each over $.<first>, join related on ref._id
            # EXISTS (SELECT 1 FROM json_each(outer.data,'$.first') a JOIN table r ON r._id = json_extract(a.value,'$._id') WHERE json_extract(r.data,'$.rest') OP ?)
            joins = f"json_each({outer_table}.data, {first_literal}) a JOIN {table} r ON r._id = json_extract(a.value, '$._id')"
            if rest_literal is None:
                where_right = f"r._id {op} ?"
            else:
                where_right = f"json_extract(r.data, {rest_literal}) {op} ?"
            sql = f"EXISTS (SELECT 1 FROM {joins} WHERE {where_right})"
        else:
            if rest_literal is None:
                where_right = f"r._id {op} ?"
            else:
                where_right = f"json_extract(r.data, {rest_literal}) {op} ?"
            sql = (
                "EXISTS (SELECT 1 FROM "
                f"{table} r WHERE r._id = json_extract({outer_table}.data, {ref_literal}) AND {where_right})"
            )
        return SQLerExpression(sql, [val])

    def __compare(self, op: str, val: Any) -> SQLerExpression:
        return self._build_exists(op, val)

    def __eq__(self, other: Any) -> SQLerExpression:  # type: ignore[override]
        return self.__compare("=", other)

    def __ne__(self, other: Any) -> SQLerExpression:  # type: ignore[override]
        return self.__compare("!=", other)

    def __gt__(self, other: Any) -> SQLerExpression:
        return self.__compare(">", other)

    def __ge__(self, other: Any) -> SQLerExpression:
        return self.__compare(">=", other)

    def __lt__(self, other: Any) -> SQLerExpression:
        return self.__compare("<", other)

    def __le__(self, other: Any) -> SQLerExpression:
        return self.__compare("<=", other)
