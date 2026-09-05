"""CERClient exercised end-to-end against the FastAPI test app (over an
ASGI transport, not a real socket)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cer.client import CERClient
from cer.contract.errors import IdempotencyConflictError, MetadataStoreError, NotFoundError


@pytest.fixture
def cer_client(app):
    # httpx's ASGITransport is async-only and cannot back a synchronous
    # httpx.Client; TestClient is the standard sync bridge for exercising
    # an ASGI app in-process. See CERClient.__init__'s http_client param.
    with TestClient(app) as test_client:
        client = CERClient(http_client=test_client)
        yield client


def test_client_get_version(cer_client):
    info = cer_client.get_version()
    assert info["contract_version"]
    assert info["schema_version"]


def test_client_health_and_ready(cer_client):
    assert cer_client.health()["status"] == "ok"
    assert cer_client.ready()["status"] == "ok"


def test_client_full_flow(cer_client):
    strategy = cer_client.register_strategy("CLIENT_STRAT", "Client Strategy", "thesis text")
    assert strategy.strategy_id == "CLIENT_STRAT"

    sv = cer_client.register_strategy_version(
        "CLIENT_STRAT", "v1.0.0", "git@example.com/repo.git", "c" * 40
    )
    assert sv.strategy_version == "v1.0.0"

    experiment = cer_client.create_experiment(
        "validate client strat", "HSA", strategy_id="CLIENT_STRAT", strategy_version="v1.0.0"
    )
    assert experiment.experiment_id.startswith("exp_")

    run = cer_client.create_run(
        experiment.experiment_id, "HSA", idempotency_key="client-run-1", environment="dev"
    )
    assert run.run_id.startswith("run_")

    # Idempotent replay through the client returns the same run.
    run_replay = cer_client.create_run(
        experiment.experiment_id, "HSA", idempotency_key="client-run-1", environment="dev"
    )
    assert run_replay.run_id == run.run_id

    evidence = cer_client.append_evidence(
        "BACKTEST",
        1,
        "HSA",
        "client-ev-1",
        strategy_id="CLIENT_STRAT",
        strategy_version="v1.0.0",
        run_id=run.run_id,
        experiment_id=experiment.experiment_id,
        metrics={"sharpe": 1.3},
    )
    assert evidence.evidence_id.startswith("ev_")

    artifact = cer_client.register_artifact(
        b"artifact-bytes", "curve.csv", content_type="text/csv", run_id=run.run_id
    )
    assert artifact.artifact_id.startswith("art_")

    fetched = cer_client.get_artifact_metadata(artifact.artifact_id)
    assert fetched.artifact_id == artifact.artifact_id

    data = cer_client.download_artifact(artifact.artifact_id)
    assert data == b"artifact-bytes"

    results = cer_client.query_evidence(run_id=run.run_id)
    assert any(e.evidence_id == evidence.evidence_id for e in results)

    closed = cer_client.close_run(run.run_id)
    assert closed.status.value == "CLOSED"

    promotion = cer_client.record_promotion(
        "CLIENT_STRAT",
        "v1.0.0",
        "DRAFT",
        "BACKTESTED",
        "HSA-authority",
        "HSA",
        [evidence.evidence_id],
        "backtest passed",
    )
    assert promotion.to_state.value == "BACKTESTED"

    health = cer_client.record_health(
        "CLIENT_STRAT",
        "v1.0.0",
        "HEALTHY",
        "NEO",
        "nominal",
        [evidence.evidence_id],
        win_rate=0.55,
    )
    assert health.health_state.value == "HEALTHY"

    promotions = cer_client.query_promotions(strategy_id="CLIENT_STRAT")
    assert len(promotions) >= 1
    healths = cer_client.query_health(strategy_id="CLIENT_STRAT")
    assert len(healths) >= 1


def test_client_register_artifact_is_idempotent_with_a_key(cer_client):
    """The client must actually plumb the key and its producer through.

    Without ``producer`` the server refuses the key (400), so a client
    that sent one and not the other would look like it supported
    idempotency here while never getting it.
    """
    first = cer_client.register_artifact(
        b"client artifact bytes",
        "curve.csv",
        content_type="text/csv",
        producer="HSA",
        idempotency_key="client-art-1",
    )
    replay = cer_client.register_artifact(
        b"client artifact bytes",
        "curve.csv",
        content_type="text/csv",
        producer="HSA",
        idempotency_key="client-art-1",
    )
    assert replay.artifact_id == first.artifact_id

    with pytest.raises(IdempotencyConflictError):
        cer_client.register_artifact(
            b"materially different bytes",
            "curve.csv",
            content_type="text/csv",
            producer="HSA",
            idempotency_key="client-art-1",
        )


def test_client_raises_not_found_error(cer_client):
    with pytest.raises(NotFoundError):
        cer_client.get_evidence("ev_doesnotexist")


def test_client_raises_idempotency_conflict_error(cer_client):
    experiment = cer_client.create_experiment("o", "HSA")
    cer_client.create_run(experiment.experiment_id, "HSA", idempotency_key="dup", environment="dev")
    with pytest.raises(IdempotencyConflictError):
        cer_client.create_run(experiment.experiment_id, "HSA", idempotency_key="dup", environment="prod")


def test_client_raises_metadata_store_error(app, metadata_store):
    metadata_store.simulate_store_error = True
    with TestClient(app) as test_client:
        client = CERClient(http_client=test_client)
        with pytest.raises(MetadataStoreError):
            client.get_evidence("ev_whatever")
