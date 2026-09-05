"""PID acceptance criterion: "Vertical slice" -- prove end to end on the
running product.

strategy/version reference -> experiment -> run -> BACKTEST evidence ->
artifact registration -> query/retrieval.

Driven through the real HTTP API against the REAL ``SQLiteMetadataStore``
and REAL ``FilesystemArtifactStore`` (see ``conftest.py``) -- not the
in-memory fakes ``tests/api/test_vertical_slice.py`` uses. Every step
asserts real content, not just a status code, and the artifact's bytes are
checked byte-identical on retrieval.
"""

from __future__ import annotations

import hashlib

from .conftest import headers


def test_full_vertical_slice_over_http_against_real_stores(client):
    # 1. strategy / version reference
    resp = client.post(
        "/v1/strategies",
        json={"strategy_id": "EMA_PULLBACK", "name": "EMA Pullback", "thesis": "mean reversion to the 21 EMA"},
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    strategy = resp.json()
    assert strategy["strategy_id"] == "EMA_PULLBACK"
    assert strategy["thesis"] == "mean reversion to the 21 EMA"

    resp = client.post(
        "/v1/strategies/EMA_PULLBACK/versions",
        json={
            "strategy_version": "v1.0.0",
            "git_repo": "git@github.com:example/strats.git",
            "git_commit": "a" * 40,
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    sv = resp.json()
    assert sv["strategy_id"] == "EMA_PULLBACK"
    assert sv["git_commit"] == "a" * 40

    # 2. experiment
    resp = client.post(
        "/v1/experiments",
        json={
            "objective": "validate EMA pullback on EURUSD H1",
            "producer": "HSA",
            "strategy_id": "EMA_PULLBACK",
            "strategy_version": "v1.0.0",
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    experiment = resp.json()
    experiment_id = experiment["experiment_id"]
    assert experiment_id.startswith("exp_")
    assert experiment["strategy_id"] == "EMA_PULLBACK"

    # 3. run
    resp = client.post(
        f"/v1/experiments/{experiment_id}/runs",
        json={
            "producer": "HSA",
            "producer_version": "1.4.2",
            "git_repo": "git@github.com:example/strats.git",
            "git_commit": "a" * 40,
            "dataset_id": "eurusd_h1_2020_2024",
            "dataset_version": "3",
            "dataset_ref": "s3://datasets/eurusd_h1_2020_2024.parquet",
            "config_hash": "cfg_" + "b" * 16,
            "config_ref": "configs/ema_pullback.yaml",
            "environment": "dev",
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    run = resp.json()
    run_id = run["run_id"]
    assert run_id.startswith("run_")
    assert run["experiment_id"] == experiment_id
    assert run["provenance_completeness"] == "COMPLETE"
    assert run["missing_provenance"] == []

    # 4. BACKTEST evidence, with full reproducibility provenance
    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": "hsa-backtest-run-1",
            "strategy_id": "EMA_PULLBACK",
            "strategy_version": "v1.0.0",
            "experiment_id": experiment_id,
            "run_id": run_id,
            "producer_version": "1.4.2",
            "observed_at_utc": "2026-01-01T00:00:00Z",
            "git_repo": "git@github.com:example/strats.git",
            "git_commit": "a" * 40,
            "dataset_id": "eurusd_h1_2020_2024",
            "dataset_version": "3",
            "dataset_ref": "s3://datasets/eurusd_h1_2020_2024.parquet",
            "config_hash": "cfg_" + "b" * 16,
            "config_ref": "configs/ema_pullback.yaml",
            "environment": "dev",
            "instruments": ["EURUSD"],
            "timeframes": ["H1"],
            "metrics": {"sharpe": 1.42, "trades": 318, "win_rate": 0.54},
            "verdict": "PROMISING",
            "status": "COMPLETE",
            "regime_tags": ["trend"],
            "notes": {"observation": "consistent across regimes"},
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    evidence = resp.json()
    evidence_id = evidence["evidence_id"]
    assert evidence_id.startswith("ev_")
    assert evidence["evidence_type"] == "BACKTEST"
    assert evidence["provenance_completeness"] == "COMPLETE"
    assert evidence["missing_provenance"] == []
    assert evidence["metrics"] == {"sharpe": 1.42, "trades": 318, "win_rate": 0.54}

    # 5. artifact registration (raw bytes body over HTTP, real filesystem store)
    payload = b"equity-curve,csv,content\n" + b"1,2,3\n" * 500
    expected_sha = hashlib.sha256(payload).hexdigest()
    resp = client.post(
        "/v1/artifacts",
        content=payload,
        headers=headers(**{
            "Content-Type": "text/csv",
            "X-CER-Filename": "equity_curve.csv",
            "X-CER-Declared-Sha256": expected_sha,
            "X-CER-Run-Id": run_id,
            "X-CER-Evidence-Id": evidence_id,
        }),
    )
    assert resp.status_code == 201, resp.text
    artifact = resp.json()
    artifact_id = artifact["artifact_id"]
    assert artifact_id.startswith("art_")
    assert artifact["run_id"] == run_id
    assert artifact["evidence_id"] == evidence_id
    assert artifact["sha256"] == expected_sha
    assert artifact["size_bytes"] == len(payload)

    # 6. query/retrieval: evidence by strategy/version/run/type/time
    resp = client.get(
        "/v1/evidence",
        params={"strategy_id": "EMA_PULLBACK", "strategy_version": "v1.0.0"},
        headers=headers(),
    )
    assert resp.status_code == 200
    fetched = {e["evidence_id"]: e for e in resp.json()}
    assert evidence_id in fetched
    assert fetched[evidence_id]["run_id"] == run_id

    resp = client.get("/v1/evidence", params={"run_id": run_id}, headers=headers())
    assert evidence_id in [e["evidence_id"] for e in resp.json()]

    resp = client.get("/v1/evidence", params={"experiment_id": experiment_id}, headers=headers())
    assert evidence_id in [e["evidence_id"] for e in resp.json()]

    resp = client.get("/v1/evidence", params={"evidence_type": "BACKTEST"}, headers=headers())
    assert evidence_id in [e["evidence_id"] for e in resp.json()]

    resp = client.get(
        "/v1/evidence",
        params={"since": "2025-12-31T00:00:00Z", "until": "2027-01-01T00:00:00Z"},
        headers=headers(),
    )
    assert evidence_id in [e["evidence_id"] for e in resp.json()]

    # direct get by id
    resp = client.get(f"/v1/evidence/{evidence_id}", headers=headers())
    assert resp.status_code == 200
    assert resp.json() == fetched[evidence_id]

    # 7. query + retrieve the artifact -- bytes must come back byte-identical
    resp = client.get("/v1/artifacts", params={"evidence_id": evidence_id}, headers=headers())
    assert resp.status_code == 200
    assert artifact_id in [a["artifact_id"] for a in resp.json()]

    resp = client.get("/v1/artifacts", params={"run_id": run_id}, headers=headers())
    assert artifact_id in [a["artifact_id"] for a in resp.json()]

    resp = client.get(f"/v1/artifacts/{artifact_id}", headers=headers())
    assert resp.status_code == 200
    assert resp.json()["sha256"] == expected_sha

    resp = client.get(f"/v1/artifacts/{artifact_id}/download", headers=headers())
    assert resp.status_code == 200
    assert resp.content == payload  # byte-identical
    assert hashlib.sha256(resp.content).hexdigest() == expected_sha
    assert resp.headers["content-type"].startswith("text/csv")
    assert 'equity_curve.csv' in resp.headers["content-disposition"]

    # 8. finalise/close the run
    resp = client.post(f"/v1/runs/{run_id}/close", json={"status": "CLOSED"}, headers=headers())
    assert resp.status_code == 200
    closed = resp.json()
    assert closed["status"] == "CLOSED"
    assert closed["run_id"] == run_id
    assert closed["ended_at"] is not None
