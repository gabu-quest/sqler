"""Shared utility functions for SQLer.

This module provides common utilities used across the library,
reducing code duplication and ensuring consistent behavior.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Compiled regex for table name validation
# Used with fullmatch: ``$`` alone would accept a name ending in a newline.
_TABLE_NAME_PATTERN = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

# The tables sqler keeps for itself, in lower case: the migrations table (``MigrationRunner``) and
# the record of settled promoted columns. None is a user's table: ``validate_table_name`` refuses
# their names, and the table counts and listings leave them out.
SQLER_TABLES: frozenset[str] = frozenset({"_sqler_migrations", "_sqler_promoted"})

# SQLite reserved words that produce invalid SQL when used as unquoted column names.
# Source: https://www.sqlite.org/lang_keywords.html
_SQL_RESERVED_WORDS: frozenset[str] = frozenset({
    "abort", "action", "add", "after", "all", "alter", "always", "analyze",
    "and", "as", "asc", "attach", "autoincrement", "before", "begin",
    "between", "by", "cascade", "case", "cast", "check", "collate", "column",
    "commit", "conflict", "constraint", "create", "cross", "current",
    "current_date", "current_time", "current_timestamp", "database", "default",
    "deferrable", "deferred", "delete", "desc", "detach", "distinct", "do",
    "drop", "each", "else", "end", "escape", "except", "exclude", "exclusive",
    "exists", "explain", "fail", "filter", "first", "following", "for",
    "foreign", "from", "full", "generated", "glob", "group", "groups",
    "having", "if", "ignore", "immediate", "in", "index", "indexed",
    "initially", "inner", "insert", "instead", "intersect", "into", "is",
    "isnull", "join", "key", "last", "left", "like", "limit", "match",
    "materialized", "natural", "no", "not", "nothing", "notnull", "null",
    "nulls", "of", "offset", "on", "or", "order", "others", "outer", "over",
    "partition", "plan", "pragma", "preceding", "primary", "query", "raise",
    "range", "recursive", "references", "regexp", "reindex", "release",
    "rename", "replace", "restrict", "returning", "right", "rollback", "row",
    "rows", "savepoint", "select", "set", "table", "temp", "temporary",
    "then", "ties", "to", "transaction", "trigger", "unbounded", "union",
    "unique", "update", "using", "vacuum", "values", "view", "virtual",
    "when", "where", "window", "with", "without",
})

logger = logging.getLogger("sqler.utils")

# names validate_table_name() has passed: a name is checked once, then found here
_VALID_TABLE_NAMES: set[str] = set()
_VALID_TABLE_NAMES_KEPT = 4096


def validate_table_name(table: str) -> str:
    """Validate and sanitize table name to prevent SQL injection.

    Args:
        table: Table name to validate.

    Returns:
        str: The validated table name.

    Raises:
        InvalidTableNameError: If the table name contains invalid characters, or is the
            name of a table sqler keeps for itself (``SQLER_TABLES``).
    """
    if table.__class__ is str and table in _VALID_TABLE_NAMES:
        return table
    from sqler.exceptions import InvalidTableNameError

    if not isinstance(table, str) or not table:
        raise InvalidTableNameError(
            f"Table name must be a non-empty string, got: {type(table).__name__}",
            table=table,
        )
    if not _TABLE_NAME_PATTERN.fullmatch(table):
        raise InvalidTableNameError(
            f"Invalid table name: {table!r}. Must match [a-zA-Z_][a-zA-Z0-9_]*",
            table=table,
        )
    if table[0] == "_" and table.lower() in SQLER_TABLES:
        raise InvalidTableNameError(f"{table!r} is a table sqler keeps for itself", table=table)
    if len(_VALID_TABLE_NAMES) < _VALID_TABLE_NAMES_KEPT:
        _VALID_TABLE_NAMES.add(table)
    return table


def validate_field_name(name: str) -> str:
    """Validate a dotted JSON field path (e.g. ``meta.level``).

    Each segment must match ``[a-zA-Z_][a-zA-Z0-9_]*``.

    Args:
        name: Dotted field path to validate.

    Returns:
        str: The validated field name.

    Raises:
        InvalidFieldNameError: If any segment is invalid.
    """
    from sqler.exceptions import InvalidFieldNameError

    if not isinstance(name, str) or not name:
        raise InvalidFieldNameError(
            f"Field name must be a non-empty string, got: {type(name).__name__}",
            field=name,
        )
    segments = name.split(".")
    for segment in segments:
        if not _TABLE_NAME_PATTERN.fullmatch(segment):
            raise InvalidFieldNameError(
                f"Invalid field name: {name!r}. "
                f"Each segment must match [a-zA-Z_][a-zA-Z0-9_]*",
                field=name,
            )
    return name


def validate_identifier(name: str) -> str:
    """Validate a SQL identifier (index name, column name).

    Must match ``[a-zA-Z_][a-zA-Z0-9_]*``.

    Args:
        name: Identifier to validate.

    Returns:
        str: The validated identifier.

    Raises:
        InvalidIdentifierError: If the identifier is invalid.
    """
    from sqler.exceptions import InvalidIdentifierError

    if not isinstance(name, str) or not name:
        raise InvalidIdentifierError(
            f"Identifier must be a non-empty string, got: {type(name).__name__}",
            identifier=name,
        )
    if not _TABLE_NAME_PATTERN.fullmatch(name):
        raise InvalidIdentifierError(
            f"Invalid identifier: {name!r}. Must match [a-zA-Z_][a-zA-Z0-9_]*",
            identifier=name,
        )
    return name


# Grammar for promoted-column definitions.
# Accepts: TYPE [NOT NULL] [DEFAULT literal]
#   TYPE ::= TEXT | INTEGER | REAL | BLOB | NUMERIC [(p[,s])]
#   literal ::= integer | float | 'single-quoted' | NULL | CURRENT_TIMESTAMP | CURRENT_DATE | CURRENT_TIME
# DEFAULT sub-queries, expression-valued DEFAULTs, and parenthesized expressions are intentionally rejected
# — those are DDL-injection vectors that cannot be parameterized in SQLite.
_COLUMN_DEF_PATTERN = re.compile(
    r"""
    ^\s*
    (?:TEXT|INTEGER|REAL|BLOB|NUMERIC(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?)
    (?:\s+NOT\s+NULL)?
    (?:\s+DEFAULT\s+
        (?:
            -?\d+(?:\.\d+)?                # numeric literal
          | '(?:[^'\\;]|'')*'              # single-quoted string literal (SQL-style '' escape allowed)
          | NULL
          | CURRENT_TIMESTAMP
          | CURRENT_DATE
          | CURRENT_TIME
        )
    )?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Keywords that must never appear inside a CHECK expression — they belong to DDL/DML, not predicates.
