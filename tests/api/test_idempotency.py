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


# =========================================================================
# Experiments / promotions / health records (R1 audit remediation, C)
# =========================================================================
#
# These three endpoints used to accept an Idempotency-Key, return 201 and
# ignore it: every retry minted a new id and a new row, so one intended
# promotion transition could be recorded three times. The PID's clause
# ("ingestion must tolerate safe retries; duplicate submissions must be
# detectable via idempotency key or equivalent") is not scoped to evidence
# and runs. The key stays OPTIONAL on all three -- omitting it must keep
# behaving exactly as it did.


def _promotion_body(**overrides) -> dict:
    body = {
        "strategy_id": "IDEMP_S",
        "strategy_version": "v1.0.0",
        "from_state": "DRAFT",
        "to_state": "IMPLEMENTED",
        "authority": "PL",
        "producer": "HSA",
        "evidence_ids": ["ev_" + "a" * 32],
        "reason": "Implementation complete.",
    }
    body.update(overrides)
    return body


def _health_body(**overrides) -> dict:
    body = {
        "strategy_id": "IDEMP_S",
        "strategy_version": "v1.0.0",
        "health_state": "HEALTHY",
        "producer": "NEO",
        "reason": "Consistent with baseline.",
        "evidence_ids": ["ev_" + "b" * 32],
    }
    body.update(overrides)
    return body


# --- experiments ---------------------------------------------------------


def test_experiment_idempotent_replay_via_header_returns_same_record(client):
    body = {"objective": "o", "producer": "HSA"}
    hdrs = headers(**{"Idempotency-Key": "exp-key-1"})
    r1 = client.post("/v1/experiments", json=body, headers=hdrs)
    r2 = client.post("/v1/experiments", json=body, headers=hdrs)
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json()["experiment_id"] == r2.json()["experiment_id"]
    assert r1.json() == r2.json()


def test_experiment_idempotent_replay_via_body_field(client):
    body = {"objective": "o", "producer": "HSA", "idempotency_key": "exp-key-body"}
    r1 = client.post("/v1/experiments", json=body, headers=headers())
    r2 = client.post("/v1/experiments", json=body, headers=headers())
    assert r1.json()["experiment_id"] == r2.json()["experiment_id"]


def test_experiment_conflicting_replay_is_409(client):
    hdrs = headers(**{"Idempotency-Key": "exp-key-2"})
    r1 = client.post("/v1/experiments", json={"objective": "o", "producer": "HSA"}, headers=hdrs)
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        "/v1/experiments", json={"objective": "a different objective", "producer": "HSA"}, headers=hdrs
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_experiment_without_a_key_still_mints_a_new_record(client):
    """The key is optional: no key means the previous behaviour."""
    body = {"objective": "o", "producer": "HSA"}
    r1 = client.post("/v1/experiments", json=body, headers=headers())
    r2 = client.post("/v1/experiments", json=body, headers=headers())
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["experiment_id"] != r2.json()["experiment_id"]


def test_experiment_same_key_different_producers_do_not_collide(client):
    hdrs = headers(**{"Idempotency-Key": "exp-shared-key"})
    r1 = client.post("/v1/experiments", json={"objective": "o", "producer": "HSA"}, headers=hdrs)
    r2 = client.post("/v1/experiments", json={"objective": "o", "producer": "NEO"}, headers=hdrs)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["experiment_id"] != r2.json()["experiment_id"]


