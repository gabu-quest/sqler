from sqler.query.expression import SQLerExpression

# sql bits to reuse
LEN_SQL = "length < ?"
TM_SQL = "tm < ?"
IS_SQL = "modification IS NULL"
LIKE_SQL = "sequence LIKE ?"


def test_and():
    """tests and to combine expressions
    an expression should take sql and parameters
    and-ing them should result in combined sql i hope"""

    # try to combine some sql expressions
    a = SQLerExpression(LEN_SQL, [20])
    b = SQLerExpression(TM_SQL, [50])
    # gonna make sure it can combine two expressions
    combined = a & b

    # should have a .sql that we can inspect
    assert combined.sql == f"({LEN_SQL}) AND ({TM_SQL})"
    # same with .params
    assert combined.params == [20, 50]


def test_or():
    """tests or to combine expresions"""
    # try to combine some sql expressions
    a = SQLerExpression(LEN_SQL, [20])
    b = SQLerExpression(TM_SQL, [50])
    # gonna make sure it can combine two expressions
    combined = a | b

    # should have a .sql that we can inspect
    assert combined.sql == f"({LEN_SQL}) OR ({TM_SQL})"
    # same with .params
    assert combined.params == [20, 50]


def test_not():
    """tests not with ~"""
    # try to negate the expression
    a = SQLerExpression(IS_SQL)
    negated = ~a

    # should have a .sql that we can inspect
    assert negated.sql == f"NOT ({IS_SQL})"
    # same with .params
    assert negated.params == []


def test_str():
    """tests string representation"""
    a = SQLerExpression(LIKE_SQL, ["G%G"])
    assert str(a) == LIKE_SQL
    assert LIKE_SQL in repr(a)
    assert "'G%G'" in repr(a)


def test_chaining_expressions():
    """tests chaining more than one"""
    # let's try and combine these
    a = SQLerExpression(LEN_SQL, [20])
    b = SQLerExpression(TM_SQL, [50])
    c = SQLerExpression(LIKE_SQL, ["TTT%"])
    d = SQLerExpression(IS_SQL)

    expression = ((a | b) & c) & ~d
    assert expression.sql == f"((({LEN_SQL}) OR ({TM_SQL})) AND ({LIKE_SQL})) AND (NOT ({IS_SQL}))"
    assert expression.params == [20, 50, "TTT%"]