_CHECK_EXPR_BANNED_KEYWORDS = re.compile(
    r"\b(?:DROP|CREATE|ALTER|ATTACH|DETACH|PRAGMA|INSERT|UPDATE|DELETE|REPLACE|VACUUM|REINDEX)\b",
    re.IGNORECASE,
)


def validate_column_def(col_def: str) -> str:
    """Validate a promoted-column definition string (e.g. ``TEXT NOT NULL DEFAULT 'pending'``).

    Promoted column *definitions* are interpolated into DDL unparameterized (SQLite DDL does
    not accept bound parameters), so the accepted grammar is strict: a SQLite storage type,
    optional ``NOT NULL``, and optional ``DEFAULT`` of a literal value. Anything richer —
    subqueries, parenthesised expressions, function calls — is rejected.

    Args:
        col_def: The definition to validate.

    Returns:
        str: The validated definition, unchanged.

    Raises:
        InvalidColumnDefError: If the definition contains anything outside the accepted grammar.
    """
    from sqler.exceptions import InvalidColumnDefError

    if not isinstance(col_def, str) or not col_def.strip():
        raise InvalidColumnDefError(
            f"Column definition must be a non-empty string, got: {type(col_def).__name__}",
            definition=col_def,
        )
    if not _COLUMN_DEF_PATTERN.match(col_def):
        raise InvalidColumnDefError(
            f"Invalid column definition: {col_def!r}. "
            "Accepted grammar: "
            "TYPE [NOT NULL] [DEFAULT <literal>] where TYPE is TEXT|INTEGER|REAL|BLOB|NUMERIC "
            "and <literal> is a number, quoted string, NULL, or CURRENT_TIMESTAMP/CURRENT_DATE/CURRENT_TIME.",
            definition=col_def,
        )
    return col_def


