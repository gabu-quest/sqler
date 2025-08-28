from sqler.query import SQLerExpression, SQLerField


def test_comparison_operators():
    """check all the operator overloads for sql and params"""
    length = SQLerField("length")
    seq = SQLerField("sequence")
    # added an == method for sqler expresion so we can verify this idk
    assert (length == 18) == SQLerExpression("JSON_EXTRACT(data, '$.length') = ?", [18])
    assert (length != 10) == SQLerExpression("JSON_EXTRACT(data, '$.length') != ?", [10])
    assert (length > 5) == SQLerExpression("JSON_EXTRACT(data, '$.length') > ?", [5])
    assert (length >= 2) == SQLerExpression("JSON_EXTRACT(data, '$.length') >= ?", [2])
    assert (length < 3) == SQLerExpression("JSON_EXTRACT(data, '$.length') < ?", [3])
    assert (length <= 4) == SQLerExpression("JSON_EXTRACT(data, '$.length') <= ?", [4])
    assert (seq == "ACGT") == SQLerExpression("JSON_EXTRACT(data, '$.sequence') = ?", ["ACGT"])


def test_json_path_and_nesting():
    """json path building w/ [] and /"""
    specs = SQLerField("specs")
    bases = specs["bases"]
    tag = specs / "tag"
    assert bases == SQLerField(["specs", "bases"])
    assert tag == SQLerField(["specs", "tag"])
    assert (bases == 10).sql == "JSON_EXTRACT(data, '$.specs.bases') = ?"
    assert (tag == "A").sql == "JSON_EXTRACT(data, '$.specs.tag') = ?"


def test_contains_isin_sql_array():
    """check contains, isin, like helpers build json_each sql for arrays"""
    tag = SQLerField("tags")

    expression = tag.contains("exon")
    assert (
        expression.sql
        == "EXISTS (SELECT 1 FROM json_each(data, '$.tags') WHERE json_each.value = ?)"
    )
    assert expression.params == ["exon"]

    expr2 = tag.isin(["exon", "intron", "utr"])
    assert expr2.sql == (
        "EXISTS (SELECT 1 FROM json_each(data, '$.tags') WHERE json_each.value IN (?, ?, ?))"
    )
    assert expr2.params == ["exon", "intron", "utr"]

    # Like stays the same since it’s not for arrays
    expr3 = tag.like("exon%")
    assert expr3.sql == "JSON_EXTRACT(data, '$.tags') LIKE ?"
    assert expr3.params == ["exon%"]


def test_fields_make_the_same_way():
    """make sure the paths are working because why not"""
    seq = SQLerField("sequence")
    seq2 = SQLerField(["sequence"])
    region = SQLerField(["sequence", "region"])
    assert seq.path == seq2.path
    assert seq.path != region.path


def test_isin_empty():
    """should return SQLerExpression("0", [])"""
    oligo_type = SQLerField("type")
    should_be_empty = oligo_type.isin([])
    assert should_be_empty.sql == "0"
    assert should_be_empty.params == []


def test_real_field_works_with_oligo_db(oligo_db):
    """integration: make sure field can be used in real queries on oligos table"""
    # insert some oligos
    oligo_db.insert_document("oligos", {"length": 18, "sequence": "ACGTACGTACGTACGTAC"})
    oligo_db.insert_document(
        "oligos", {"length": 20, "sequence": "CGTAAAGGGTTTCCCAAAGG", "tag": "dye"}
    )
    oligo_db.insert_document("oligos", {"length": 15, "sequence": "GGGTTTAAACCCGGG"})

    length = SQLerField("length")
    tag = SQLerField("tag")
    # query for oligos with length > 16
    expression = length > 16
    results = oligo_db.execute_sql(
        f"SELECT _id, data FROM oligos WHERE {expression.sql}", expression.params
    )
    docs = [d for d in results]
    assert all(doc["length"] > 16 for doc in docs)

    # query for oligos with tag = 'dye'
    expression = tag == "dye"
    results = oligo_db.execute_sql(
        f"SELECT _id, data FROM oligos WHERE {expression.sql}", expression.params
    )
    docs = [d for d in results]
    assert all(doc.get("tag") == "dye" for doc in docs)


def test_contains_isin_real_integration(oligo_db):
    """integration: make sure contains and isin work in real queries with array fields"""
    # Insert oligos with different tags
    oligo_db.insert_document(
        "oligos",
        {"sequence": "ACGTACGTACGTACGTAC", "tags": ["test", "forward"]},
    )
    oligo_db.insert_document(
        "oligos",
        {"sequence": "CGTAAAGGGTTTCCCAAAGG", "tags": ["test", "reverse"]},
    )
    oligo_db.insert_document(
        "oligos",
        {"sequence": "GGGTTTAAACCCGGG", "tags": []},
    )

    tag = SQLerField("tags")

    # Should find the first oligo (has "forward" tag)
    expression = tag.contains("forward")
    results = oligo_db.execute_sql(
        f"SELECT _id, data FROM oligos WHERE {expression.sql}", expression.params
    )
    docs = [d for d in results]
    assert len(docs) == 1
    assert docs[0]["sequence"] == "ACGTACGTACGTACGTAC"

    # Should find the second oligo (has "reverse" tag)
    expression = tag.contains("reverse")
    results = oligo_db.execute_sql(
        f"SELECT _id, data FROM oligos WHERE {expression.sql}", expression.params
    )
    docs = [d for d in results]
    assert len(docs) == 1
    assert docs[0]["sequence"] == "CGTAAAGGGTTTCCCAAAGG"

    # Should not find any with tag "hairpin"
    expression = tag.contains("hairpin")
    results = oligo_db.execute_sql(
        f"SELECT _id, data FROM oligos WHERE {expression.sql}", expression.params
    )
    docs = [d for d in results]
    assert docs == []

    # Test isin: find any oligo with tag "forward" or "reverse"
    expression = tag.isin(["forward", "reverse"])
    results = oligo_db.execute_sql(
        f"SELECT _id, data FROM oligos WHERE {expression.sql}", expression.params
    )
    docs = [d for d in results]
    seqs = {d["sequence"] for d in docs}
    assert seqs == {"ACGTACGTACGTACGTAC", "CGTAAAGGGTTTCCCAAAGG"}
