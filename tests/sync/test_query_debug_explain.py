from sqler import SQLerDB
from sqler.query import SQLerField as F


def test_debug_returns_sql_and_params(oligo_db: SQLerDB):
    db = oligo_db
    # seed
    db.insert_document("oligos", {"length": 10, "sequence": "AAA"})
    db.insert_document("oligos", {"length": 20, "sequence": "BBB"})

    from sqler.query import SQLerQuery

    q = SQLerQuery(table="oligos", adapter=db.adapter).filter(F("length") >= 15)
    sql, params = q.debug()
    assert "SELECT" in sql and params == [15]


def test_explain_query_plan_runs_and_returns_rows(oligo_db: SQLerDB):
    db = oligo_db
    from sqler.query import SQLerQuery

    q = SQLerQuery(table="oligos", adapter=db.adapter).filter(F("length") >= 10)
    rows = q.explain_query_plan(db.adapter)
    assert isinstance(rows, list) and rows