def validate_check_expression(expr: str) -> str:
    """Validate a CHECK-constraint expression.

    CHECK expressions are interpolated raw into DDL, so we forbid anything that could escape
    the constraint context: semicolons, SQL comment markers, and DDL/DML keywords.

    This is not a full SQL parser — it's an allowlist of the *shape* we expect (a predicate),
    rejecting the common injection vectors. Users who need richer CHECK expressions should
    construct the table DDL directly rather than relying on promoted-column convenience.

    Args:
        expr: The CHECK expression to validate.

    Returns:
        str: The validated expression, unchanged.

    Raises:
        InvalidColumnDefError: If the expression contains a forbidden token.
    """
    from sqler.exceptions import InvalidColumnDefError

    if not isinstance(expr, str) or not expr.strip():
        raise InvalidColumnDefError(
            f"CHECK expression must be a non-empty string, got: {type(expr).__name__}",
            definition=expr,
        )
    if ";" in expr:
        raise InvalidColumnDefError(
            f"CHECK expression may not contain ';': {expr!r}",
            definition=expr,
        )
    if "--" in expr or "/*" in expr or "*/" in expr:
        raise InvalidColumnDefError(
            f"CHECK expression may not contain SQL comment markers: {expr!r}",
            definition=expr,
        )
    match = _CHECK_EXPR_BANNED_KEYWORDS.search(expr)
    if match:
        raise InvalidColumnDefError(
            f"CHECK expression may not contain keyword {match.group(0)!r}: {expr!r}",
            definition=expr,
        )
    return expr


def validate_column_name(name: str) -> str:
    """Validate a SQL column name — format check plus reserved-word rejection.

    Promoted columns appear unquoted in DDL and DML, so SQLite reserved words
    like ``index``, ``order``, or ``group`` would produce invalid SQL.

    Args:
        name: Column name to validate.

    Returns:
        str: The validated column name.

    Raises:
        InvalidIdentifierError: If the name is invalid or a reserved word.
    """
    from sqler.exceptions import InvalidIdentifierError

    validate_identifier(name)
    if name.lower() in _SQL_RESERVED_WORDS:
        raise InvalidIdentifierError(
            f"'{name}' is a SQLite reserved word and cannot be used as a column name. "
            f"Choose a different name (e.g. '{name}_val', '{name}_col').",
            identifier=name,
        )
    return name


def is_ref_dict(value: object) -> bool:
    """True when ``value`` is a reference to a row: a dict whose keys are exactly ``_table``,
    a ``str``, and ``_id``, an ``int`` that is not a ``bool`` (what
    :func:`sqler.models.ref.as_ref` builds). Any other dict is data: stored whole, read
    whole, and never followed or counted as a referrer.

    Args:
        value: Value to check.

    Returns:
        bool: True if value is a reference dictionary.
    """
    if not isinstance(value, dict) or len(value) != 2:
        return False
    table, rid = value.get("_table"), value.get("_id")
    return isinstance(table, str) and isinstance(rid, int) and not isinstance(rid, bool)
