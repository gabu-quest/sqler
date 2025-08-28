from typing import Any, List, Optional, Tuple, Union

from sqler.query import SQLerExpression


class SQLerField:
    """
    proxy for a json field lets you do: field == x, field > 5, field['a'], field / 'b', field.any(), etc

    examples:
      SQLerField('flag') == True
      # -> JSON_EXTRACT(data, '$.flag') = ?

      SQLerField(['level1','field2']) < 50
      # -> JSON_EXTRACT(data, '$.level1.field2') < ?

      SQLerField('level1') / 'field2' / 'field3' >= 10
      # -> JSON_EXTRACT(data, '$.level1.field2.field3') >= ?

      SQLerField('array1')[3] == 123
      # -> JSON_EXTRACT(data, '$.array1[3]') = ?

      SQLerField('tags').contains('red')
      # -> EXISTS (SELECT 1 FROM json_each(data, '$.tags') WHERE json_each.value = ?)

      SQLerField(['arr']).any()['field'] == 5
      # -> EXISTS (
      #     SELECT 1
      #     FROM json_each(json_extract(data, '$.arr')) AS a
      #     WHERE json_extract(a.value, '$.field') = ?
      #   )

      SQLerField(['level1']).any()['arr2'].any()['val'] > 0
      # -> EXISTS (
      #     SELECT 1
      #     FROM json_each(json_extract(data, '$.level1')) AS a
      #     JOIN json_each(json_extract(a.value, '$.arr2')) AS b
      #     WHERE json_extract(b.value, '$.val') > ?
      #   )

      SQLerField(['outer','a','b','c','val']) == 42
      # -> JSON_EXTRACT(data, '$.outer.a.b.c.val') = ?

      (SQLerField('count') > 1) & (SQLerField('count') < 10)
      # -> (JSON_EXTRACT(data, '$.count') > ?) AND (JSON_EXTRACT(data, '$.count') < ?)
    """

    def __init__(
        self,
        path: Union[str, List[Union[str, int]]],
        alias_stack: Optional[
            List[Union[tuple[str, str], tuple[str, str, Optional[SQLerExpression]]]]
        ] = None,
    ):
        """
        path: a string (single field) or list of keys/indexes (deep/nested)
          ex: 'level1'
          ex: ['level1','arr2',3,'field4'] (for data['level1']['arr2'][3]['field4'])
        alias_stack: stores (alias, array_field) for every .any() in the chain
          ex: [('a','arr1'), ('b','arr2')] for arr1[].arr2[]
        """
        if isinstance(path, str):
            self.path: List[Union[str, int]] = [path]
        else:
            self.path = list(path)
        self.alias_stack: List[
            Union[tuple[str, str], tuple[str, str, Optional[SQLerExpression]]]
        ] = alias_stack or []

    def __repr__(self) -> str:
        return f"SQLerField({self.path!r}, alias_stack={self.alias_stack!r})"

    def _json_path(self) -> str:
        """
        build a sqlite json path string
          ex: ['a', 'b', 1, 'c'] -> '$.a.b[1].c'
        """
        import re

        if not self.path:
            return "$"

        def needs_quoting(s: str) -> bool:
            # quotes if not valid json key
            return not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", s)

        parts = ["$"]
        for segment in self.path:
            if isinstance(segment, int):
                parts.append(f"[{segment}]")
            else:
                if needs_quoting(segment):
                    escaped = segment.replace('"', '\\"')
                    parts.append(f'."{escaped}"')
                else:
                    parts.append(f".{segment}")
        return "".join(parts)

    def any(self) -> "SQLerAnyContext":
        """
        adds a .any() at this level for querying arrays of dicts
        lets you write things like:
          SQLerField(['arr1']).any()['val'] == 10
          # -> EXISTS (
          #     SELECT 1
          #     FROM json_each(json_extract(data, '$.arr1')) AS a
          #     WHERE json_extract(a.value, '$.val') = ?
          #   )
        you can chain:
          SQLerField(['level1']).any()['arr2'].any()['score'] > 50
          # -> EXISTS (
          #     SELECT 1
          #     FROM json_each(json_extract(data, '$.level1')) AS a
          #     JOIN json_each(json_extract(a.value, '$.arr2')) AS b
          #     WHERE json_extract(b.value, '$.score') > ?
          #   )
        """
        alias = chr(ord("a") + len(self.alias_stack))
        field = self.path[-1]
        base = SQLerField(self.path, self.alias_stack + [(alias, field, None)])
        return SQLerAnyContext(base, alias)

    def __getitem__(self, item: Union[str, int]) -> "SQLerField":
        """
        goes one key/index deeper:
          SQLerField(['a'])['b']  -> ['a','b']
          SQLerField(['arr'])[0]  -> ['arr',0]
        """
        return SQLerField(self.path + [item], self.alias_stack)

    def __truediv__(self, other: str) -> "SQLerField":
        """
        alternative to __getitem__, lets you do field / 'b'
        """
        return SQLerField(self.path + [other], self.alias_stack)

    def __compare(self, op: str, val: Any) -> SQLerExpression:
        """
        do a comparison on this field (==, >, etc)
        uses SQLerAnyExpression for any() chains, else direct json_extract
        """
        if self.alias_stack:
            return SQLerAnyExpression(self.path, self.alias_stack, op, val)
        expr = f"JSON_EXTRACT(data, '{self._json_path()}') {op} ?"
        return SQLerExpression(expr, [val])

    def __eq__(self, other: Any) -> SQLerExpression:
        """field == value"""
        return self.__compare("=", other)

    def __ne__(self, other: Any) -> SQLerExpression:
        """field != value"""
        return self.__compare("!=", other)

    def __gt__(self, other: Any) -> SQLerExpression:
        """field > value"""
        return self.__compare(">", other)

    def __ge__(self, other: Any) -> SQLerExpression:
        """field >= value"""
        return self.__compare(">=", other)

    def __lt__(self, other: Any) -> SQLerExpression:
        """field < value"""
        return self.__compare("<", other)

    def __le__(self, other: Any) -> SQLerExpression:
        """field <= value"""
        return self.__compare("<=", other)

    def contains(self, value: Any) -> SQLerExpression:
        """
        check if array at this field contains a value
          SQLerField('tags').contains('red')
          # -> EXISTS (SELECT 1 FROM json_each(data, '$.tags') WHERE json_each.value = ?)
        """
        json_path = self._json_path()
        expr = f"EXISTS (SELECT 1 FROM json_each(data, '{json_path}') WHERE json_each.value = ?)"
        return SQLerExpression(expr, [value])

    def isin(self, values: List[Any]) -> SQLerExpression:
        """
        check if array at this field contains any of the given values
          SQLerField('tags').isin(['red','green'])
          # -> EXISTS (SELECT 1 FROM json_each(data, '$.tags') WHERE json_each.value IN (?,?))
        """
        if not values:
            return SQLerExpression("0", [])
        json_path = self._json_path()
        placeholders = ", ".join("?" for _ in values)
        expr = (
            f"EXISTS (SELECT 1 FROM json_each(data, '{json_path}') "
            f"WHERE json_each.value IN ({placeholders}))"
        )
        return SQLerExpression(expr, values)

    def like(self, pattern: str) -> SQLerExpression:
        """
        pattern matching with LIKE
          SQLerField('field1').like('a%')
          # -> JSON_EXTRACT(data, '$.field1') LIKE ?
        """
        expr = f"JSON_EXTRACT(data, '{self._json_path()}') LIKE ?"
        return SQLerExpression(expr, [pattern])


