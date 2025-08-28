from typing import Any, Optional, Self


class SQLerExpression:
    """sql expression fragment with parameters; supports & for and, | for or, ~ for not"""

    def __init__(self, sql: str, params: Optional[list[Any]] = None):
        """init with sql fragment and param list; sql like "foo > ?" or "json_extract(data, '$.x') = ?" """
        self.sql = sql
        self.params = params or []

    def __and__(self, other: Self) -> Self:
        """combine two exprs with and; params concatenated"""
        return self.__class__(f"({self.sql}) AND ({other.sql})", self.params + other.params)

    def __or__(self, other: Self) -> Self:
        """combine two exprs with or; params concatenated"""
        return self.__class__(f"({self.sql}) OR ({other.sql})", self.params + other.params)

    def __invert__(self) -> Self:
        """negate expr with not"""
        return self.__class__(f"NOT ({self.sql})", self.params)

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
