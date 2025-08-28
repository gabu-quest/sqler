import json

import pytest

from sqler.query import SQLerField, SQLerQuery


@pytest.fixture
def setup_oligos(oligo_db):
    """populate oligos table with various test oligos"""
    oligos = [
        {
            "sequence": "ACGT",
            "length": 4,
            "tm": 12.3,
            "mass": 1.1,
            "tags": ["short", "test"],
        },
        {
            "sequence": "AACCCGGGGTTTT",
            "length": 13,
            "tm": 47.2,
            "mass": 4.2,
            "tags": ["long", "weird"],
        },
        {"sequence": "TTTT", "length": 4, "tm": 10.2, "mass": 1.0, "tags": ["short"]},
        {
            "sequence": "GATTACA",
            "length": 7,
            "tm": 22.0,
            "mass": 2.0,
            "tags": ["movie", "dna"],
        },
        {
            "sequence": "CCGGAA",
            "length": 6,
            "tm": 18.7,
            "mass": 1.7,
            "tags": ["even", "test"],
        },
        {"sequence": "NNNN", "length": 4, "tm": 0.0, "mass": 0.0, "tags": ["mixed"]},
    ]
    for oligo in oligos:
        oligo_db.insert_document("oligos", oligo)
    return oligos


def test_filter_length_gt(oligo_db, setup_oligos):
    length = SQLerField("length")
    q = SQLerQuery("oligos", oligo_db.adapter)
    result = q.filter(length > 6).all()
    oligos = [json.loads(row) for row in result]
    seqs = {o["sequence"] for o in oligos}
    assert "AACCCGGGGTTTT" in seqs
    assert "GATTACA" in seqs
    assert "CCGGAA" not in seqs


def test_and_or_logic(oligo_db, setup_oligos):
    length = SQLerField("length")
    tag = SQLerField("tags")
    q = SQLerQuery("oligos", oligo_db.adapter)
    expr = ((length == 4) & tag.contains("short")) | (tag.contains("movie"))
    rows = [json.loads(j) for j in q.filter(expr).all()]
    seqs = {o["sequence"] for o in rows}
    # Should include "ACGT", "TTTT" (length 4 and short) and "GATTACA" (movie)
    assert "ACGT" in seqs
    assert "TTTT" in seqs
    assert "GATTACA" in seqs


def test_exclude_by_mass(oligo_db, setup_oligos):
    mass = SQLerField("mass")
    q = SQLerQuery("oligos", oligo_db.adapter)
    result = q.exclude(mass == 0.0).all()
    oligos = [json.loads(row) for row in result]
    seqs = {o["sequence"] for o in oligos}
    assert "NNNN" not in seqs
    assert "ACGT" in seqs


def test_order_by_tm_desc(oligo_db, setup_oligos):
    q = SQLerQuery("oligos", oligo_db.adapter)
    rows = [json.loads(j) for j in q.order_by("tm", desc=True).all()]
    tms = [o["tm"] for o in rows]
    assert tms == sorted(tms, reverse=True)


def test_limit_two_shortest(oligo_db, setup_oligos):
    q = SQLerQuery("oligos", oligo_db.adapter)
    rows = [json.loads(j) for j in q.order_by("length").limit(2).all()]
    # All your short oligos have length 4
    assert all(o["length"] == 4 for o in rows)
    assert len(rows) == 2


def test_first_returns_one(oligo_db, setup_oligos):
    q = SQLerQuery("oligos", oligo_db.adapter)
    first = q.order_by("sequence").first()
    oligo = json.loads(first)
    assert isinstance(oligo, dict)
    assert "sequence" in oligo


def test_chained_queries_are_immutable(oligo_db, setup_oligos):
    tag = SQLerField("tags")
    q = SQLerQuery("oligos", oligo_db.adapter)
    q1 = q.filter(tag.contains("short"))
    q2 = q1.exclude(SQLerField("sequence") == "ACGT")
    seqs1 = {json.loads(j)["sequence"] for j in q1.all()}
    seqs2 = {json.loads(j)["sequence"] for j in q2.all()}
    assert "ACGT" in seqs1
    assert "ACGT" not in seqs2


def test_in_and_like(oligo_db, setup_oligos):
    seq = SQLerField("sequence")
    q = SQLerQuery("oligos", oligo_db.adapter)
    # any sequence exactly one of these
    expr = seq.isin(["ACGT", "GATTACA"])
    rows = [json.loads(j) for j in q.filter(expr).all()]
    seqs = {o["sequence"] for o in rows}
    assert seqs == {"ACGT", "GATTACA"}
    # pattern matching
    expr2 = seq.like("A%")
    rows2 = [json.loads(j) for j in q.filter(expr2).all()]
    for o in rows2:
        assert o["sequence"].startswith("A")