def test_experiment_header_body_key_mismatch_is_rejected(client):
    resp = client.post(
        "/v1/experiments",
        json={"objective": "o", "producer": "HSA", "idempotency_key": "body-key"},
        headers=headers(**{"Idempotency-Key": "header-key"}),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "contract_violation"


# --- promotions ----------------------------------------------------------


def test_promotion_idempotent_replay_returns_same_transition(client):
    hdrs = headers(**{"Idempotency-Key": "promo-key-1"})
    r1 = client.post("/v1/promotions", json=_promotion_body(), headers=hdrs)
    r2 = client.post("/v1/promotions", json=_promotion_body(), headers=hdrs)
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json()["transition_id"] == r2.json()["transition_id"]
    assert r1.json() == r2.json()


def test_promotion_retried_three_times_records_one_transition(client):
    """The Auditor's reproduction: one intended transition recorded three
    times corrupts the promotion history, which is a first-class PID
    deliverable."""
    hdrs = headers(**{"Idempotency-Key": "promo-retry-key"})
    for _ in range(3):
        resp = client.post("/v1/promotions", json=_promotion_body(), headers=hdrs)
        assert resp.status_code == 201, resp.text

    listed = client.get(
        "/v1/promotions", params={"strategy_id": "IDEMP_S"}, headers=headers()
    )
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_promotion_conflicting_replay_is_409(client):
    hdrs = headers(**{"Idempotency-Key": "promo-key-2"})
    r1 = client.post("/v1/promotions", json=_promotion_body(), headers=hdrs)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/v1/promotions", json=_promotion_body(to_state="RETIRED"), headers=hdrs)
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_promotion_without_a_key_records_every_call(client):
    client.post("/v1/promotions", json=_promotion_body(), headers=headers())
    client.post("/v1/promotions", json=_promotion_body(), headers=headers())
    listed = client.get("/v1/promotions", params={"strategy_id": "IDEMP_S"}, headers=headers())
    assert len(listed.json()) == 2


def test_promotion_same_key_different_producers_do_not_collide(client):
    hdrs = headers(**{"Idempotency-Key": "promo-shared-key"})
    r1 = client.post("/v1/promotions", json=_promotion_body(producer="HSA"), headers=hdrs)
    r2 = client.post("/v1/promotions", json=_promotion_body(producer="NEO"), headers=hdrs)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["transition_id"] != r2.json()["transition_id"]


# --- health records ------------------------------------------------------


def test_health_record_idempotent_replay_returns_same_record(client):
    hdrs = headers(**{"Idempotency-Key": "health-key-1"})
    r1 = client.post("/v1/health-records", json=_health_body(), headers=hdrs)
    r2 = client.post("/v1/health-records", json=_health_body(), headers=hdrs)
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json()["health_id"] == r2.json()["health_id"]
    assert r1.json() == r2.json()

    listed = client.get("/v1/health-records", params={"strategy_id": "IDEMP_S"}, headers=headers())
    assert len(listed.json()) == 1


def test_health_record_conflicting_replay_is_409(client):
    hdrs = headers(**{"Idempotency-Key": "health-key-2"})
    r1 = client.post("/v1/health-records", json=_health_body(), headers=hdrs)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/v1/health-records", json=_health_body(health_state="DEGRADED"), headers=hdrs)
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_health_record_without_a_key_records_every_call(client):
    client.post("/v1/health-records", json=_health_body(), headers=headers())
    client.post("/v1/health-records", json=_health_body(), headers=headers())
    listed = client.get("/v1/health-records", params={"strategy_id": "IDEMP_S"}, headers=headers())
    assert len(listed.json()) == 2


def test_health_record_same_key_different_producers_do_not_collide(client):
    hdrs = headers(**{"Idempotency-Key": "health-shared-key"})
    r1 = client.post("/v1/health-records", json=_health_body(producer="NEO"), headers=hdrs)
    r2 = client.post("/v1/health-records", json=_health_body(producer="APOLLO"), headers=hdrs)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["health_id"] != r2.json()["health_id"]


# --- artifact idempotency ----------------------------------------------
#
# Content-addressing dedups an artifact's BYTES, never its REGISTRATION.
# Before the key was honoured here, two identical POSTs stored one blob
# but returned two artifact_ids, leaving the registry holding two
# indistinguishable records of one logical submission with nothing to tell
# them apart -- the exact "duplicate submissions must be detectable"
# failure the PID forbids, on a required v1 capability.


def _artifact_headers(**extra):
    base = {"Content-Type": "text/plain", "X-CER-Filename": "report.txt"}
    base.update(extra)
    return headers(**base)


def test_artifact_idempotent_replay_returns_same_record(client):
    payload = b"one logical registration, retried"
    hdrs = _artifact_headers(**{"X-CER-Producer": "HSA", "Idempotency-Key": "art-key-1"})

    r1 = client.post("/v1/artifacts", content=payload, headers=hdrs)
    r2 = client.post("/v1/artifacts", content=payload, headers=hdrs)

    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json() == r2.json()
    assert r1.json()["artifact_id"] == r2.json()["artifact_id"]


def test_artifact_retried_three_times_registers_one_artifact(client):
    payload = b"retried three times"
    hdrs = _artifact_headers(**{"X-CER-Producer": "HSA", "Idempotency-Key": "art-key-2"})

    ids = set()
    for _ in range(3):
        resp = client.post("/v1/artifacts", content=payload, headers=hdrs)
        assert resp.status_code == 201, resp.text
        ids.add(resp.json()["artifact_id"])

    assert len(ids) == 1, "one logical registration must yield exactly one artifact_id"

    listed = client.get("/v1/artifacts", headers=headers()).json()
    assert len([a for a in listed if a["artifact_id"] in ids]) == 1


def test_artifact_conflicting_replay_is_409(client):
    hdrs = _artifact_headers(**{"X-CER-Producer": "HSA", "Idempotency-Key": "art-key-3"})
    first = client.post("/v1/artifacts", content=b"original bytes", headers=hdrs)
    assert first.status_code == 201, first.text

    conflicting = client.post("/v1/artifacts", content=b"different bytes entirely", headers=hdrs)
    assert conflicting.status_code == 409, conflicting.text
    assert conflicting.json()["code"] == "idempotency_conflict"


def test_artifact_conflicting_filename_under_same_key_is_409(client):
    payload = b"same bytes, different declared filename"
    first = client.post(
        "/v1/artifacts",
        content=payload,
        headers=_artifact_headers(**{"X-CER-Producer": "HSA", "Idempotency-Key": "art-key-4"}),
    )
    assert first.status_code == 201, first.text

    conflicting = client.post(
        "/v1/artifacts",
        content=payload,
        headers=headers(**{
            "Content-Type": "text/plain",
            "X-CER-Filename": "a_completely_different_name.txt",
            "X-CER-Producer": "HSA",
            "Idempotency-Key": "art-key-4",
        }),
    )
    assert conflicting.status_code == 409, conflicting.text
    assert conflicting.json()["code"] == "idempotency_conflict"


def test_artifact_without_a_key_registers_every_call(client):
    """Omitting the key keeps the previous behaviour exactly."""
    payload = b"no key supplied"
    hdrs = _artifact_headers()
    r1 = client.post("/v1/artifacts", content=payload, headers=hdrs)
    r2 = client.post("/v1/artifacts", content=payload, headers=hdrs)

    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["artifact_id"] != r2.json()["artifact_id"]


def test_artifact_same_key_different_producers_do_not_collide(client):
    """One producer's key choice must never deny or shadow another's."""
    hsa = client.post(
        "/v1/artifacts",
        content=b"HSA's artifact",
        headers=_artifact_headers(**{"X-CER-Producer": "HSA", "Idempotency-Key": "shared-key"}),
    )
    neo = client.post(
        "/v1/artifacts",
        content=b"NEO's completely unrelated artifact",
        headers=_artifact_headers(**{"X-CER-Producer": "NEO", "Idempotency-Key": "shared-key"}),
    )
    assert hsa.status_code == 201, hsa.text
    assert neo.status_code == 201, neo.text
    assert hsa.json()["artifact_id"] != neo.json()["artifact_id"]


def test_artifact_idempotency_key_without_producer_is_400(client):
    resp = client.post(
        "/v1/artifacts",
        content=b"bytes",
        headers=_artifact_headers(**{"Idempotency-Key": "unscoped-key"}),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "contract_violation"


def test_artifact_producer_without_a_key_is_ignored_not_rejected(client):
    """X-CER-Producer alone is harmless -- it only ever scopes a key."""
    resp = client.post(
        "/v1/artifacts",
        content=b"bytes",
        headers=_artifact_headers(**{"X-CER-Producer": "HSA"}),
    )
    assert resp.status_code == 201, resp.text
    # It is not smuggled onto the record: ArtifactRecord has no producer.
    assert "producer" not in resp.json()


def test_artifact_replay_with_a_bad_declared_checksum_is_still_422(client):
    """A used key must not become a way past the integrity check."""
    hdrs = _artifact_headers(**{"X-CER-Producer": "HSA", "Idempotency-Key": "art-key-5"})
    first = client.post("/v1/artifacts", content=b"honest bytes", headers=hdrs)
    assert first.status_code == 201, first.text

    resp = client.post(
        "/v1/artifacts",
        content=b"honest bytes",
        headers=_artifact_headers(**{
            "X-CER-Producer": "HSA",
            "Idempotency-Key": "art-key-5",
            "X-CER-Declared-Sha256": "0" * 64,
        }),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "checksum_mismatch"
