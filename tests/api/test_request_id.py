from __future__ import annotations

from .helpers import headers


def test_request_id_is_generated_when_absent(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.headers.get("x-request-id")


def test_inbound_request_id_is_honoured(client):
    resp = client.get("/health", headers={"X-Request-Id": "my-fixed-id-123"})
    assert resp.headers.get("x-request-id") == "my-fixed-id-123"


def test_request_id_present_in_error_body(client):
    resp = client.get(
        "/v1/evidence/ev_doesnotexist",
        headers=headers(**{"X-Request-Id": "err-id-456"}),
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["request_id"] == "err-id-456"
    assert resp.headers.get("x-request-id") == "err-id-456"


def test_request_id_differs_across_requests_when_not_supplied(client):
    r1 = client.get("/health")
    r2 = client.get("/health")
    assert r1.headers["x-request-id"] != r2.headers["x-request-id"]