def test_operator_precedence_docstring(oligo_db, setup_oligos):
    seq = SQLerField("sequence")
    q = SQLerQuery("oligos", oligo_db.adapter)
    expr = ((seq == "ACGT") & (seq == "TTTT")) | (seq == "AACCCGGGGTTTT")
    rows = [json.loads(j) for j in q.filter(expr).all()]
    # Only the long one matches (the other AND is never true)
    assert rows[0]["sequence"] == "AACCCGGGGTTTT"
    expr2 = (seq == "ACGT") & ((seq == "TTTT") | (seq == "AACCCGGGGTTTT"))
    rows2 = [json.loads(j) for j in q.filter(expr2).all()]
    # Should be empty
    assert rows2 == []


def test_contains_on_array(oligo_db, setup_oligos):
    tags = SQLerField("tags")
    q = SQLerQuery("oligos", oligo_db.adapter)
    expr = tags.contains("test")
    rows = [json.loads(j) for j in q.filter(expr).all()]
    assert any("test" in o["tags"] for o in rows)


def test_chained_any_levels_with_oligo_db(oligo_db):
    """integration: test single and double any on nested arrays"""
    # Insert doc with a high mz in one of the reads' masses
    oligo_db.insert_document(
        "oligos",
        {
            "sample_name": "NESTED",
            "reads": [
                {
                    "date": "2025-07-10",
                    "masses": [
                        {"mz": 925.4, "note": "target"},
                        {"mz": 789.5, "note": "offtarget"},
                    ],
                },
                {
                    "date": "2025-07-11",
                    "masses": [
                        {"mz": 810.1, "note": "other"},
                    ],
                },
            ],
        },
    )

    # Insert doc with all mz < 900 and different dates
    oligo_db.insert_document(
        "oligos",
        {
            "sample_name": "CONTROL",
            "reads": [
                {
                    "date": "2025-07-15",
                    "masses": [
                        {"mz": 243.12, "note": "low"},
                        {"mz": 789.5, "note": "low2"},
                    ],
                }
            ],
        },
    )

    # One level: find any oligo with a read taken on 2025-07-10
    date = SQLerField(["reads"]).any()["date"]
    expression = date == "2025-07-10"
    q = SQLerQuery("oligos", oligo_db.adapter)
    rows = [json.loads(j) for j in q.filter(expression).all()]
    names = {r["sample_name"] for r in rows}
    assert "NESTED" in names
    assert "CONTROL" not in names

    # Two levels: find any oligo with any read that has any mass with mz > 900
    mz = SQLerField(["reads"]).any()["masses"].any()["mz"]
    expression = mz > 900
    rows = [json.loads(j) for j in q.filter(expression).all()]
    names = {r["sample_name"] for r in rows}
    assert "NESTED" in names
    assert "CONTROL" not in names


def test_deeply_nested_contains_and_range(oligo_db):
    """insert 10k docs with deep arrays, test contains(0) and range query at last level"""
    count = 10000
    keys = [f"level{i}" for i in range(1, 21)]  # level1 ... level20

    for i in range(count):
        d = {}
        ptr = d
        for k in keys[:-1]:
            ptr[k] = {}
            ptr = ptr[k]
        ptr[keys[-1]] = [i, i % 100]  # final array has deterministic values
        d["sample_name"] = f"SAMPLE_{i}"
        oligo_db.insert_document("oligos", d)

    # Query 1: find all with array containing 0 at final level
    field = SQLerField(keys)
    expr = field.contains(0)
    q = SQLerQuery("oligos", oligo_db.adapter)
    rows = [json.loads(j) for j in q.filter(expr).all()]
    found_names = {r["sample_name"] for r in rows}
    expected_names = {f"SAMPLE_{i}" for i in range(0, count, 100)}
    assert found_names == expected_names
    assert len(found_names) == 100

    # Query 2: get all where i in [500, 599]
    # (because final array always includes i as first element)
    expr = field[0] >= 500
    q2 = SQLerQuery("oligos", oligo_db.adapter)
    expr2 = (field[0] >= 500) & (field[0] < 600)
    rows2 = [json.loads(j) for j in q2.filter(expr2).all()]
    found_names2 = {r["sample_name"] for r in rows2}
    expected_names2 = {f"SAMPLE_{i}" for i in range(500, 600)}
    assert found_names2 == expected_names2
    assert len(found_names2) == 100


