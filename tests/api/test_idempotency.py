"""Idempotency contract: a safe retry (same key, same body) returns the
stored record; a conflicting retry (same key, different body) is a 409.

Idempotency keys are scoped per producer (see cer.api.routes's module
docstring): two different producers may safely reuse the exact same key
string without colliding. These are the regression tests for that."""

from __future__ import annotations

from .helpers import headers


def _setup_experiment(client):
    client.post(
        "/v1/strategies",
        json={"strategy_id": "IDEMP_S", "name": "n", "thesis": "t"},
        headers=headers(),
    )
    resp = client.post(
        "/v1/experiments",
        json={"objective": "o", "producer": "HSA", "strategy_id": "IDEMP_S"},
        headers=headers(),
    )
    return resp.json()["experiment_id"]


# --- run idempotency ---------------------------------------------------


def test_run_idempotent_replay_same_body_returns_same_run(client):
    experiment_id = _setup_experiment(client)
    body = {"producer": "HSA", "environment": "dev", "idempotency_key": "run-key-1"}

    r1 = client.post(f"/v1/experiments/{experiment_id}/runs", json=body, headers=headers())
    r2 = client.post(f"/v1/experiments/{experiment_id}/runs", json=body, headers=headers())

    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["run_id"] == r2.json()["run_id"]


