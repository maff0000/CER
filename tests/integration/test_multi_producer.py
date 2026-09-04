"""PID acceptance criterion: "Multi-producer proof".

Three representative producers -- HSA, APOLLO, NEO -- write distinct valid
records through the same contract without ambiguity, against the REAL
stores (see ``conftest.py``). Each producer gets a genuinely different
evidence shape:

* HSA:    BACKTEST, with full reproducibility provenance (COMPLETE).
* APOLLO: WALK_FORWARD, a different historical-evidence shape with its own
  metrics vocabulary (fold counts, oos_sharpe) -- also COMPLETE.
* NEO:    NEO_OBSERVATION and DRIFT_ALERT, deliberately provenance-light
  (no run/dataset/config context) -- both must land as INCOMPLETE with the
  *specific* missing fields named, never rejected and never silently
  backfilled with a guessed value.

Also proves the idempotency-keys-are-producer-scoped rule from the PID's
multi-producer wording ("without ambiguity"): two different producers may
safely use the exact same idempotency key string without colliding, and
each producer's own retry of its own key is still idempotent.
"""

from __future__ import annotations

from .conftest import headers


def _register_strategy(client, strategy_id: str) -> None:
    resp = client.post(
        "/v1/strategies",
        json={"strategy_id": strategy_id, "name": strategy_id, "thesis": "multi-producer proof fixture"},
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        f"/v1/strategies/{strategy_id}/versions",
        json={"strategy_version": "v1.0.0", "git_repo": "git@example.com/strats.git", "git_commit": "f" * 40},
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text


def _create_experiment_and_run(client, strategy_id: str, producer: str) -> tuple[str, str]:
    """Create a real experiment + run so a "full reproducibility
    provenance" evidence record actually has an experiment_id/run_id to
    reference -- both are part of EvidenceRecord's required-provenance set."""
    resp = client.post(
        "/v1/experiments",
        json={
            "objective": f"{producer} multi-producer proof",
            "producer": producer,
            "strategy_id": strategy_id,
            "strategy_version": "v1.0.0",
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    experiment_id = resp.json()["experiment_id"]

    resp = client.post(
        f"/v1/experiments/{experiment_id}/runs",
        json={"producer": producer, "environment": "dev"},
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["run_id"]
    return experiment_id, run_id


# --- HSA: BACKTEST, full provenance -----------------------------------------


def test_hsa_backtest_full_provenance_is_complete_and_attributed(client):
    _register_strategy(client, "MP_STRAT")
    experiment_id, run_id = _create_experiment_and_run(client, "MP_STRAT", "HSA")

    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": "hsa-mp-backtest-1",
            "strategy_id": "MP_STRAT",
            "strategy_version": "v1.0.0",
            "experiment_id": experiment_id,
            "run_id": run_id,
            "producer_version": "2.0.0",
            "observed_at_utc": "2026-02-01T00:00:00Z",
            "git_repo": "git@example.com/strats.git",
            "git_commit": "f" * 40,
            "dataset_id": "ds1",
            "dataset_version": "1",
            "dataset_ref": "s3://ds1",
            "config_hash": "cfg_" + "a" * 16,
            "config_ref": "configs/hsa.yaml",
            "environment": "dev",
            "instruments": ["EURUSD"],
            "timeframes": ["H1"],
            "metrics": {"sharpe": 1.5},
            "verdict": "PROMISING",
            "status": "COMPLETE",
            "regime_tags": ["trend"],
            "notes": {"engine": "HSA backtester v2"},
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    ev = resp.json()
    assert ev["producer"] == "HSA"
    assert ev["evidence_type"] == "BACKTEST"
    assert ev["provenance_completeness"] == "COMPLETE"
    assert ev["missing_provenance"] == []


# --- APOLLO: WALK_FORWARD, a different shape --------------------------------


def test_apollo_walk_forward_is_distinct_shape_and_attributed(client):
    _register_strategy(client, "MP_STRAT2")
    experiment_id, run_id = _create_experiment_and_run(client, "MP_STRAT2", "APOLLO")

    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "WALK_FORWARD",
            "schema_version": 1,
            "producer": "APOLLO",
            "idempotency_key": "apollo-mp-wf-1",
            "strategy_id": "MP_STRAT2",
            "strategy_version": "v1.0.0",
            "experiment_id": experiment_id,
            "run_id": run_id,
            "producer_version": "0.9.1",
            "observed_at_utc": "2026-02-02T00:00:00Z",
            "git_repo": "git@example.com/strats.git",
            "git_commit": "f" * 40,
            "dataset_id": "ds2",
            "dataset_version": "1",
            "dataset_ref": "s3://ds2",
            "config_hash": "cfg_" + "b" * 16,
            "config_ref": "configs/apollo.yaml",
            "environment": "dev",
            "instruments": ["GBPUSD"],
            "timeframes": ["H4"],
            # APOLLO's own metrics vocabulary -- distinct from HSA's.
            "metrics": {"folds": 6, "oos_sharpe": 0.87, "is_oos_ratio": 0.91},
            "verdict": "NEEDS_MORE_DATA",
            "status": "COMPLETE",
            "regime_tags": ["range"],
            "notes": {"folds_config": "expanding window, 6 folds"},
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    ev = resp.json()
    assert ev["producer"] == "APOLLO"
    assert ev["evidence_type"] == "WALK_FORWARD"
    assert ev["metrics"]["folds"] == 6
    assert ev["provenance_completeness"] == "COMPLETE"


# --- NEO: provenance-light surveillance records -----------------------------


def test_neo_observation_is_incomplete_with_named_missing_fields_not_rejected(client):
    """NEO's surveillance observation carries a strategy reference (it is
    watching a specific strategy/version) but genuinely has no run, no
    dataset, no config, no environment, no git commit -- it isn't
    executing a backtest. This must be accepted, recorded as INCOMPLETE,
    and name exactly which fields are missing -- never rejected, and never
    silently backfilled with a guessed dataset/config identity."""
    _register_strategy(client, "MP_STRAT3")

    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "NEO_OBSERVATION",
            "schema_version": 1,
            "producer": "NEO",
            "idempotency_key": "neo-mp-observation-1",
            "strategy_id": "MP_STRAT3",
            "strategy_version": "v1.0.0",
            "observed_at_utc": "2026-02-03T00:00:00Z",
            "notes": {"observation": "trigger rate elevated vs. baseline"},
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    ev = resp.json()
    assert ev["producer"] == "NEO"
    assert ev["evidence_type"] == "NEO_OBSERVATION"
    assert ev["provenance_completeness"] == "INCOMPLETE"

    # Supplied fields must NOT show up as missing.
    for present in ("strategy_id", "strategy_version", "observed_at_utc", "notes"):
        assert present not in ev["missing_provenance"], ev["missing_provenance"]

    # Genuinely absent fields must be named explicitly, not silently
    # inferred or defaulted.
    for missing in (
        "run_id",
        "experiment_id",
        "dataset_id",
        "dataset_version",
        "dataset_ref",
        "config_hash",
        "config_ref",
        "environment",
        "git_repo",
        "git_commit",
        "producer_version",
        "metrics",
        "verdict",
        "status",
        "instruments",
        "timeframes",
        "regime_tags",
    ):
        assert missing in ev["missing_provenance"], (missing, ev["missing_provenance"])

    # Never silently backfilled: absent fields really are None/empty, not a
    # guessed value.
    assert ev["run_id"] is None
    assert ev["dataset_id"] is None
    assert ev["config_hash"] is None
    assert ev["metrics"] == {}


def test_neo_drift_alert_with_no_dataset_provenance_is_incomplete(client):
    """DRIFT_ALERT with essentially no reproducibility context at all
    (not even a strategy reference) -- the extreme end of "little or no
    dataset provenance". Must still be accepted and recorded, not rejected."""
    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "DRIFT_ALERT",
            "schema_version": 1,
            "producer": "NEO",
            "idempotency_key": "neo-mp-drift-1",
            "observed_at_utc": "2026-02-04T00:00:00Z",
            "metrics": {"z_score": 3.4},
            "notes": {"alert": "trigger-rate drift beyond 3 sigma"},
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    ev = resp.json()
    assert ev["producer"] == "NEO"
    assert ev["evidence_type"] == "DRIFT_ALERT"
    assert ev["provenance_completeness"] == "INCOMPLETE"
    assert ev["strategy_id"] is None
    assert "strategy_id" in ev["missing_provenance"]
    assert "dataset_id" in ev["missing_provenance"]
    assert "environment" in ev["missing_provenance"]
    # metrics/observed_at_utc/notes were supplied -- must not be "missing".
    assert "metrics" not in ev["missing_provenance"]
    assert "observed_at_utc" not in ev["missing_provenance"]
    assert "notes" not in ev["missing_provenance"]


# --- cross-producer independence & idempotency scoping ----------------------


def test_three_producers_are_independently_queryable_and_correctly_attributed(client):
    _register_strategy(client, "MP_ALL")

    ids = {}
    ids["HSA"] = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": "all-hsa-1",
            "strategy_id": "MP_ALL",
            "strategy_version": "v1.0.0",
            "verdict": "PROMISING",
        },
        headers=headers(),
    ).json()["evidence_id"]

    ids["APOLLO"] = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "PARAMETER_SWEEP",
            "schema_version": 1,
            "producer": "APOLLO",
            "idempotency_key": "all-apollo-1",
            "strategy_id": "MP_ALL",
            "strategy_version": "v1.0.0",
            "metrics": {"best_param_set": 1, "n_combinations": 128},
        },
        headers=headers(),
    ).json()["evidence_id"]

    ids["NEO"] = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "NEO_OBSERVATION",
            "schema_version": 1,
            "producer": "NEO",
            "idempotency_key": "all-neo-1",
            "strategy_id": "MP_ALL",
            "strategy_version": "v1.0.0",
        },
        headers=headers(),
    ).json()["evidence_id"]

    assert len(set(ids.values())) == 3  # no id collisions across producers

    # Each is independently queryable and correctly attributed to its own
    # producer -- not conflated with the others.
    all_for_strategy = client.get(
        "/v1/evidence", params={"strategy_id": "MP_ALL", "strategy_version": "v1.0.0"}, headers=headers()
    ).json()
    by_id = {e["evidence_id"]: e for e in all_for_strategy}
    for producer, evidence_id in ids.items():
        assert evidence_id in by_id
        assert by_id[evidence_id]["producer"] == producer


