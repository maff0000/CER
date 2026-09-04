from __future__ import annotations


def test_missing_contract_version_header_is_rejected(client):
    resp = client.post(
        "/v1/strategies",
        json={"strategy_id": "EMA_PULLBACK", "name": "EMA Pullback", "thesis": "x"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "contract_violation"
    assert "request_id" in body


def test_incompatible_major_contract_version_is_rejected(client):
    resp = client.post(
        "/v1/strategies",
        json={"strategy_id": "EMA_PULLBACK", "name": "EMA Pullback", "thesis": "x"},
        headers={"X-CER-Contract-Version": "99.0.0"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "incompatible_schema_version"


def test_malformed_contract_version_is_rejected(client):
    resp = client.post(
        "/v1/strategies",
        json={"strategy_id": "EMA_PULLBACK", "name": "EMA Pullback", "thesis": "x"},
        headers={"X-CER-Contract-Version": "not-a-semver"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "incompatible_schema_version"


def test_compatible_contract_version_is_accepted(client):
    from .helpers import headers

    resp = client.post(
        "/v1/strategies",
        json={"strategy_id": "EMA_PULLBACK", "name": "EMA Pullback", "thesis": "x"},
        headers=headers(),
    )
    assert resp.status_code == 201
