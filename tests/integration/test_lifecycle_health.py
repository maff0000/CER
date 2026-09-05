"""PID acceptance criterion: "Lifecycle / health proof".

Promotion history and STRATEGY_HEALTH records persist and are queryable
against the REAL SQLite metadata store, driven over HTTP:

* promotion transitions across several canonical states (DRAFT ->
  IMPLEMENTED -> BACKTESTED -> VALIDATED -> FORWARD_TEST), each preserving
  strategy/version, from/to state, UTC timestamp, authority/producer,
  supporting evidence references and reason;
* STRATEGY_HEALTH observations across several health states (HEALTHY ->
  WATCH -> DEGRADED -> SUSPENDED), each preserving its evidence-backed
  reason and metrics.

Both are proven to persist and be queryable *in order* through the
real store, not just individually retrievable.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .conftest import headers

STRATEGY_ID = "LIFECYCLE_STRAT"
STRATEGY_VERSION = "v1.0.0"


def _setup_strategy_and_evidence(client) -> str:
    """Register a strategy/version and one real evidence record to use as
    the supporting evidence reference for every promotion/health record
    below."""
    resp = client.post(
        "/v1/strategies",
        json={"strategy_id": STRATEGY_ID, "name": "Lifecycle Strategy", "thesis": "prove lifecycle records"},
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text

    resp = client.post(
        f"/v1/strategies/{STRATEGY_ID}/versions",
        json={
            "strategy_version": STRATEGY_VERSION,
            "git_repo": "git@example.com/strats.git",
            "git_commit": "c" * 40,
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text

    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": "lifecycle-supporting-evidence",
            "strategy_id": STRATEGY_ID,
            "strategy_version": STRATEGY_VERSION,
            "verdict": "PROMISING",
            "metrics": {"sharpe": 1.2},
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["evidence_id"]


def _promote(client, from_state: str, to_state: str, evidence_id: str, *, authority: str, reason: str):
    resp = client.post(
        "/v1/promotions",
        json={
            "strategy_id": STRATEGY_ID,
            "strategy_version": STRATEGY_VERSION,
            "from_state": from_state,
            "to_state": to_state,
            "authority": authority,
            "producer": "HSA",
            "evidence_ids": [evidence_id],
            "reason": reason,
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_promotion_transitions_persist_queryable_and_preserve_required_fields(client):
    evidence_id = _setup_strategy_and_evidence(client)

    before = datetime.now(timezone.utc)
    t1 = _promote(client, "DRAFT", "IMPLEMENTED", evidence_id, authority="PL", reason="code complete")
    t2 = _promote(client, "IMPLEMENTED", "BACKTESTED", evidence_id, authority="HSA", reason="backtest run complete")
    t3 = _promote(client, "BACKTESTED", "VALIDATED", evidence_id, authority="PL", reason="backtest reviewed and accepted")
    t4 = _promote(client, "VALIDATED", "FORWARD_TEST", evidence_id, authority="PL", reason="promoted to forward test")
    after = datetime.now(timezone.utc)

    for t in (t1, t2, t3, t4):
        assert t["strategy_id"] == STRATEGY_ID
        assert t["strategy_version"] == STRATEGY_VERSION
        assert t["evidence_ids"] == [evidence_id]
        at_utc = datetime.fromisoformat(t["at_utc"].replace("Z", "+00:00"))
        assert at_utc.tzinfo is not None
        assert at_utc.utcoffset().total_seconds() == 0  # UTC
        assert before <= at_utc <= after

    assert (t1["from_state"], t1["to_state"]) == ("DRAFT", "IMPLEMENTED")
    assert t1["authority"] == "PL"
    assert t1["reason"] == "code complete"

    assert (t2["from_state"], t2["to_state"]) == ("IMPLEMENTED", "BACKTESTED")
    assert t2["producer"] == "HSA"

    assert (t3["from_state"], t3["to_state"]) == ("BACKTESTED", "VALIDATED")
    assert (t4["from_state"], t4["to_state"]) == ("VALIDATED", "FORWARD_TEST")

    # Queryable, and in the order they were recorded (query returns
    # newest-first per the store's ORDER BY at_utc DESC).
    resp = client.get("/v1/promotions", params={"strategy_id": STRATEGY_ID}, headers=headers())
    assert resp.status_code == 200
    promotions = resp.json()
    transition_ids_in_order = [p["transition_id"] for p in promotions]
    # newest-first: t4, t3, t2, t1
    assert transition_ids_in_order == [t4["transition_id"], t3["transition_id"], t2["transition_id"], t1["transition_id"]]

    resp = client.get(
        "/v1/promotions",
        params={"strategy_id": STRATEGY_ID, "strategy_version": STRATEGY_VERSION},
        headers=headers(),
    )
    assert {p["transition_id"] for p in resp.json()} == {
        t1["transition_id"], t2["transition_id"], t3["transition_id"], t4["transition_id"]
    }


def test_promotion_to_suspended_and_retired_are_recorded(client):
    """Exercise the two terminal/exception states not covered by the happy
    path above -- SUSPENDED and RETIRED are canonical states too."""
    evidence_id = _setup_strategy_and_evidence(client)
    _promote(client, "DRAFT", "IMPLEMENTED", evidence_id, authority="PL", reason="setup")

    suspended = _promote(
        client, "IMPLEMENTED", "SUSPENDED", evidence_id, authority="NEO", reason="drift detected, pausing rollout"
    )
    assert suspended["to_state"] == "SUSPENDED"
    assert suspended["producer"] == "HSA"
    assert suspended["authority"] == "NEO"

    retired = _promote(client, "SUSPENDED", "RETIRED", evidence_id, authority="PL", reason="thesis invalidated")
    assert retired["to_state"] == "RETIRED"


# --- STRATEGY_HEALTH -------------------------------------------------------


def _record_health(client, health_state: str, evidence_id: str, **metrics):
    body = {
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "health_state": health_state,
        "producer": "NEO",
        "reason": f"observed state {health_state}",
        "evidence_ids": [evidence_id],
        **metrics,
    }
    resp = client.post("/v1/health-records", json=body, headers=headers())
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_health_records_across_states_persist_and_are_queryable_in_order(client):
    evidence_id = _setup_strategy_and_evidence(client)

    h1 = _record_health(client, "HEALTHY", evidence_id, observed_trigger_rate=0.12, win_rate=0.55, confidence=0.9)
    h2 = _record_health(client, "WATCH", evidence_id, observed_trigger_rate=0.30, win_rate=0.48, confidence=0.7)
    h3 = _record_health(
        client, "DEGRADED", evidence_id, observed_trigger_rate=0.55, win_rate=0.31, drawdown=-0.18, confidence=0.6
    )
    h4 = _record_health(client, "SUSPENDED", evidence_id, confidence=0.95)

    for h in (h1, h2, h3, h4):
        assert h["strategy_id"] == STRATEGY_ID
        assert h["strategy_version"] == STRATEGY_VERSION
        assert h["evidence_ids"] == [evidence_id]
        assert h["producer"] == "NEO"
        observed_at = datetime.fromisoformat(h["observed_at_utc"].replace("Z", "+00:00"))
        assert observed_at.tzinfo is not None

    assert h1["health_state"] == "HEALTHY"
    assert h1["observed_trigger_rate"] == 0.12
    assert h2["health_state"] == "WATCH"
    assert h3["health_state"] == "DEGRADED"
    assert h3["drawdown"] == -0.18
    assert h4["health_state"] == "SUSPENDED"
    # Metrics NEO didn't supply for h4 are recorded as absent, not defaulted
    # to zero (per the StrategyHealthRecord docstring).
    assert h4["win_rate"] is None
    assert h4["observed_trigger_rate"] is None

    resp = client.get("/v1/health-records", params={"strategy_id": STRATEGY_ID}, headers=headers())
    assert resp.status_code == 200
    records = resp.json()
    ids_in_order = [r["health_id"] for r in records]
    # newest-first
    assert ids_in_order == [h4["health_id"], h3["health_id"], h2["health_id"], h1["health_id"]]

    resp = client.get(
        "/v1/health-records",
        params={"strategy_id": STRATEGY_ID, "strategy_version": STRATEGY_VERSION},
        headers=headers(),
    )
    assert {r["health_id"] for r in resp.json()} == {h["health_id"] for h in (h1, h2, h3, h4)}


def test_health_record_requires_evidence_backed_reason(client):
    """StrategyHealthRecord.evidence_ids has min_length=1 -- CER records
    observations, but every one must cite at least one supporting evidence
    record; NEO's analysis/hypothesis is not itself evidence."""
    resp = client.post(
        "/v1/health-records",
        json={
            "strategy_id": STRATEGY_ID,
            "strategy_version": STRATEGY_VERSION,
            "health_state": "HEALTHY",
            "producer": "NEO",
            "reason": "no evidence cited",
            "evidence_ids": [],
        },
        headers=headers(),
    )
    # Pinned, not "400 or 422": the status and the stable error ``code``
    # are the contract a producer codes against, and a range that accepts
    # either would keep passing if the rejection changed shape.
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "validation_error"
