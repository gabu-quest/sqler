from typing import Any, NamedTuple, Optional, Self


class Related(NamedTuple):
    """The rows of another table a part of an expression reads (a filter through a
    reference): ``table``, the related table; ``where``, the condition on its rows, written
    as a query on that table writes it (``json_extract(data, ...)``); ``text``, the subquery
    of those rows as the expression's ``sql`` holds it, with no table laid out. A query with
    a database behind it puts in its place the same rows laid out as that database
    remembers ``table`` (``sqler.query.query.related_rows``)."""

    table: str
    where: str
    text: str


class SQLerExpression:
    """sql expression fragment with parameters; supports & for and, | for or, ~ for not"""

    # the rows of other tables the expression reads (:class:`Related`), in order; none for most
    related: tuple[Related, ...] = ()

    def __init__(self, sql: str, params: Optional[list[Any]] = None):
        """init with sql fragment and param list; sql like "foo > ?" or "json_extract(data, '$.x') = ?" """
        self.sql = sql
        self.params = params or []

    def __and__(self, other: Self) -> Self:
        """combine two exprs with and; params concatenated"""
        return _carried(
            self.__class__(f"({self.sql}) AND ({other.sql})", self.params + other.params),
            self,
            other,
        )

    def __or__(self, other: Self) -> Self:
        """combine two exprs with or; params concatenated"""
        return _carried(
            self.__class__(f"({self.sql}) OR ({other.sql})", self.params + other.params),
            self,
            other,
        )

    def __invert__(self) -> Self:
        """negate expr with not"""
        return _carried(self.__class__(f"NOT ({self.sql})", self.params), self)

    def __eq__(self, other: object) -> bool:
        """equality for testing"""
        if not isinstance(other, self.__class__):
            return False
        return self.sql == other.sql and self.params == other.params

    def __str__(self) -> str:
        """return sql fragment string"""
        return self.sql

    def __repr__(self) -> str:
        return f"{self.__class__}({self.sql!r}, {self.params!r})"


def _carried(made: Any, *parts: Any) -> Any:
    """``made``, an expression built from the text of ``parts``, with the related rows they
    read (:attr:`SQLerExpression.related`)."""
    related = tuple(r for part in parts for r in getattr(part, "related", ()))
    if related:
        made.related = related
    return made
