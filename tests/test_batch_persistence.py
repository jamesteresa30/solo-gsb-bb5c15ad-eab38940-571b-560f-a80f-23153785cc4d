"""Tests for the transactional batch persistence of multi-record audits.

POST /api/v1/audits/diff and POST /api/v1/audits/json write every record
of one request in a single SQLite transaction: a failing insert rolls the
whole batch back (the caller never sees partially added records), record
IDs are backfilled in input order, and an empty batch writes nothing.
The single-record and query endpoints keep using the same table.
"""

import pytest

RLO = "‮"
PDF = "‬"


def _two_file_diff():
    return (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1 +1 @@\n-x\n+first\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
        f"@@ -1 +1 @@\n-x\n+{RLO}second{PDF}\n"
    )


def _fail_batch_insert(monkeypatch, fail_on):
    """Patch the row insert so the record matched by ``fail_on`` raises."""
    from app import database

    real_insert_row = database._insert_audit_row

    def flaky_insert(conn, *, filename, **kwargs):
        if fail_on(filename):
            raise RuntimeError("simulated insert failure")
        return real_insert_row(conn, filename=filename, **kwargs)

    monkeypatch.setattr(database, "_insert_audit_row", flaky_insert)


# ------------------------------------------------------------- ordering ---

def test_diff_batch_ids_backfilled_in_file_order(client):
    body = client.post(
        "/api/v1/audits/diff", json={"diff": _two_file_diff()}
    ).json()
    assert [item["path"] for item in body["items"]] == ["a.py", "b.py"]
    ids = [item["id"] for item in body["items"]]
    # one transaction, sequential IDs in the order of the diff's files
    assert ids == list(range(ids[0], ids[0] + len(ids)))
    for audit_id in ids:
        assert client.get(f"/api/v1/audits/{audit_id}").status_code == 200


def test_json_batch_ids_backfilled_in_document_order(client):
    doc = '{"a": "x", "b": ["y", "z"]}'
    body = client.post("/api/v1/audits/json", json={"document": doc}).json()
    assert [item["pointer"] for item in body["items"]] == \
        ["/a", "/b/0", "/b/1"]
    ids = [item["id"] for item in body["items"]]
    assert ids == list(range(ids[0], ids[0] + len(ids)))


# ------------------------------------------------------------- rollback ---

def test_diff_insert_failure_rolls_back_whole_batch(client, monkeypatch):
    _fail_batch_insert(monkeypatch, lambda filename: filename == "b.py")
    with pytest.raises(RuntimeError, match="simulated insert failure"):
        client.post("/api/v1/audits/diff", json={"diff": _two_file_diff()})
    # the first file's insert was rolled back together with the failure
    assert client.get("/api/v1/audits").json()["total"] == 0


def test_json_insert_failure_rolls_back_whole_batch(client, monkeypatch):
    _fail_batch_insert(
        monkeypatch, lambda filename: filename == "#/b"
    )
    with pytest.raises(RuntimeError, match="simulated insert failure"):
        client.post(
            "/api/v1/audits/json",
            json={"document": '{"a": "x", "b": "y"}'},
        )
    assert client.get("/api/v1/audits").json()["total"] == 0


def test_batch_failure_does_not_consume_ids(client, monkeypatch):
    # A rolled-back batch leaves no trace: the next successful request
    # continues the ID sequence without a gap from the failed attempt.
    _fail_batch_insert(monkeypatch, lambda filename: filename == "b.py")
    with pytest.raises(RuntimeError):
        client.post("/api/v1/audits/diff", json={"diff": _two_file_diff()})
    monkeypatch.undo()
    body = client.post(
        "/api/v1/audits/diff", json={"diff": _two_file_diff()}
    ).json()
    assert [item["id"] for item in body["items"]] == [1, 2]
    assert client.get("/api/v1/audits").json()["total"] == 2


# ---------------------------------------------------------- empty batch ---

def test_empty_batches_write_nothing(client):
    diff = (
        "diff --git a/d.txt b/d.txt\n--- a/d.txt\n+++ b/d.txt\n"
        "@@ -1 +0,0 @@\n-x\n"
    )
    body = client.post("/api/v1/audits/diff", json={"diff": diff}).json()
    assert body["file_count"] == 0 and body["items"] == []
    body = client.post(
        "/api/v1/audits/json", json={"document": "42"}
    ).json()
    assert body["value_count"] == 0 and body["items"] == []
    assert client.get("/api/v1/audits").json()["total"] == 0


# ------------------------------------------------------ shared data table ---

def test_single_and_batch_records_share_one_table(client):
    single = client.post(
        "/api/v1/audits", json={"text": "plain", "filename": "one.txt"}
    ).json()
    batch = client.post(
        "/api/v1/audits/diff", json={"diff": _two_file_diff()}
    ).json()
    listing = client.get("/api/v1/audits").json()
    assert listing["total"] == 3
    listed_ids = sorted(item["id"] for item in listing["items"])
    assert listed_ids == sorted(
        [single["id"]] + [item["id"] for item in batch["items"]]
    )
    # batch records keep their meta; the single record has none
    detail = client.get(f"/api/v1/audits/{batch['items'][0]['id']}").json()
    assert detail["meta"]["source"] == "unified_diff"
    assert "meta" not in client.get(f"/api/v1/audits/{single['id']}").json()
