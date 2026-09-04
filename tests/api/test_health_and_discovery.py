from __future__ import annotations

from cer.contract.version import CONTRACT_VERSION, SCHEMA_VERSION


def test_version_endpoint_requires_no_contract_header(client):
    # A producer must be able to discover the server's version *before*
    # it knows what to send as its own X-CER-Contract-Version.
    resp = client.get("/v1/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["contract_version"] == CONTRACT_VERSION
    assert body["schema_version"] == SCHEMA_VERSION
    assert SCHEMA_VERSION in body["supported_schema_versions"]


def test_liveness_is_always_ok_and_cheap(client, metadata_store, artifact_store):
    metadata_store.healthy = False
    artifact_store.healthy = False
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readiness_ok_when_both_stores_healthy(client):
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readiness_503_when_metadata_store_unhealthy(client, metadata_store):
    metadata_store.healthy = False
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["failed_dependency"] == "metadata_store"


def test_readiness_503_when_artifact_store_unhealthy(client, artifact_store):
    artifact_store.healthy = False
    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["failed_dependency"] == "artifact_store"


def test_readiness_is_not_cached(client, metadata_store):
    # Flip health between two calls; readiness must reflect the current
    # state each time, never a cached prior result.
    resp1 = client.get("/ready")
    assert resp1.status_code == 200

    metadata_store.healthy = False
    resp2 = client.get("/ready")
    assert resp2.status_code == 503

    metadata_store.healthy = True
    resp3 = client.get("/ready")
    assert resp3.status_code == 200
