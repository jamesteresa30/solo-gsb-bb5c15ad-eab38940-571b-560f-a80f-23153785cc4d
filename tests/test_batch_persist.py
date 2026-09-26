"""Tests for the atomic multi-record persistence of the batch endpoints.

``POST /api/v1/audits/diff`` and ``POST /api/v1/audits/json`` parse the
whole input first, then write every record of the request in a single
SQLite transaction via ``database.insert_audits``: IDs are backfilled in
input order, a mid-batch failure rolls the whole batch back (the caller
never sees partial records), and an empty batch writes nothing.  The
single-record endpoint shares the same write path and table.
"""

import pytest

RLO = "‮"
PDF = "‬"

_TWO_FILE_DIFF = (
    "diff --git a/clean.py b/clean.py\n--- a/clean.py\n+++ b/clean.py\n"
    "@@ -1 +1 @@\n-aa\n+bb\n"
    "diff --git a/evil.js b/evil.js\n--- a/evil.js\n+++ b/evil.js\n"
    f"@@ -1 +1 @@\n-ok\n+{RLO}bad.js{PDF}\n"
)

_MULTI_VALUE_DOC = (
    '{"clean": "a", "nums": [1, 2], "evil": "' + RLO + '"}'
)


def _break_second_insert(monkeypatch):
    """Make the second audit-row insert of a batch raise."""
    from app import database

    original = database._insert_audit_row
    calls = {"n": 0}

    def failing_insert(conn, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated mid-batch insert failure")
        return original(conn, **kwargs)

    monkeypatch.setattr(database, "_insert_audit_row", failing_insert)
    return calls


# ------------------------------------------------------------- diff batch ---

def test_diff_batch_ids_backfilled_in_file_order(client):
    body = client.post(
        "/api/v1/audits/diff", json={"diff": _TWO_FILE_DIFF}
    ).json()
    assert [i["path"] for i in body["items"]] == ["clean.py", "evil.js"]
    ids = [i["id"] for i in body["items"]]
    assert ids == sorted(ids) and len(set(ids)) == 2
    # every record of the batch is retrievable with its meta intact
    for item, path in zip(body["items"], ["clean.py", "evil.js"]):
        detail = client.get(f"/api/v1/audits/{item['id']}").json()
        assert detail["filename"] == path
        assert detail["meta"]["source"] == "unified_diff"
        assert detail["meta"]["new_path"] == path


def test_diff_batch_rolls_back_everything_on_insert_failure(
    client, monkeypatch
):
    calls = _break_second_insert(monkeypatch)
    with pytest.raises(RuntimeError, match="simulated mid-batch"):
        client.post("/api/v1/audits/diff", json={"diff": _TWO_FILE_DIFF})
    assert calls["n"] == 2  # the first insert ran, then the batch failed
    # the first file's row was rolled back too: nothing partially stored
    assert client.get("/api/v1/audits").json()["total"] == 0


# ------------------------------------------------------------- json batch ---

def test_json_batch_ids_backfilled_in_value_order(client):
    body = client.post(
        "/api/v1/audits/json",
        json={"document": _MULTI_VALUE_DOC, "filename": "cfg.json"},
    ).json()
    assert [i["pointer"] for i in body["items"]] == ["/clean", "/evil"]
    ids = [i["id"] for i in body["items"]]
    assert ids == sorted(ids) and len(set(ids)) == 2
    detail = client.get(f"/api/v1/audits/{ids[1]}").json()
    assert detail["filename"] == "cfg.json#/evil"
    assert detail["meta"]["source"] == "json_document"
    assert detail["meta"]["pointer"] == "/evil"


def test_json_batch_rolls_back_everything_on_insert_failure(
    client, monkeypatch
):
    calls = _break_second_insert(monkeypatch)
    with pytest.raises(RuntimeError, match="simulated mid-batch"):
        client.post(
            "/api/v1/audits/json",
            json={"document": _MULTI_VALUE_DOC, "filename": "cfg.json"},
        )
    assert calls["n"] == 2
    assert client.get("/api/v1/audits").json()["total"] == 0


# --------------------------------------------------------- shared write path

def test_empty_batches_write_nothing(client):
    # no string values / no added lines -> 201 with zero records
    body = client.post(
        "/api/v1/audits/json", json={"document": '{"a": 1}'}
    ).json()
    assert body["value_count"] == 0 and body["items"] == []
    diff = "--- a/d.txt\n+++ b/d.txt\n@@ -1 +0,0 @@\n-x\n"
    body = client.post(
        "/api/v1/audits/diff", json={"diff": diff}
    ).json()
    assert body["file_count"] == 0 and body["items"] == []
    assert client.get("/api/v1/audits").json()["total"] == 0


def test_single_audit_endpoint_uses_the_same_table(client):
    resp = client.post(
        "/api/v1/audits", json={"text": f"x{RLO}y{PDF}", "filename": "t"}
    )
    assert resp.status_code == 201
    audit_id = resp.json()["id"]
    # stored alongside batch records in the same audits table
    client.post("/api/v1/audits/diff", json={"diff": _TWO_FILE_DIFF})
    listing = client.get("/api/v1/audits").json()
    assert listing["total"] == 3
    detail = client.get(f"/api/v1/audits/{audit_id}").json()
    assert detail["filename"] == "t"
    assert client.get(f"/api/v1/audits/{audit_id}/verify").json()[
        "consistent"
    ] is True


def test_insert_audits_empty_list_is_a_noop():
    from app import database

    assert database.insert_audits([]) == []