class SQLerAnyExpression(SQLerExpression):
    """
    builds EXISTS select with JOINs for every .any() in the chain

    examples:
      SQLerField(['arr']).any()['val'] == 1
      # -> EXISTS (
      #     SELECT 1
      #     FROM json_each(json_extract(data, '$.arr')) AS a
      #     WHERE json_extract(a.value, '$.val') = ?
      #   )

      SQLerField(['level1']).any()['arr2'].any()['field3'] > 100
      # -> EXISTS (
      #     SELECT 1
      #     FROM json_each(json_extract(data, '$.level1')) AS a
      #     JOIN json_each(json_extract(a.value, '$.arr2')) AS b
      #     WHERE json_extract(b.value, '$.field3') > ?
      #   )
    """

    def __init__(
        self,
        path: List[Union[str, int]],
        alias_stack: List[Union[tuple[str, str], tuple[str, str, Optional[SQLerExpression]]]],
        op: str,
        val: Any,
    ):
        """
        path: the full chain of keys/indexes (e.g. ['level1','arr2','val'])
        alias_stack: one entry per .any(): (alias, array_field)
          e.g. [('a','level1'), ('b','arr2')]
        op: sql operator, e.g. '=', '>', etc
        val: comparison value
        """
        # array_keys: just the array fields we .any()'d over
        norm: List[tuple[str, str, Optional[SQLerExpression]]] = []
        for entry in alias_stack:
            if len(entry) == 2:  # type: ignore[arg-type]
                a, f = entry  # type: ignore[misc]
                norm.append((a, f, None))
            else:
                a, f, w = entry  # type: ignore[misc]
                norm.append((a, f, w))

        array_keys = [field for _, field, _ in norm]
        aliases = [alias for alias, _, _ in norm]
        last_field = path[-1]

        joins: List[str] = []
        where_clauses: List[str] = []
        where_params: List[Any] = []

        # where does the arrays start in the path?
        first_array_key = array_keys[0]
        idx0 = path.index(first_array_key)
        base = path[:idx0]  # all path before arrays (could be [])
        base_json = "$" + "".join(f".{p}" for p in base + [first_array_key])

        first_alias = aliases[0]
        # first FROM: make a table out of the first array
        joins.append(f"json_each(json_extract(data, '{base_json}')) AS {first_alias}")
        first_where = norm[0][2]
        if first_where is not None:
            wsql, wparams = _scope_expr(first_where, first_alias)
            where_clauses.append(wsql)
            where_params += wparams
        prev_alias = first_alias

        # handle more .any()s: join each nested array
        for alias, array_key, wexpr in norm[1:]:
            # e.g. JOIN json_each(json_extract(a.value, '$.arr2')) AS b
            joins.append(f"json_each(json_extract({prev_alias}.value, '$.{array_key}')) AS {alias}")
            if wexpr is not None:
                wsql, wparams = _scope_expr(wexpr, alias)
                where_clauses.append(wsql)
                where_params += wparams
            prev_alias = alias

        from_join = " JOIN ".join(joins)
        # always compare the last_field in the innermost joined alias
        where = f"json_extract({prev_alias}.value, '$.{last_field}') {op} ?"
        if where_clauses:
            where = " AND ".join(where_clauses + [where])

        # full EXISTS clause, e.g. for two-level array:
        # EXISTS (
        #   SELECT 1
        #   FROM json_each(json_extract(data, '$.level1')) AS a
        #   JOIN json_each(json_extract(a.value, '$.arr2')) AS b
        #   WHERE json_extract(b.value, '$.score') > ?
        # )
        sql = f"EXISTS (SELECT 1 FROM {from_join} WHERE {where})"
        super().__init__(sql, where_params + [val])


