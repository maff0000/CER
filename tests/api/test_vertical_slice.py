"""The PID's vertical-slice acceptance criterion, proven over HTTP:

strategy/version -> experiment -> run -> BACKTEST evidence -> artifact
registration -> query/retrieval.
"""

from __future__ import annotations

from .helpers import headers


def test_full_vertical_slice_over_http(client):
    # 1. strategy / version
    resp = client.post(
        "/v1/strategies",
        json={"strategy_id": "EMA_PULLBACK", "name": "EMA Pullback", "thesis": "mean reversion"},
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text

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
    assert run["provenance_completeness"] == "COMPLETE"

    # 4. BACKTEST evidence
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
    assert evidence["provenance_completeness"] == "COMPLETE"

    # 5. artifact registration (raw bytes body)
    payload = b"equity-curve,csv,content\n1,2,3\n"
    resp = client.post(
        "/v1/artifacts",
        content=payload,
        headers=headers(**{
            "Content-Type": "text/csv",
            "X-CER-Filename": "equity_curve.csv",
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
    assert len(artifact["sha256"]) == 64

    # 6. query evidence by strategy/version/run/type/time
    resp = client.get(
        "/v1/evidence",
        params={"strategy_id": "EMA_PULLBACK", "strategy_version": "v1.0.0"},
        headers=headers(),
    )
    assert resp.status_code == 200
    ids = [e["evidence_id"] for e in resp.json()]
    assert evidence_id in ids

    resp = client.get("/v1/evidence", params={"run_id": run_id}, headers=headers())
    assert evidence_id in [e["evidence_id"] for e in resp.json()]

    resp = client.get("/v1/evidence", params={"evidence_type": "BACKTEST"}, headers=headers())
    assert evidence_id in [e["evidence_id"] for e in resp.json()]

    # 7. query + retrieve the artifact
    resp = client.get("/v1/artifacts", params={"evidence_id": evidence_id}, headers=headers())
    assert resp.status_code == 200
    assert artifact_id in [a["artifact_id"] for a in resp.json()]

    resp = client.get(f"/v1/artifacts/{artifact_id}/download", headers=headers())
    assert resp.status_code == 200
    assert resp.content == payload
    assert resp.headers["content-type"].startswith("text/csv")

    # 8. finalise/close the run
    resp = client.post(f"/v1/runs/{run_id}/close", json={"status": "CLOSED"}, headers=headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "CLOSED"