def test_deeply_nested_index_and_key(oligo_db):
    """insert 10k docs with 20-deep arrays, test [0] and ['thekey'] querying at last level"""

    count = 10000
    keys = [f"level{i}" for i in range(1, 21)]  # level1 ... level20

    # Each doc: deepest level is a dict with a key 'myval' and a list [i, i % 100]
    for i in range(count):
        d = {}
        ptr = d
        for k in keys[:-1]:
            ptr[k] = {}
            ptr = ptr[k]
        ptr[keys[-1]] = {"myval": i, "array": [i, i % 100]}
        d["sample_name"] = f"SAMPLE_{i}"
        oligo_db.insert_document("oligos", d)

    # Query 1: final_level['myval'] < 600 (dict access)
    field = SQLerField(keys)["myval"]
    expr = field < 600
    q = SQLerQuery("oligos", oligo_db.adapter)
    rows = [json.loads(j) for j in q.filter(expr).all()]
    found_names = {r["sample_name"] for r in rows}
    expected_names = {f"SAMPLE_{i}" for i in range(600)}
    assert found_names == expected_names
    assert len(found_names) == 600

    # Query 2: final_level['array'][0] >= 500 and < 600 (array access)
    arr_field = SQLerField(keys)["array"][0]
    expr2 = (arr_field >= 500) & (arr_field < 600)
    q2 = SQLerQuery("oligos", oligo_db.adapter)
    rows2 = [json.loads(j) for j in q2.filter(expr2).all()]
    found_names2 = {r["sample_name"] for r in rows2}
    expected_names2 = {f"SAMPLE_{i}" for i in range(500, 600)}
    assert found_names2 == expected_names2
    assert len(found_names2) == 100


# some cute afterthought tests to show mini examples, sanity check?


# 1. simple field comparison
def test_simple_field_gt(oligo_db):
    """simple numeric > comparison"""
    oligo_db.insert_document("oligos", {"count": 3})
    oligo_db.insert_document("oligos", {"count": 12})
    expr = SQLerField("count") > 5
    q = SQLerQuery("oligos", oligo_db.adapter)
    rows = [json.loads(j) for j in q.filter(expr).all()]
    assert [r["count"] for r in rows] == [12]


# 2. nested field
def test_nested_field_le(oligo_db):
    """nested field <= comparison"""
    doc1 = {"meta": {"info": {"score": 150}}}
    doc2 = {"meta": {"info": {"score": 90}}}
    oligo_db.insert_document("oligos", doc1)
    oligo_db.insert_document("oligos", doc2)
    expr = SQLerField(["meta", "info", "score"]) <= 100
    q = SQLerQuery("oligos", oligo_db.adapter)
    rows = [json.loads(j) for j in q.filter(expr).all()]
    assert rows[0]["meta"]["info"]["score"] == 90


# 3. alternative / syntax
def test_alternative_syntax_ne(oligo_db):
    """use / to dive into nested keys"""
    doc1 = {"meta": {"info": {"score": 0}}}
    doc2 = {"meta": {"info": {"score": 5}}}
    oligo_db.insert_document("oligos", doc1)
    oligo_db.insert_document("oligos", doc2)
    f = SQLerField("meta") / "info" / "score"
    rows = [json.loads(j) for j in SQLerQuery("oligos", oligo_db.adapter).filter(f != 0).all()]
    assert rows[0]["meta"]["info"]["score"] == 5


# 4. contains on array of primitives
def test_contains_array_primitives(oligo_db):
    """array contains value"""
    oligo_db.insert_document("oligos", {"tags": ["red", "blue"]})
    oligo_db.insert_document("oligos", {"tags": ["green"]})
    expr = SQLerField("tags").contains("blue")
    q = SQLerQuery("oligos", oligo_db.adapter)
    rows = [json.loads(j) for j in q.filter(expr).all()]
    assert rows[0]["tags"] == ["red", "blue"]


# 5. isin on array of primitives
def test_isin_array_primitives(oligo_db):
    """array isin values"""
    oligo_db.insert_document("oligos", {"tags": [1, 2, 3]})
    oligo_db.insert_document("oligos", {"tags": [4, 5]})
    expr = SQLerField("tags").isin([3, 5])
    rows = [json.loads(j) for j in SQLerQuery("oligos", oligo_db.adapter).filter(expr).all()]
    names = [r["tags"] for r in rows]
    assert [[1, 2, 3], [4, 5]][0] in names  # both match one of the list


