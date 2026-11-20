from __future__ import annotations

from typing import Any, List, Sequence, Type, Union

from sqler._stored import stored
from sqler.models._table_names import _model_table_name
from sqler.models.model import SQLerModel
from sqler.query.expression import Related, SQLerExpression
from sqler.query.query import SQLerQuery, related_rows


class SQLerModelField:
    """Model-aware field that compiles relationship-crossing predicates.

    Example:
      SQLerModelField(User, ["address", "city"]) == "Kyoto"
      -> EXISTS (
           SELECT 1 FROM (SELECT _id AS rid FROM addresses rt
                          WHERE json_extract(rt.data, '$.city') = ?) r
           WHERE r.rid = json_extract(data, '$.address._id')
         )

    The condition on the related row is a query on the related table: a query with a
    database behind it lays it out as that database remembers the table (a promoted
    ``city`` is read from its column; ``sqler.query.query.related_rows``), as it lays out
    its own. The related rows give only ``rid``, so ``data`` outside them is the column of
    the row the query reads, in whatever table it reads: the class's own, or another one
    (``using(db, table)``).
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

        # the condition on a related row, as a query on its table writes it; ``_id`` is a column
        is_id_field = rest == ["_id"]
        if rest and not is_id_field:
            where = f"json_extract(data, {_json_path_literal(list(rest))}) {op} ?"
        else:
            where = f"_id {op} ?"
        # its rows as a query on that table with no database behind it reads them; a query
        # with a database behind it lays them out as that database remembers the table
        rows = related_rows(table, SQLerQuery(table, expression=SQLerExpression(where))._where()[0])

        if self.array_any:
            # each entry of the outer row's array of references, joined to its related row
            sql = f"EXISTS (SELECT 1 FROM json_each(data, {first_literal}) a JOIN {rows} r ON r.rid = json_extract(a.value, '$._id'))"
        else:
            sql = f"EXISTS (SELECT 1 FROM {rows} r WHERE r.rid = json_extract(data, {ref_literal}))"
        made = SQLerExpression(sql, [stored(val)])
        made.related = (Related(table, where, rows),)
        return made

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
