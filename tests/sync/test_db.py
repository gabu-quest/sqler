import pytest

from sqler import NotConnectedError


def test_insert_and_find_document(oligo_db):
    nucleotide = {"sequence": "ACGT", "length": 4, "label": "sample-1"}
    doc_id = oligo_db.insert_document("oligos", nucleotide)
    assert isinstance(doc_id, int)
    result = oligo_db.find_document("oligos", doc_id)
    assert result["sequence"] == "ACGT"
    assert result["length"] == 4
    assert result["label"] == "sample-1"
    assert result["_id"] == doc_id


def test_upsert_document(oligo_db):
    oligo = {"sequence": "TTAA", "length": 4}
    doc_id = oligo_db.insert_document("oligos", oligo)
    updated_oligo = {"sequence": "TTAA", "length": 5, "modification": "phosphate"}
    updated_id = oligo_db.upsert_document("oligos", doc_id, updated_oligo)
    assert updated_id == doc_id
    fetched = oligo_db.find_document("oligos", doc_id)
    assert fetched["length"] == 5
    assert fetched["modification"] == "phosphate"


def test_delete_document(oligo_db):
    oligo = {"sequence": "CCGG", "length": 4}
    doc_id = oligo_db.insert_document("oligos", oligo)
    oligo_db.delete_document("oligos", doc_id)
    assert oligo_db.find_document("oligos", doc_id) is None


def test_execute_sql(oligo_db):
    for seq in ["A", "AC", "ACG"]:
        oligo_db.insert_document("oligos", {"sequence": seq, "length": len(seq)})
    results = oligo_db.execute_sql(
        "SELECT _id, data FROM oligos WHERE json_extract(data, '$.length') >= ?;",
        [2],
    )
    lengths = [d["length"] for d in results]
    assert set(lengths) == {2, 3}


def test_bulk_upsert_mixed_insert_and_update(oligo_db):
    # Insert 100 oligos
    oligos = [{"sequence": "A" * i, "length": i} for i in range(1, 101)]
    ids = oligo_db.bulk_upsert("oligos", oligos)
    assert len(ids) == 100

    # Update first 50, add 50 new
    for oligo in oligos[:50]:
        oligo["label"] = "modified"
    new_oligos = [{"sequence": "T" * i, "length": i} for i in range(101, 151)]
    batch = oligos[:50] + new_oligos
    all_ids = oligo_db.bulk_upsert("oligos", batch)
    assert len(all_ids) == 100

    # Check updates
    for oligo in batch[:10]:
        found = oligo_db.find_document("oligos", oligo["_id"])
        if "label" in oligo:
            assert found["label"] == "modified"

    # Check new inserts
    for oligo in new_oligos[:5]:
        found = oligo_db.find_document("oligos", oligo["_id"])
        assert found["sequence"] == oligo["sequence"]


def test_find_document_nonexistent(oligo_db):
    assert oligo_db.find_document("oligos", -12345) is None


def test_upsert_document_new_and_existing(oligo_db):
    new_oligo = {"sequence": "GCGC"}
    doc_id = oligo_db.upsert_document("oligos", None, new_oligo)
    assert isinstance(doc_id, int)
    updated_oligo = {"sequence": "GCGC", "purified": True}
    updated_id = oligo_db.upsert_document("oligos", doc_id, updated_oligo)
    assert updated_id == doc_id
    result = oligo_db.find_document("oligos", doc_id)
    assert result["purified"] is True


def test_delete_document_nonexistent(oligo_db):
    oligo_db.delete_document("oligos", -99999)  # Should not raise


def test_insert_document_empty_json(oligo_db):
    doc_id = oligo_db.insert_document("oligos", {})
    fetched = oligo_db.find_document("oligos", doc_id)
    assert fetched == {"_id": doc_id}


def test_execute_sql_no_params(oligo_db):
    doc_id = oligo_db.insert_document("oligos", {"sequence": "ATCG"})
    results = oligo_db.execute_sql("SELECT _id, data FROM oligos;")
    assert any(d["_id"] == doc_id for d in results)


def test_adapter_closed_error(oligo_db):
    oligo_db.adapter.close()
    with pytest.raises(NotConnectedError):
        oligo_db.insert_document("oligos", {"sequence": "AGCT"})


def test_bulk_upsert_empty_list(oligo_db):
    result = oligo_db.bulk_upsert("oligos", [])
    assert result == []
    cur = oligo_db.adapter.execute("SELECT COUNT(*) FROM oligos;")
    assert cur.fetchone()[0] == 0
