from sqler.query import SQLerQuery


def test_all_dicts_returns_parsed_with_id(dummy_adapter):
    q = SQLerQuery(table="users", adapter=dummy_adapter)
    dummy_adapter.return_value = [
        {"_id": 1, "name": "Alice", "age": 30},
        {"_id": 2, "name": "Bob", "age": 25},
    ]

    docs = q.all_dicts()
    assert docs == [
        {"_id": 1, "name": "Alice", "age": 30},
        {"_id": 2, "name": "Bob", "age": 25},
    ]

    # Ensure query selected both columns
    assert any(stmt[0].startswith("SELECT _id, data") for stmt in dummy_adapter.executed)


def test_first_dict_limits_and_returns_none(dummy_adapter):
    q = SQLerQuery(table="users", adapter=dummy_adapter)
    dummy_adapter.return_value = []
    assert q.first_dict() is None

    # Now with a result
    dummy_adapter.return_value = [{"_id": 10, "name": "Zoe"}]
    doc = q.first_dict()
    assert doc == {"_id": 10, "name": "Zoe"}
    # Should have used LIMIT 1
    assert any("LIMIT 1" in stmt[0] for stmt in dummy_adapter.executed)