# 6. like on strings
def test_like_string(oligo_db):
    """text LIKE pattern"""
    oligo_db.insert_document("oligos", {"name": "ABC123"})
    oligo_db.insert_document("oligos", {"name": "XYZ"})
    expr = SQLerField("name").like("ABC%")
    rows = [json.loads(j) for j in SQLerQuery("oligos", oligo_db.adapter).filter(expr).all()]
    assert rows[0]["name"] == "ABC123"


# 7. index into array of primitives
def test_index_array_primitives(oligo_db):
    """access array index 0"""
    oligo_db.insert_document("oligos", {"tags": ["first", "second"]})
    expr = SQLerField("tags")[0] == "first"
    rows = [json.loads(j) for j in SQLerQuery("oligos", oligo_db.adapter).filter(expr).all()]
    assert rows[0]["tags"][0] == "first"


# 8. one-level array of objects
def test_any_one_level(oligo_db):
    """any() on array of dicts"""
    oligo_db.insert_document("oligos", {"peaks": [{"mz": 800}, {"mz": 950}]})
    expr = SQLerField(["peaks"]).any()["mz"] > 900
    rows = [json.loads(j) for j in SQLerQuery("oligos", oligo_db.adapter).filter(expr).all()]
    assert rows[0]["peaks"][1]["mz"] == 950


# 9. two-level nested arrays
def test_any_two_levels(oligo_db):
    """double any() on nested arrays"""
    doc = {"reads": [{"masses": [{"mz": 910}, {"mz": 880}]}]}
    oligo_db.insert_document("oligos", doc)
    expr = SQLerField(["reads"]).any()["masses"].any()["mz"] > 900
    rows = [json.loads(j) for j in SQLerQuery("oligos", oligo_db.adapter).filter(expr).all()]
    assert rows and rows[0]["reads"][0]["masses"][0]["mz"] == 910


# 10. deeply nested dicts
def test_deeply_nested_dict(oligo_db):
    """no arrays, pure dict nesting"""
    d = {"a": {"b": {"c": {"d": 42}}}}
    oligo_db.insert_document("oligos", d)
    expr = SQLerField(["a", "b", "c", "d"]) == 42
    rows = [json.loads(j) for j in SQLerQuery("oligos", oligo_db.adapter).filter(expr).all()]
    assert rows[0]["a"]["b"]["c"]["d"] == 42


# 11. combining filters
def test_combined_filters(oligo_db):
    """AND combination of two comparisons"""
    oligo_db.insert_document("oligos", {"count": 5})
    oligo_db.insert_document("oligos", {"count": 15})
    expr = (SQLerField("count") >= 10) & (SQLerField("count") < 20)
    rows = [json.loads(j) for j in SQLerQuery("oligos", oligo_db.adapter).filter(expr).all()]
    assert rows[0]["count"] == 15


def test_mid_chain_filter_on_nested_array(oligo_db):
    """
    this is a placeholder: should match any record where
    any read in reads[] has 'note' == 'good', even if other reads are bad

    should support:
      SQLerField(['reads']).any()['note'] == 'good'
      # (currently works)

    but also should support:
      SQLerField(['reads']).any()['masses'].any()['val'] > 10
      # (works now)
      # mid-chain filter:
      SQLerField(['reads']).any()[lambda r: r['note'] == 'good']['masses'].any()['val'] > 10
      # (not yet possible!)
    """
    oligo_db.insert_document(
        "oligos",
        {
            "sample_name": "MIXED",
            "reads": [
                {"note": "bad", "masses": [{"val": 5}, {"val": 11}]},
                {"note": "good", "masses": [{"val": 9}, {"val": 20}]},
            ],
        },
    )
    oligo_db.insert_document(
        "oligos",
        {
            "sample_name": "NONE",
            "reads": [{"note": "bad", "masses": [{"val": 1}, {"val": 2}]}],
        },
    )

    # query for oligos where any read has note == 'good' and, for that read, any mass.val > 10
    expr = (
        SQLerField(["reads"]).any().where(SQLerField(["note"]) == "good")["masses"].any()["val"]
        > 10
    )
    rows = [json.loads(j) for j in SQLerQuery("oligos", oligo_db.adapter).filter(expr).all()]
    assert len(rows) == 1
    assert rows[0]["sample_name"] == "MIXED"