def _scope_expr(expr: SQLerExpression, alias: str) -> Tuple[str, list[Any]]:
    """Transform an expression on root data into alias-scoped expression.

    Rewrites JSON_EXTRACT(data, ...) to json_extract(<alias>.value, ...).
    """
    sql = expr.sql
    sql = sql.replace("JSON_EXTRACT(data, ", f"json_extract({alias}.value, ")
    sql = sql.replace("json_extract(data, ", f"json_extract({alias}.value, ")
    return sql, expr.params


class SQLerAnyContext:
    """Context for mid-chain filters on any(). Use .where(expr)."""

    def __init__(self, field: SQLerField, alias: str):
        self._field = field
        self._alias = alias

    def where(self, expression: SQLerExpression) -> "SQLerAnyContext":
        # attach to last alias entry
        if not self._field.alias_stack:
            return self
        new_stack = list(self._field.alias_stack)
        last = new_stack[-1]
        if len(last) == 2:  # type: ignore[arg-type]
            a, f = last  # type: ignore[misc]
            new_stack[-1] = (a, f, expression)
        else:
            a, f, _ = last  # type: ignore[misc]
            new_stack[-1] = (a, f, expression)
        self._field = SQLerField(self._field.path, new_stack)
        return self

    def __getitem__(self, item: Union[str, int]) -> SQLerField:
        return SQLerField(self._field.path + [item], self._field.alias_stack)

    def __truediv__(self, other: str) -> SQLerField:
        return SQLerField(self._field.path + [other], self._field.alias_stack)