def test_run_idempotency_key_via_header_matches_body_field(client):
    experiment_id = _setup_experiment(client)
    body = {"producer": "HSA", "environment": "dev"}

    r1 = client.post(
        f"/v1/experiments/{experiment_id}/runs",
        json=body,
        headers=headers(**{"Idempotency-Key": "run-key-hdr"}),
    )
    r2 = client.post(
        f"/v1/experiments/{experiment_id}/runs",
        json=body,
        headers=headers(**{"Idempotency-Key": "run-key-hdr"}),
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["run_id"] == r2.json()["run_id"]


def test_run_idempotency_key_conflict_between_header_and_body_is_rejected(client):
    experiment_id = _setup_experiment(client)
    resp = client.post(
        f"/v1/experiments/{experiment_id}/runs",
        json={"producer": "HSA", "idempotency_key": "body-key"},
        headers=headers(**{"Idempotency-Key": "header-key"}),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "contract_violation"


def test_run_conflicting_replay_is_409_idempotency_conflict(client):
    experiment_id = _setup_experiment(client)
    r1 = client.post(
        f"/v1/experiments/{experiment_id}/runs",
        json={"producer": "HSA", "environment": "dev", "idempotency_key": "run-key-2"},
        headers=headers(),
    )
    assert r1.status_code == 201
    r2 = client.post(
        f"/v1/experiments/{experiment_id}/runs",
        json={"producer": "HSA", "environment": "prod", "idempotency_key": "run-key-2"},
        headers=headers(),
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


# --- evidence idempotency -------------------------------------------------


def _evidence_body(idempotency_key: str, verdict: str = "PROMISING") -> dict:
    return {
        "evidence_type": "BACKTEST",
        "schema_version": 1,
        "producer": "HSA",
        "idempotency_key": idempotency_key,
        "verdict": verdict,
        "metrics": {"sharpe": 1.1},
    }


def test_evidence_idempotent_replay_same_body_returns_same_record(client):
    body = _evidence_body("ev-key-1")
    r1 = client.post("/v1/evidence", json=body, headers=headers())
    r2 = client.post("/v1/evidence", json=body, headers=headers())
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["evidence_id"] == r2.json()["evidence_id"]
    assert r1.json() == r2.json()


def test_evidence_conflicting_replay_is_409_idempotency_conflict(client):
    r1 = client.post("/v1/evidence", json=_evidence_body("ev-key-2", verdict="PROMISING"), headers=headers())
    assert r1.status_code == 201
    r2 = client.post("/v1/evidence", json=_evidence_body("ev-key-2", verdict="REJECTED"), headers=headers())
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_evidence_idempotency_key_via_header_only(client):
    body = {
        "evidence_type": "BACKTEST",
        "schema_version": 1,
        "producer": "HSA",
    }
    r1 = client.post(
        "/v1/evidence", json=body, headers=headers(**{"Idempotency-Key": "ev-key-hdr"})
    )
    r2 = client.post(
        "/v1/evidence", json=body, headers=headers(**{"Idempotency-Key": "ev-key-hdr"})
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["evidence_id"] == r2.json()["evidence_id"]


# --- idempotency keys are scoped per producer (regression) -----------------


def test_evidence_same_key_different_producers_do_not_collide(client):
    """Two unrelated producers happening to pick the same key string must
    both succeed as distinct, independently retrievable records -- this is
    the exact scenario from the PL's bug report (HSA vs NEO both using
    "2026-09-04-btc-sweep")."""
    shared_key = "2026-09-04-btc-sweep"
    hsa_body = {
        "evidence_type": "BACKTEST",
        "schema_version": 1,
        "producer": "HSA",
        "idempotency_key": shared_key,
        "verdict": "PROMISING",
    }
    neo_body = {
        "evidence_type": "NEO_OBSERVATION",
        "schema_version": 1,
        "producer": "NEO",
        "idempotency_key": shared_key,
        "notes": {"observation": "unrelated to HSA's record"},
    }

    r1 = client.post("/v1/evidence", json=hsa_body, headers=headers())
    r2 = client.post("/v1/evidence", json=neo_body, headers=headers())

    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    hsa_id = r1.json()["evidence_id"]
    neo_id = r2.json()["evidence_id"]
    assert hsa_id != neo_id

    hsa_fetched = client.get(f"/v1/evidence/{hsa_id}", headers=headers())
    neo_fetched = client.get(f"/v1/evidence/{neo_id}", headers=headers())
    assert hsa_fetched.status_code == 200
    assert hsa_fetched.json()["producer"] == "HSA"
    assert neo_fetched.status_code == 200
    assert neo_fetched.json()["producer"] == "NEO"


def test_evidence_same_producer_same_key_same_body_is_still_idempotent(client):
    shared_key = "2026-09-04-btc-sweep"
    body = {
        "evidence_type": "BACKTEST",
        "schema_version": 1,
        "producer": "HSA",
        "idempotency_key": shared_key,
        "verdict": "PROMISING",
    }
    r1 = client.post("/v1/evidence", json=body, headers=headers())
    r2 = client.post("/v1/evidence", json=body, headers=headers())
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["evidence_id"] == r2.json()["evidence_id"]
    assert r1.json() == r2.json()  # the replay returned the stored record, not a new one


def test_evidence_same_producer_same_key_different_body_is_still_409(client):
    shared_key = "2026-09-04-btc-sweep"
    r1 = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": shared_key,
            "verdict": "PROMISING",
        },
        headers=headers(),
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": shared_key,
            "verdict": "REJECTED",
        },
        headers=headers(),
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_run_same_key_different_producers_do_not_collide(client):
    experiment_id = _setup_experiment(client)
    shared_key = "shared-run-key"
    r1 = client.post(
        f"/v1/experiments/{experiment_id}/runs",
        json={"producer": "HSA", "environment": "dev", "idempotency_key": shared_key},
        headers=headers(),
    )
    r2 = client.post(
        f"/v1/experiments/{experiment_id}/runs",
        json={"producer": "APOLLO", "environment": "prod", "idempotency_key": shared_key},
        headers=headers(),
    )
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json()["run_id"] != r2.json()["run_id"]


def test_evidence_idempotency_key_with_empty_producer_is_400(client):
    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "",
            "idempotency_key": "orphan-key",
        },
        headers=headers(),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "contract_violation"


def test_run_idempotency_key_with_empty_producer_is_400(client):
    experiment_id = _setup_experiment(client)
    resp = client.post(
        f"/v1/experiments/{experiment_id}/runs",
        json={"producer": "", "idempotency_key": "orphan-run-key"},
        headers=headers(),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "contract_violation"
