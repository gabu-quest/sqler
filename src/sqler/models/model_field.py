from __future__ import annotations

from typing import Any, List, Sequence, Type, Union

from sqler._stored import stored
from sqler.models._table_names import _model_table_name
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
        table = _related_table(self.model, first, self.array_any)
        outer_table = _model_table_name(self.model)

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
        return SQLerExpression(sql, [stored(val)])

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


def _related_table(model: type, first: str, array_any: bool) -> str:
    """The table the rows a reference field of ``model`` points to are stored in.

    It is the table of the model class the field's annotation admits there
    (``Optional[Team]``, ``Team``; each entry of ``list[Team]`` with ``any()``), from
    the owners of both rules: :func:`typed_fields` and :func:`_model_table_name`.

    A field whose annotation admits no model class there (a plain ``dict``, a key the
    class does not declare) keeps the table its name gives (``team`` -> ``teams``, or a
    table of that name the registry knows), as before. A field that admits classes of
    several tables uses that name's table only when it is one of them.

    Raises:
        ValueError: The field admits classes of several tables and its name gives none of them.
    """
    from sqler.models._refs import typed_fields

    at = typed_fields(model).get(first)
    if at is None:
        admits: tuple = ()
    elif array_any:
        admits = at.item.admits if at.item is not None else ()
    else:
        admits = at.admits
    tables = list(dict.fromkeys(_model_table_name(cls) for cls in admits))
    if len(tables) == 1:
        return tables[0]
    guess = _table_of_field_name(first)
    if tables and guess not in tables:
        raise ValueError(
            f"{model.__name__}.{first}: its annotation admits classes of the tables "
            f"{', '.join(tables)}, so a filter through it cannot name one table"
        )
    return guess


def _table_of_field_name(first: str) -> str:
    """The table a field's name gives: one the registry knows by that name or its plural,
    else the plural of the name."""
    from sqler import registry
    from sqler.utils import validate_table_name

    keys = set(registry.tables().keys())
    if first in keys:
        table = first
    elif f"{first}s" in keys:
        table = f"{first}s"
    elif first.lower().endswith("s"):
        table = first.lower()
    else:
        table = f"{first.lower()}s"
    # the table name goes into the statement as an identifier: the owner's check
    return validate_table_name(table)
