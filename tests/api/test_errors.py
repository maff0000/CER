"""Exhaustive exercise of cer.api.errors's exception -> HTTP mapping.

Every case asserts both the HTTP status code and the structured body's
stable ``code`` (never string-matching a message).
"""

from __future__ import annotations

from .fakes import BOOM_PRODUCER
from .helpers import headers


def _create_strategy(client, strategy_id="S1"):
    resp = client.post(
        "/v1/strategies",
        json={"strategy_id": strategy_id, "name": "n", "thesis": "t"},
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_experiment(client, strategy_id="S1"):
    resp = client.post(
        "/v1/experiments",
        json={"objective": "o", "producer": "HSA", "strategy_id": strategy_id},
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_run(client, experiment_id, **kwargs):
    body = {"producer": "HSA"}
    body.update(kwargs)
    resp = client.post(f"/v1/experiments/{experiment_id}/runs", json=body, headers=headers())
    return resp


# --- 404 not_found ---------------------------------------------------------


def test_get_unknown_evidence_is_404(client):
    resp = client.get("/v1/evidence/ev_doesnotexist", headers=headers())
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "not_found"
    assert "request_id" in body


def test_close_unknown_run_is_404(client):
    resp = client.post("/v1/runs/run_doesnotexist/close", json={"status": "CLOSED"}, headers=headers())
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_attach_unknown_artifact_is_404(client):
    resp = client.post(
        "/v1/artifacts/art_doesnotexist/attach", json={"evidence_id": None, "run_id": None}, headers=headers()
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_download_unknown_artifact_is_404(client):
    resp = client.get("/v1/artifacts/art_doesnotexist/download", headers=headers())
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


# --- 400 contract violation family -----------------------------------------


def test_unknown_evidence_type_is_rejected_loudly(client):
    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "NOT_A_REAL_EVIDENCE_TYPE",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": "k1",
        },
        headers=headers(),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_evidence_type"


def test_incompatible_schema_version_is_rejected_loudly(client):
    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 999,
            "producer": "HSA",
            "idempotency_key": "k1",
        },
        headers=headers(),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "incompatible_schema_version"


def test_missing_idempotency_key_on_evidence_is_rejected(client):
    resp = client.post(
        "/v1/evidence",
        json={"evidence_type": "BACKTEST", "schema_version": 1, "producer": "HSA"},
        headers=headers(),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "contract_violation"


def test_missing_artifact_filename_header_is_rejected(client):
    resp = client.post("/v1/artifacts", content=b"data", headers=headers())
    assert resp.status_code == 400
    assert resp.json()["code"] == "contract_violation"


def test_malformed_request_body_is_422_validation_error(client):
    # 'name' is required on StrategyCreateRequest.
    resp = client.post(
        "/v1/strategies", json={"strategy_id": "S1", "thesis": "t"}, headers=headers()
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert "request_id" in body


def test_bad_identity_shape_is_rejected(client):
    resp = client.post(
        "/v1/strategies",
        json={"strategy_id": "not valid! id", "name": "n", "thesis": "t"},
        headers=headers(),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "identity_error"


# --- 409 immutability / idempotency conflict --------------------------------


def test_conflicting_idempotent_run_replay_is_409(client):
    strategy = _create_strategy(client, "S_CONFLICT")
    experiment = _create_experiment(client, strategy["strategy_id"])
    r1 = _create_run(client, experiment["experiment_id"], idempotency_key="dup-key", environment="dev")
    assert r1.status_code == 201, r1.text
    r2 = _create_run(client, experiment["experiment_id"], idempotency_key="dup-key", environment="prod")
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_re_registering_artifact_with_different_attachment_is_immutability_conflict(client):
    payload = b"same-bytes-both-times"
    r1 = client.post(
        "/v1/artifacts",
        content=payload,
        headers=headers(**{"Content-Type": "text/plain", "X-CER-Filename": "f.txt"}),
    )
    assert r1.status_code == 201, r1.text

    # Same content (same hash-addressed artifact_id) but now attached to a
    # run_id -- a materially different record for the same immutable id.
    r2 = client.post(
        "/v1/artifacts",
        content=payload,
        headers=headers(**{
            "Content-Type": "text/plain",
            "X-CER-Filename": "f.txt",
            "X-CER-Run-Id": "run_" + "0" * 32,
        }),
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["code"] == "immutability_violation"


# --- 422 checksum mismatch --------------------------------------------------


def test_checksum_mismatch_is_422(client):
    resp = client.post(
        "/v1/artifacts",
        content=b"real content",
        headers=headers(**{
            "Content-Type": "text/plain",
            "X-CER-Filename": "f.txt",
            "X-CER-Declared-Sha256": "0" * 64,
        }),
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "checksum_mismatch"


# --- 503 dependency unavailable ---------------------------------------------


def test_metadata_store_failure_is_503(client, metadata_store):
    metadata_store.simulate_store_error = True
    resp = client.get("/v1/evidence/ev_x", headers=headers())
    assert resp.status_code == 503
    assert resp.json()["code"] == "metadata_store_error"


def test_artifact_store_failure_is_503(client, artifact_store):
    artifact_store.simulate_store_error = True
    resp = client.get("/v1/artifacts/art_x/download", headers=headers())
    assert resp.status_code == 503
    assert resp.json()["code"] == "artifact_store_error"


# --- 500 unexpected exception: no traceback/path leaked ---------------------


def test_unexpected_exception_is_500_and_leaks_nothing(client):
    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": BOOM_PRODUCER,
            "idempotency_key": "boom-1",
        },
        headers=headers(),
    )
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "internal_error"
    assert "request_id" in body
    text = resp.text.lower()
    assert "traceback" not in text
    assert "/srv/" not in text
    assert ".py" not in text