def test_same_idempotency_key_string_across_producers_does_not_collide(client):
    """The exact scenario the PID's multi-producer wording guards against:
    two unrelated producers happening to choose the same natural key (e.g.
    both keying on today's date) must both succeed as distinct records,
    each independently retrievable and correctly attributed -- one
    producer's key can never collide with, or be shadowed by, another's."""
    shared_key = "2026-09-04-daily-sweep"

    hsa_resp = client.post(
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
    apollo_resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "WALK_FORWARD",
            "schema_version": 1,
            "producer": "APOLLO",
            "idempotency_key": shared_key,
            "verdict": "NEEDS_MORE_DATA",
        },
        headers=headers(),
    )
    neo_resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "NEO_OBSERVATION",
            "schema_version": 1,
            "producer": "NEO",
            "idempotency_key": shared_key,
            "notes": {"observation": "unrelated to HSA/APOLLO"},
        },
        headers=headers(),
    )

    for resp in (hsa_resp, apollo_resp, neo_resp):
        assert resp.status_code == 201, resp.text

    hsa_id = hsa_resp.json()["evidence_id"]
    apollo_id = apollo_resp.json()["evidence_id"]
    neo_id = neo_resp.json()["evidence_id"]
    assert len({hsa_id, apollo_id, neo_id}) == 3  # all distinct

    # Each producer's own retry of ITS OWN key (with the identical body)
    # is still idempotent -- returns the same stored record, not a 409 and
    # not a new record.
    hsa_retry = client.post(
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
    assert hsa_retry.status_code == 201
    assert hsa_retry.json()["evidence_id"] == hsa_id
    assert hsa_retry.json() == hsa_resp.json()

    neo_retry = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "NEO_OBSERVATION",
            "schema_version": 1,
            "producer": "NEO",
            "idempotency_key": shared_key,
            "notes": {"observation": "unrelated to HSA/APOLLO"},
        },
        headers=headers(),
    )
    assert neo_retry.status_code == 201
    assert neo_retry.json()["evidence_id"] == neo_id

    # And each is independently fetchable, still correctly attributed.
    for producer, evidence_id in (("HSA", hsa_id), ("APOLLO", apollo_id), ("NEO", neo_id)):
        fetched = client.get(f"/v1/evidence/{evidence_id}", headers=headers())
        assert fetched.status_code == 200
        assert fetched.json()["producer"] == producer
