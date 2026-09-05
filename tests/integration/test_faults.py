"""PID acceptance criterion: "Fault / restart proof".

Every failure mode the PID names, proven against the REAL
``SQLiteMetadataStore`` and REAL ``FilesystemArtifactStore`` -- not fakes
with a ``simulate_store_error`` flag flipped by the test itself (that's
what ``tests/api/test_errors.py`` does; this module makes the *real*
backend genuinely unavailable and checks the API degrades correctly).

* duplicate / retried submission;
* metadata-store failure (genuine, not simulated);
* artifact-store failure (genuine, not simulated);
* malformed provenance (unknown evidence type, incompatible contract/schema
  version, naive/non-UTC datetime, unknown extra field);
* checksum mismatch (and that nothing is stored);
* restart with evidence preserved (dispose of the stores, construct new
  instances on the same paths, prove through the API that everything
  written before is still there).

The "genuinely unavailable" tests deliberately avoid filesystem permission
bits (chmod) as the failure mechanism: this suite runs as root in its
sandbox, and root bypasses permission checks entirely, so a chmod-based
"failure" would silently not fail at all. Instead each test removes the
store's backing directory -- something no privilege level can paper over.

The artifact-store failure tests cover BOTH ways the backing can be gone:

* ``removed`` -- the directory is simply deleted. This is the ordinary
  operational case (a volume unmounted, a data directory wiped) and the
  one that matters: an earlier version of this suite tested only the
  variant below, which happened to be the single failure mode that could
  not be papered over by ``put()``'s unconditional lazy ``initialise()``.
  The suite was green while the write path silently re-manufactured a
  vanished store, acknowledged 201 for artifacts that were already lost,
  and flipped ``/ready`` back to 200 over it.
* ``replaced_by_file`` -- the directory is replaced by a regular file, so
  even ``mkdir`` fails. Kept as an additional case, not as the only one.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cer.api.app import create_app
from cer.api.main import _build_stores
from cer.artifacts import FilesystemArtifactStore
from cer.metadata import SQLiteMetadataStore

from .conftest import headers, make_settings


# =====================================================================
# Duplicate / retried submission
# =====================================================================


def test_byte_identical_evidence_retry_returns_stored_record_not_409(client):
    body = {
        "evidence_type": "BACKTEST",
        "schema_version": 1,
        "producer": "HSA",
        "idempotency_key": "fault-dup-1",
        "verdict": "PROMISING",
        "metrics": {"sharpe": 1.1},
    }
    r1 = client.post("/v1/evidence", json=body, headers=headers())
    r2 = client.post("/v1/evidence", json=body, headers=headers())
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json() == r2.json()


def test_materially_different_retry_same_key_is_409(client):
    r1 = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": "fault-dup-2",
            "verdict": "PROMISING",
        },
        headers=headers(),
    )
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": "fault-dup-2",
            "verdict": "REJECTED",  # materially different body, same key
        },
        headers=headers(),
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_byte_identical_artifact_retry_registers_one_artifact(client, artifact_store):
    """Retrying one artifact registration must not produce two records.

    Against the REAL stores: the filesystem store mints a fresh
    artifact_id and writes a fresh sidecar per put(), and content
    addressing only dedups the blob -- so without the idempotency key
    being honoured, this retry produced two indistinguishable
    registrations of the same bytes. The sidecar count is asserted too:
    the replay must be resolved before the bytes ever reach the artifact
    store, or every retry leaves an orphaned sidecar on disk that
    /download would serve but that GET /v1/artifacts/{id} reports as
    unknown.
    """
    payload = b"artifact bytes registered once, submitted twice"
    request_headers = headers(**{
        "Content-Type": "text/plain",
        "X-CER-Filename": "dup.txt",
        "X-CER-Producer": "HSA",
        "Idempotency-Key": "fault-dup-artifact-1",
    })

    r1 = client.post("/v1/artifacts", content=payload, headers=request_headers)
    r2 = client.post("/v1/artifacts", content=payload, headers=request_headers)

    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json() == r2.json()

    artifact_id = r1.json()["artifact_id"]
    listed = client.get("/v1/artifacts", headers=headers()).json()
    assert [a["artifact_id"] for a in listed] == [artifact_id]

    sidecars = [p for p in artifact_store.index_dir.rglob("*.json")]
    assert len(sidecars) == 1, f"a replay must not leave an orphan sidecar: {sidecars}"

    blobs = [p for p in artifact_store.blobs_dir.rglob("*") if p.is_file()]
    assert len(blobs) == 1

    # And the one record is genuinely retrievable.
    download = client.get(f"/v1/artifacts/{artifact_id}/download", headers=headers())
    assert download.status_code == 200
    assert download.content == payload


def test_materially_different_artifact_retry_same_key_is_409_and_stores_nothing(client, artifact_store):
    """A conflicting resubmission must be refused before anything is written."""
    request_headers = headers(**{
        "Content-Type": "text/plain",
        "X-CER-Filename": "dup.txt",
        "X-CER-Producer": "HSA",
        "Idempotency-Key": "fault-dup-artifact-2",
    })
    first = client.post("/v1/artifacts", content=b"the original bytes", headers=request_headers)
    assert first.status_code == 201, first.text

    blobs_before = {p for p in artifact_store.blobs_dir.rglob("*") if p.is_file()}
    sidecars_before = {p for p in artifact_store.index_dir.rglob("*.json")}

    conflicting = client.post(
        "/v1/artifacts", content=b"materially different bytes", headers=request_headers
    )
    assert conflicting.status_code == 409, conflicting.text
    assert conflicting.json()["code"] == "idempotency_conflict"

    assert {p for p in artifact_store.blobs_dir.rglob("*") if p.is_file()} == blobs_before
    assert {p for p in artifact_store.index_dir.rglob("*.json")} == sidecars_before


def test_byte_identical_run_retry_returns_stored_record(client):
    resp = client.post(
        "/v1/experiments",
        json={"objective": "fault dup run", "producer": "HSA"},
        headers=headers(),
    )
    experiment_id = resp.json()["experiment_id"]
    body = {"producer": "HSA", "environment": "dev", "idempotency_key": "fault-dup-run-1"}
    r1 = client.post(f"/v1/experiments/{experiment_id}/runs", json=body, headers=headers())
    r2 = client.post(f"/v1/experiments/{experiment_id}/runs", json=body, headers=headers())
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["run_id"] == r2.json()["run_id"]


def _promotion_body(**overrides) -> dict:
    body = {
        "strategy_id": "FAULT_IDEMP_S",
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
        "strategy_id": "FAULT_IDEMP_S",
        "strategy_version": "v1.0.0",
        "health_state": "HEALTHY",
        "producer": "NEO",
        "reason": "Consistent with baseline.",
        "evidence_ids": ["ev_" + "b" * 32],
    }
    body.update(overrides)
    return body


def test_experiment_retry_with_idempotency_key_returns_stored_record(client):
    """Against the REAL store: POST /v1/experiments used to accept an
    Idempotency-Key, return 201 and ignore it -- a fresh id and a new row
    on every retry."""
    hdrs = headers(**{"Idempotency-Key": "fault-exp-key"})
    body = {"objective": "fault idempotent experiment", "producer": "HSA"}
    r1 = client.post("/v1/experiments", json=body, headers=hdrs)
    r2 = client.post("/v1/experiments", json=body, headers=hdrs)
    assert r1.status_code == 201 and r2.status_code == 201, r2.text
    assert r1.json() == r2.json()


def test_experiment_conflicting_retry_same_key_is_409(client):
    hdrs = headers(**{"Idempotency-Key": "fault-exp-key-2"})
    r1 = client.post("/v1/experiments", json={"objective": "a", "producer": "HSA"}, headers=hdrs)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/v1/experiments", json={"objective": "b", "producer": "HSA"}, headers=hdrs)
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_promotion_retried_three_times_records_one_transition(client):
    """Against the REAL store: the Auditor demonstrated one intended
    promotion transition recorded three times, corrupting the promotion
    history that is itself a first-class PID deliverable."""
    hdrs = headers(**{"Idempotency-Key": "fault-promo-key"})
    responses = [client.post("/v1/promotions", json=_promotion_body(), headers=hdrs) for _ in range(3)]
    for resp in responses:
        assert resp.status_code == 201, resp.text
    assert responses[0].json() == responses[1].json() == responses[2].json()

    listed = client.get("/v1/promotions", params={"strategy_id": "FAULT_IDEMP_S"}, headers=headers())
    assert listed.status_code == 200
    assert len(listed.json()) == 1


def test_promotion_conflicting_retry_same_key_is_409(client):
    hdrs = headers(**{"Idempotency-Key": "fault-promo-key-2"})
    r1 = client.post("/v1/promotions", json=_promotion_body(), headers=hdrs)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/v1/promotions", json=_promotion_body(to_state="RETIRED"), headers=hdrs)
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_health_record_retry_with_idempotency_key_returns_stored_record(client):
    hdrs = headers(**{"Idempotency-Key": "fault-health-key"})
    r1 = client.post("/v1/health-records", json=_health_body(), headers=hdrs)
    r2 = client.post("/v1/health-records", json=_health_body(), headers=hdrs)
    assert r1.status_code == 201 and r2.status_code == 201, r2.text
    assert r1.json() == r2.json()

    listed = client.get(
        "/v1/health-records", params={"strategy_id": "FAULT_IDEMP_S"}, headers=headers()
    )
    assert len(listed.json()) == 1


def test_health_record_conflicting_retry_same_key_is_409(client):
    hdrs = headers(**{"Idempotency-Key": "fault-health-key-2"})
    r1 = client.post("/v1/health-records", json=_health_body(), headers=hdrs)
    assert r1.status_code == 201, r1.text
    r2 = client.post("/v1/health-records", json=_health_body(health_state="DEGRADED"), headers=hdrs)
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_optional_key_omitted_still_creates_a_record_on_every_call(client):
    """The key is optional on all three: omitting it must behave exactly
    as it did before -- a fresh id and a new row."""
    e1 = client.post("/v1/experiments", json={"objective": "o", "producer": "HSA"}, headers=headers())
    e2 = client.post("/v1/experiments", json={"objective": "o", "producer": "HSA"}, headers=headers())
    assert e1.json()["experiment_id"] != e2.json()["experiment_id"]

    client.post("/v1/promotions", json=_promotion_body(strategy_id="FAULT_OPT_S"), headers=headers())
    client.post("/v1/promotions", json=_promotion_body(strategy_id="FAULT_OPT_S"), headers=headers())
    listed = client.get("/v1/promotions", params={"strategy_id": "FAULT_OPT_S"}, headers=headers())
    assert len(listed.json()) == 2

    client.post("/v1/health-records", json=_health_body(strategy_id="FAULT_OPT_S"), headers=headers())
    client.post("/v1/health-records", json=_health_body(strategy_id="FAULT_OPT_S"), headers=headers())
    listed = client.get("/v1/health-records", params={"strategy_id": "FAULT_OPT_S"}, headers=headers())
    assert len(listed.json()) == 2


def test_safe_retry_of_strategy_registration_is_not_an_immutability_error(client):
    """A producer that re-sends an identical registration (letting the
    server stamp created_at both times) is retrying, not conflicting with
    stored history -- 201 with the stored record, not 409."""
    body = {"strategy_id": "RETRY_STRAT", "name": "Retry", "thesis": "t"}
    r1 = client.post("/v1/strategies", json=body, headers=headers())
    r2 = client.post("/v1/strategies", json=body, headers=headers())
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert r1.json() == r2.json()  # the stored record, including its original created_at

    version_body = {"strategy_version": "v1.0.0", "git_repo": "git@x:y.git", "git_commit": "a" * 40}
    v1 = client.post("/v1/strategies/RETRY_STRAT/versions", json=version_body, headers=headers())
    v2 = client.post("/v1/strategies/RETRY_STRAT/versions", json=version_body, headers=headers())
    assert v1.status_code == 201, v1.text
    assert v2.status_code == 201, v2.text
    assert v1.json() == v2.json()

    # A genuine content change is still refused.
    conflict = client.post(
        "/v1/strategies",
        json={"strategy_id": "RETRY_STRAT", "name": "Retry", "thesis": "a different thesis"},
        headers=headers(),
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "immutability_violation"

    version_conflict = client.post(
        "/v1/strategies/RETRY_STRAT/versions",
        json={"strategy_version": "v1.0.0", "git_repo": "git@x:y.git", "git_commit": "b" * 40},
        headers=headers(),
    )
    assert version_conflict.status_code == 409
    assert version_conflict.json()["code"] == "immutability_violation"


def test_idempotent_replays_are_recognised_after_restart(tmp_path):
    """The key and its content fingerprint live in the database, so a
    retry that arrives after a restart is still recognised as a replay --
    not recorded a second time."""
    settings = make_settings(tmp_path)
    store1 = SQLiteMetadataStore(settings.metadata_db_path)
    art1 = FilesystemArtifactStore(settings.artifact_root, max_bytes=settings.max_artifact_bytes)
    art1.initialise()
    hdrs = headers(**{"Idempotency-Key": "restart-promo-key"})

    with TestClient(create_app(store1, art1, settings), raise_server_exceptions=False) as c1:
        before = c1.post("/v1/promotions", json=_promotion_body(), headers=hdrs)
        assert before.status_code == 201, before.text
    store1.close()

    store2 = SQLiteMetadataStore(settings.metadata_db_path)
    art2 = FilesystemArtifactStore(settings.artifact_root, max_bytes=settings.max_artifact_bytes)
    art2.initialise()
    try:
        with TestClient(create_app(store2, art2, settings), raise_server_exceptions=False) as c2:
            after = c2.post("/v1/promotions", json=_promotion_body(), headers=hdrs)
            assert after.status_code == 201, after.text
            assert after.json() == before.json()

            listed = c2.get(
                "/v1/promotions", params={"strategy_id": "FAULT_IDEMP_S"}, headers=headers()
            )
            assert len(listed.json()) == 1
    finally:
        store2.close()


# =====================================================================
# Metadata-store failure (genuine)
# =====================================================================


def test_metadata_store_genuinely_unavailable_is_503_on_ready_and_on_write(tmp_path):
    """The real ordering: connections are established first, the failure
    arrives later.

    This test used to construct its ``TestClient`` *after* destroying the
    store directory, so the serving thread had no cached connection and
    failed cleanly on its first attempt to open one. That is the reverse
    of what happens on a running service, and it let a real defect pass a
    green suite: on the live container, writes carried on being
    acknowledged ``201 Created`` after the metadata directory was deleted
    (SQLite keeps writing to an open file descriptor whose inode has been
    unlinked), and every one of those records was gone at the next
    restart. ``/ready`` told the truth throughout; the write path never
    asked.

    So: build the app and client, write successfully so a connection is
    genuinely established and cached, *then* destroy the backing
    directory, and only then assert.
    """
    meta_dir = tmp_path / "meta"
    db_path = meta_dir / "cer.db"
    settings = make_settings(tmp_path, metadata_db_path=str(db_path))

    metadata_store = SQLiteMetadataStore(db_path)  # creates meta_dir + applies migrations
    artifact_store = FilesystemArtifactStore(settings.artifact_root, max_bytes=settings.max_artifact_bytes)
    artifact_store.initialise()
    app = create_app(metadata_store, artifact_store, settings)
    assert meta_dir.is_dir()

    with TestClient(app, raise_server_exceptions=False) as client:
        # --- connections first --------------------------------------
        assert client.get("/ready").status_code == 200
        established = client.post(
            "/v1/evidence",
            json={
                "evidence_type": "BACKTEST",
                "schema_version": 1,
                "producer": "HSA",
                "idempotency_key": "before-the-volume-vanished",
                "verdict": "PROMISING",
            },
            headers=headers(),
        )
        assert established.status_code == 201, established.text

        # Also establish a cached connection on *this* thread, so the
        # "the process could have gone on writing" assertion below is
        # deterministic rather than depending on which worker thread the
        # app happened to serve the request from.
        assert metadata_store.query_evidence(limit=10)

        # --- failure later ------------------------------------------
        # Genuinely destroy the backing storage -- not a permission trick.
        shutil.rmtree(meta_dir)
        assert not meta_dir.exists()

        # The cached connection really is still alive on the now-unlinked
        # inode: a read through it still succeeds. So the refusals below
        # are the write path's own durable-backing check, not an
        # incidental "could not open the database file".
        assert metadata_store.query_evidence(limit=10)

        # /ready must call health() for real and report which dependency
        # failed -- never cached, never a false positive.
        resp = client.get("/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["failed_dependency"] == "metadata_store"

        # A write attempt must surface a clean 503 CER error, never a raw
        # traceback or an unhandled 500 -- and above all never a 201 for a
        # record that is already lost.
        resp2 = client.post(
            "/v1/strategies",
            json={"strategy_id": "FAULT_META", "name": "n", "thesis": "t"},
            headers=headers(),
        )
        assert resp2.status_code == 503, resp2.text
        body2 = resp2.json()
        assert body2["code"] == "metadata_store_error"
        assert "request_id" in body2
        # A clean structured 503, not a raw traceback -- CERError messages
        # are allowed to describe *what* failed (unlike the bare-Exception
        # 500 path, which must leak nothing -- see test_unexpected_error
        # coverage in tests/api/test_errors.py).
        assert "traceback" not in resp2.text.lower()

        # The durable write producers actually care about is refused too,
        # and refused the same way.
        resp3 = client.post(
            "/v1/evidence",
            json={
                "evidence_type": "BACKTEST",
                "schema_version": 1,
                "producer": "HSA",
                "idempotency_key": "after-the-volume-vanished",
                "verdict": "PROMISING",
            },
            headers=headers(),
        )
        assert resp3.status_code == 503, resp3.text
        assert resp3.json()["code"] == "metadata_store_error"

    metadata_store.close()


def test_write_refused_after_backing_loss_leaves_no_phantom_record(tmp_path):
    """A write refused because the backing vanished must leave nothing
    behind: restore the store, restart, and the refused submission must be
    absent while everything written before the failure is still there.

    This is the other half of the acknowledged-then-lost defect. A 503 is
    only honest if it really means "not recorded".
    """
    meta_dir = tmp_path / "meta"
    db_path = meta_dir / "cer.db"
    backup_dir = tmp_path / "meta_backup"
    settings = make_settings(tmp_path, metadata_db_path=str(db_path))

    metadata_store_1 = SQLiteMetadataStore(db_path)
    artifact_store_1 = FilesystemArtifactStore(settings.artifact_root, max_bytes=settings.max_artifact_bytes)
    artifact_store_1.initialise()
    app1 = create_app(metadata_store_1, artifact_store_1, settings)

    survivor_key = "phantom-test-survivor"
    phantom_key = "phantom-test-refused"

    with TestClient(app1, raise_server_exceptions=False) as client1:
        resp = client1.post(
            "/v1/evidence",
            json={
                "evidence_type": "BACKTEST",
                "schema_version": 1,
                "producer": "HSA",
                "idempotency_key": survivor_key,
                "strategy_id": "PHANTOM_STRAT",
                "verdict": "PROMISING",
            },
            headers=headers(),
        )
        assert resp.status_code == 201, resp.text
        survivor_id = resp.json()["evidence_id"]

        # Snapshot the intact store, then destroy it under the live process.
        shutil.copytree(meta_dir, backup_dir)
        shutil.rmtree(meta_dir)

        refused = client1.post(
            "/v1/evidence",
            json={
                "evidence_type": "BACKTEST",
                "schema_version": 1,
                "producer": "HSA",
                "idempotency_key": phantom_key,
                "strategy_id": "PHANTOM_STRAT",
                "verdict": "REJECTED",
            },
            headers=headers(),
        )
        assert refused.status_code == 503, refused.text
        assert refused.json()["code"] == "metadata_store_error"

    metadata_store_1.close()

    # --- restore the backing and restart -----------------------------
    shutil.copytree(backup_dir, meta_dir)
    metadata_store_2 = SQLiteMetadataStore(db_path)
    artifact_store_2 = FilesystemArtifactStore(settings.artifact_root, max_bytes=settings.max_artifact_bytes)
    artifact_store_2.initialise()
    app2 = create_app(metadata_store_2, artifact_store_2, settings)

    try:
        with TestClient(app2, raise_server_exceptions=False) as client2:
            assert client2.get("/ready").status_code == 200

            listed = client2.get(
                "/v1/evidence", params={"strategy_id": "PHANTOM_STRAT"}, headers=headers()
            )
            assert listed.status_code == 200
            keys = {e["idempotency_key"] for e in listed.json()}
            assert survivor_key in keys, "the write made before the failure must survive"
            assert phantom_key not in keys, "the refused write must not have been recorded"

            assert client2.get(f"/v1/evidence/{survivor_id}", headers=headers()).status_code == 200
    finally:
        metadata_store_2.close()


# =====================================================================
# Artifact-store failure (genuine)
# =====================================================================


def _destroy_artifact_backing(artifact_root, mode: str) -> None:
    """Genuinely destroy the artifact store's backing storage.

    ``removed``: the directory is simply gone -- the ordinary case.
    ``replaced_by_file``: a regular file sits where the directory was, so
    even ``mkdir`` fails. Neither is a permission trick (this suite runs
    as root, which bypasses permission bits entirely).
    """
    shutil.rmtree(artifact_root)
    if mode == "replaced_by_file":
        artifact_root.write_bytes(b"not a directory any more")
    elif mode != "removed":  # pragma: no cover - guards a typo in a param id
        raise AssertionError(f"unknown destruction mode {mode!r}")


@pytest.mark.parametrize("mode", ["removed", "replaced_by_file"])
def test_artifact_store_genuinely_unavailable_is_503_on_ready_and_on_write(tmp_path, mode):
    """Both failure modes: /ready is 503 AND the write is refused.

    ``removed`` is the case that used to pass only by accident. Before the
    write path separated first-time initialisation from silent
    re-creation, ``put()`` called ``initialise()`` unconditionally, so a
    simply-deleted root was recreated by the very next write: the write
    was acknowledged 201 and ``/ready`` then reported 200 over a store
    that had lost everything registered in it.
    """
    settings = make_settings(tmp_path)
    metadata_store = SQLiteMetadataStore(settings.metadata_db_path)
    artifact_root = tmp_path / "cer_artifacts"
    artifact_store = FilesystemArtifactStore(artifact_root, max_bytes=settings.max_artifact_bytes)
    artifact_store.initialise()
    app = create_app(metadata_store, artifact_store, settings)

    assert artifact_root.is_dir()
    _destroy_artifact_backing(artifact_root, mode)

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/ready")
            assert resp.status_code == 503
            assert resp.json()["failed_dependency"] == "artifact_store"

            resp2 = client.post(
                "/v1/artifacts",
                content=b"some bytes",
                headers=headers(**{"X-CER-Filename": "f.txt"}),
            )
            assert resp2.status_code == 503, resp2.text
            body2 = resp2.json()
            assert body2["code"] == "artifact_store_error"
            assert "request_id" in body2
            text = resp2.text.lower()
            assert "traceback" not in text

            # The refused write must not have repaired readiness by
            # re-manufacturing the store behind the caller's back.
            resp3 = client.get("/ready")
            assert resp3.status_code == 503, resp3.text
            assert resp3.json()["failed_dependency"] == "artifact_store"
            if mode == "removed":
                assert not artifact_root.exists(), (
                    "the refused write must not have recreated the artifact root"
                )
    finally:
        metadata_store.close()


def test_registered_artifact_after_store_vanishes_is_503_not_404(tmp_path):
    """A vanished store is a server-side failure, not an unknown id.

    The artifact IS registered -- the metadata store still says so. Only
    the bulk storage is gone. Reporting 404 ("artifact_id ... is not
    registered") sends the producer to debug an id that was never the
    problem and hides a live operational fault behind a caller-side
    status.
    """
    settings = make_settings(tmp_path)
    metadata_store = SQLiteMetadataStore(settings.metadata_db_path)
    artifact_root = tmp_path / "cer_artifacts"
    artifact_store = FilesystemArtifactStore(artifact_root, max_bytes=settings.max_artifact_bytes)
    artifact_store.initialise()
    app = create_app(metadata_store, artifact_store, settings)

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            registered = client.post(
                "/v1/artifacts",
                content=b"evidence registered before the volume vanishes",
                headers=headers(**{"X-CER-Filename": "before.txt"}),
            )
            assert registered.status_code == 201, registered.text
            artifact_id = registered.json()["artifact_id"]

            assert client.get(f"/v1/artifacts/{artifact_id}/download", headers=headers()).status_code == 200

            _destroy_artifact_backing(artifact_root, "removed")

            gone = client.get(f"/v1/artifacts/{artifact_id}/download", headers=headers())
            assert gone.status_code == 503, gone.text
            assert gone.json()["code"] == "artifact_store_error"

            # An id that genuinely never existed is a different question,
            # but with the backing gone the honest answer is still "the
            # store is unavailable", not a confident 404.
            unknown = client.get("/v1/artifacts/art_" + "0" * 32 + "/download", headers=headers())
            assert unknown.status_code == 503, unknown.text

            assert not artifact_root.exists()
    finally:
        metadata_store.close()


# =====================================================================
# Malformed provenance
# =====================================================================


def test_unknown_evidence_type_fails_loudly(client):
    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "NOT_A_REAL_TYPE",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": "fault-unknown-type",
        },
        headers=headers(),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "unknown_evidence_type"


def test_incompatible_schema_version_on_evidence_body_fails_loudly(client):
    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 999,
            "producer": "HSA",
            "idempotency_key": "fault-bad-schema",
        },
        headers=headers(),
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "incompatible_schema_version"


def test_incompatible_contract_version_header_fails_loudly(client):
    resp = client.post(
        "/v1/strategies",
        json={"strategy_id": "FAULT_CV", "name": "n", "thesis": "t"},
        headers={"X-CER-Contract-Version": "99.0.0"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "incompatible_schema_version"


def test_naive_non_utc_datetime_fails_loudly(client):
    """observed_at_utc with no timezone offset at all -- CER never guesses
    a timezone on a caller's behalf."""
    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": "fault-naive-dt",
            "observed_at_utc": "2026-01-01T00:00:00",  # naive: no Z, no offset
        },
        headers=headers(),
    )
    assert resp.status_code in (400, 422)
    body = resp.json()
    assert body["code"] == "validation_error"


def test_unknown_extra_field_fails_loudly(client):
    """Every API model forbids unknown fields -- a typo'd or invented field
    must fail loudly, never be silently dropped."""
    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": "fault-extra-field",
            "totally_made_up_field": "should never be accepted",
        },
        headers=headers(),
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "validation_error"


def test_malformed_provenance_never_creates_a_record(client):
    """None of the malformed-provenance rejections above should leave a
    partial record behind -- confirm the rejected idempotency keys never
    became real evidence."""
    for key in ("fault-unknown-type", "fault-bad-schema", "fault-naive-dt", "fault-extra-field"):
        resp = client.get("/v1/evidence", params={}, headers=headers())
        ids = {e["idempotency_key"] for e in resp.json()}
        assert key not in ids


# =====================================================================
# Checksum mismatch
# =====================================================================


def test_checksum_mismatch_fails_loudly_and_stores_nothing(client, artifact_store):
    payload = b"real artifact content for checksum test"
    wrong_sha = "0" * 64
    assert hashlib.sha256(payload).hexdigest() != wrong_sha

    blobs_before = sorted(p for p in artifact_store.blobs_dir.rglob("*") if p.is_file())
    index_before = sorted(artifact_store.index_dir.glob("*.json"))

    resp = client.post(
        "/v1/artifacts",
        content=payload,
        headers=headers(**{
            "Content-Type": "text/plain",
            "X-CER-Filename": "checksum_test.txt",
            "X-CER-Declared-Sha256": wrong_sha,
        }),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "checksum_mismatch"

    # Nothing written: neither a new blob nor a new sidecar.
    blobs_after = sorted(p for p in artifact_store.blobs_dir.rglob("*") if p.is_file())
    index_after = sorted(artifact_store.index_dir.glob("*.json"))
    assert blobs_after == blobs_before
    assert index_after == index_before

    # And specifically: the real content's digest path was never created.
    real_digest = hashlib.sha256(payload).hexdigest()
    real_blob_path = artifact_store.blobs_dir / real_digest[0:2] / real_digest[2:4] / real_digest
    assert not real_blob_path.exists()


# =====================================================================
# Restart with evidence preserved
# =====================================================================


def test_restart_preserves_evidence_and_artifact_bytes_through_the_api(tmp_path):
    settings = make_settings(tmp_path)

    metadata_store_1 = SQLiteMetadataStore(settings.metadata_db_path)
    artifact_store_1 = FilesystemArtifactStore(settings.artifact_root, max_bytes=settings.max_artifact_bytes)
    artifact_store_1.initialise()
    app1 = create_app(metadata_store_1, artifact_store_1, settings)

    payload = b"restart-proof artifact bytes: " + b"x" * 1000
    expected_sha = hashlib.sha256(payload).hexdigest()

    with TestClient(app1, raise_server_exceptions=False) as client1:
        resp = client1.post(
            "/v1/strategies",
            json={"strategy_id": "RESTART_STRAT", "name": "n", "thesis": "t"},
            headers=headers(),
        )
        assert resp.status_code == 201, resp.text

        resp = client1.post(
            "/v1/evidence",
            json={
                "evidence_type": "BACKTEST",
                "schema_version": 1,
                "producer": "HSA",
                "idempotency_key": "restart-evidence-1",
                "strategy_id": "RESTART_STRAT",
                "verdict": "PROMISING",
                "metrics": {"sharpe": 1.05},
            },
            headers=headers(),
        )
        assert resp.status_code == 201, resp.text
        evidence_before = resp.json()
        evidence_id = evidence_before["evidence_id"]

        resp = client1.post(
            "/v1/artifacts",
            content=payload,
            headers=headers(**{
                "Content-Type": "application/octet-stream",
                "X-CER-Filename": "restart.bin",
                "X-CER-Declared-Sha256": expected_sha,
                "X-CER-Evidence-Id": evidence_id,
            }),
        )
        assert resp.status_code == 201, resp.text
        artifact_before = resp.json()
        artifact_id = artifact_before["artifact_id"]

    # --- dispose of the stores ------------------------------------------
    metadata_store_1.close()
    del metadata_store_1, artifact_store_1, app1

    # --- construct new store instances on the same paths ("restart") ----
    metadata_store_2 = SQLiteMetadataStore(settings.metadata_db_path)
    artifact_store_2 = FilesystemArtifactStore(settings.artifact_root, max_bytes=settings.max_artifact_bytes)
    artifact_store_2.initialise()  # idempotent -- must not disturb existing data
    app2 = create_app(metadata_store_2, artifact_store_2, settings)

    with TestClient(app2, raise_server_exceptions=False) as client2:
        resp = client2.get("/ready")
        assert resp.status_code == 200, resp.text

        resp = client2.get(f"/v1/evidence/{evidence_id}", headers=headers())
        assert resp.status_code == 200, resp.text
        assert resp.json() == evidence_before

        resp = client2.get(
            "/v1/evidence", params={"strategy_id": "RESTART_STRAT"}, headers=headers()
        )
        assert evidence_id in [e["evidence_id"] for e in resp.json()]

        resp = client2.get(f"/v1/artifacts/{artifact_id}", headers=headers())
        assert resp.status_code == 200
        # The single-artifact GET is served from the metadata store (the
        # authoritative source for lineage), so it must reflect everything
        # -- identity, checksum, size, content-type, filename, AND the
        # evidence_id attachment made before restart -- byte-for-byte, the
        # same way the evidence restart check above does.
        assert resp.json() == artifact_before

        # The evidence<->artifact linkage itself (owned by the metadata
        # store, not the artifact store's sidecar) does survive restart --
        # proven via the query endpoint, which is backed by the metadata
        # store, and agrees with the single-artifact GET above.
        resp = client2.get("/v1/artifacts", params={"evidence_id": evidence_id}, headers=headers())
        assert artifact_id in [a["artifact_id"] for a in resp.json()]

        resp = client2.get(f"/v1/artifacts/{artifact_id}/download", headers=headers())
        assert resp.status_code == 200
        assert resp.content == payload  # byte-identical after restart
        assert hashlib.sha256(resp.content).hexdigest() == expected_sha

        # New writes against the reopened store also work (not read-only).
        resp = client2.post(
            "/v1/evidence",
            json={
                "evidence_type": "BACKTEST",
                "schema_version": 1,
                "producer": "HSA",
                "idempotency_key": "restart-evidence-2-after-reopen",
                "strategy_id": "RESTART_STRAT",
                "verdict": "PROMISING",
            },
            headers=headers(),
        )
        assert resp.status_code == 201, resp.text

    metadata_store_2.close()


def test_restart_onto_a_lost_artifact_volume_is_503_not_a_lying_200(tmp_path):
    """Restart durability across TWO separate volumes (R5-RESTART-DURABILITY).

    ``docker-compose.yml`` backs the metadata DB and the artifact root
    with two independent named volumes. Lose only the artifact volume
    while the service is stopped and, before this fix, startup's
    unconditional ``artifact_store.initialise()`` re-manufactured an
    empty artifact store: ``/ready`` reported 200, ``GET
    /v1/artifacts/{id}`` still asserted the artifact existed (its
    metadata was on the intact volume) and only the download 404-ed —
    registered evidence, silently unretrievable, behind a green
    readiness probe.

    Driven through ``cer.api.main._build_stores`` — the actual container
    startup path — rather than by calling ``initialise()`` directly, so
    it proves the decision the entrypoint makes and not just the store's
    own guard.
    """
    settings = make_settings(tmp_path)
    artifact_root = Path(settings.artifact_root)
    payload = b"registered evidence that the artifact volume then loses"

    metadata_store_1, artifact_store_1 = _build_stores(settings)
    app1 = create_app(metadata_store_1, artifact_store_1, settings)
    with TestClient(app1, raise_server_exceptions=False) as client1:
        assert client1.get("/ready").status_code == 200

        resp = client1.post(
            "/v1/artifacts",
            content=payload,
            headers=headers(**{"X-CER-Filename": "before-volume-loss.bin"}),
        )
        assert resp.status_code == 201, resp.text
        artifact_before = resp.json()
        artifact_id = artifact_before["artifact_id"]

        assert client1.get(f"/v1/artifacts/{artifact_id}/download", headers=headers()).status_code == 200

    metadata_store_1.close()
    del metadata_store_1, artifact_store_1, app1

    # --- lose ONLY the artifact volume, while the service is stopped ----
    _destroy_artifact_backing(artifact_root, "removed")

    # --- start again, exactly as the container entrypoint does -----------
    metadata_store_2, artifact_store_2 = _build_stores(settings)
    app2 = create_app(metadata_store_2, artifact_store_2, settings)
    try:
        with TestClient(app2, raise_server_exceptions=False) as client2:
            # The whole point: readiness tells the truth.
            ready = client2.get("/ready")
            assert ready.status_code == 503, ready.text
            assert ready.json()["failed_dependency"] == "artifact_store"

            # Startup did not re-create the artifact root behind our back.
            assert not artifact_root.exists(), (
                "startup re-created the artifact root over a lost volume"
            )

            # Writes are refused, and refusing does not repair readiness by
            # lazily initialising an empty store.
            write = client2.post(
                "/v1/artifacts",
                content=b"a write that must not be acknowledged",
                headers=headers(**{"X-CER-Filename": "after-volume-loss.bin"}),
            )
            assert write.status_code == 503, write.text
            assert write.json()["code"] == "artifact_store_error"
            assert not artifact_root.exists()
            assert client2.get("/ready").status_code == 503

            # The lost artifact reads as a server-side storage failure, not
            # a 404 blaming an artifact_id that was never the problem.
            download = client2.get(f"/v1/artifacts/{artifact_id}/download", headers=headers())
            assert download.status_code == 503, download.text
            assert download.json()["code"] == "artifact_store_error"

            # ...and its metadata is still readable, so an operator can see
            # exactly which evidence the lost volume took with it.
            meta = client2.get(f"/v1/artifacts/{artifact_id}", headers=headers())
            assert meta.status_code == 200, meta.text
            assert meta.json() == artifact_before
    finally:
        metadata_store_2.close()


def test_restart_with_both_volumes_intact_stays_ready(tmp_path):
    """The control for the test above: the same startup path, nothing
    destroyed. ``/ready`` is 200 and the artifact is still retrievable —
    the new startup check must not make an ordinary restart unready."""
    settings = make_settings(tmp_path)
    payload = b"ordinary restart, both volumes intact"

    metadata_store_1, artifact_store_1 = _build_stores(settings)
    app1 = create_app(metadata_store_1, artifact_store_1, settings)
    with TestClient(app1, raise_server_exceptions=False) as client1:
        resp = client1.post(
            "/v1/artifacts",
            content=payload,
            headers=headers(**{"X-CER-Filename": "intact.bin"}),
        )
        assert resp.status_code == 201, resp.text
        artifact_id = resp.json()["artifact_id"]
    metadata_store_1.close()
    del metadata_store_1, artifact_store_1, app1

    metadata_store_2, artifact_store_2 = _build_stores(settings)
    app2 = create_app(metadata_store_2, artifact_store_2, settings)
    try:
        with TestClient(app2, raise_server_exceptions=False) as client2:
            assert client2.get("/ready").status_code == 200
            download = client2.get(f"/v1/artifacts/{artifact_id}/download", headers=headers())
            assert download.status_code == 200
            assert download.content == payload
    finally:
        metadata_store_2.close()


# =====================================================================
# Lineage: single-artifact GET reflects evidence/run attachment
# =====================================================================


def test_artifact_single_get_reflects_evidence_run_attachment(client):
    """Fixed defect, found by this real-store suite: ``GET
    /v1/artifacts/{artifact_id}`` (``routes.get_artifact_metadata``) used
    to be served from ``artifact_store.stat()`` -- the filesystem sidecar
    JSON written once by ``FilesystemArtifactStore.put()`` at creation
    time and never updated afterwards. ``run_id``/``evidence_id``
    attachment is recorded only in the metadata store's ``artifacts`` row
    (via ``metadata_store.register_artifact``/``attach_artifact``), so the
    single-artifact-by-id endpoint permanently reported stale linkage
    while ``GET /v1/artifacts?evidence_id=``/``?run_id=`` and ``POST
    .../attach``'s own response (both backed by the metadata store)
    reported it correctly. Two endpoints of the same contract disagreed
    about the same artifact's linkage.

    ``get_artifact_metadata`` now reads the authoritative ``ArtifactRecord``
    from the metadata store instead, so it reflects attachment made either
    at registration (``X-CER-Run-Id``/``X-CER-Evidence-Id`` headers) or
    later (``POST /v1/artifacts/{id}/attach``). This test proves both
    paths.
    """
    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": "lineage-artifact-attach-1",
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    evidence_id = resp.json()["evidence_id"]

    # --- path 1: attachment supplied at registration (header) -----------
    resp = client.post(
        "/v1/artifacts",
        content=b"lineage-repro-bytes-at-registration",
        headers=headers(**{"X-CER-Filename": "f.txt", "X-CER-Evidence-Id": evidence_id}),
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    artifact_id = created["artifact_id"]
    # The creation response itself correctly reflects the attachment...
    assert created["evidence_id"] == evidence_id

    # ...and now the single-item GET agrees with it.
    single_get = client.get(f"/v1/artifacts/{artifact_id}", headers=headers())
    assert single_get.status_code == 200
    assert single_get.json()["evidence_id"] == evidence_id

    # ...and so does the query endpoint -- all three views now agree.
    queried = client.get("/v1/artifacts", params={"evidence_id": evidence_id}, headers=headers())
    assert artifact_id in [a["artifact_id"] for a in queried.json()]

    # --- path 2: attachment applied later, via POST .../attach ----------
    resp = client.post(
        "/v1/experiments",
        json={"objective": "lineage attach-later", "producer": "HSA"},
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    experiment_id = resp.json()["experiment_id"]
    resp = client.post(
        f"/v1/experiments/{experiment_id}/runs",
        json={"producer": "HSA"},
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["run_id"]

    resp = client.post(
        "/v1/artifacts",
        content=b"lineage-repro-bytes-unattached",
        headers=headers(**{"X-CER-Filename": "g.txt"}),
    )
    assert resp.status_code == 201, resp.text
    unattached_artifact_id = resp.json()["artifact_id"]
    assert resp.json()["run_id"] is None

    # Before attaching, the single GET correctly reports no linkage yet.
    before_attach = client.get(f"/v1/artifacts/{unattached_artifact_id}", headers=headers())
    assert before_attach.status_code == 200
    assert before_attach.json()["run_id"] is None

    resp = client.post(
        f"/v1/artifacts/{unattached_artifact_id}/attach",
        json={"run_id": run_id},
        headers=headers(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["run_id"] == run_id

    # The single-item GET now reflects the later attachment too.
    after_attach = client.get(f"/v1/artifacts/{unattached_artifact_id}", headers=headers())
    assert after_attach.status_code == 200
    assert after_attach.json()["run_id"] == run_id


def test_three_views_of_artifact_lineage_agree(client):
    """The registration response, the single-item GET, and the query-by-
    evidence_id endpoint must all report the same lineage for the same
    artifact -- the exact three-way disagreement this defect produced."""
    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": "lineage-three-views-1",
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    evidence_id = resp.json()["evidence_id"]

    resp = client.post(
        "/v1/experiments",
        json={"objective": "lineage three views", "producer": "HSA"},
        headers=headers(),
    )
    experiment_id = resp.json()["experiment_id"]
    resp = client.post(
        f"/v1/experiments/{experiment_id}/runs",
        json={"producer": "HSA"},
        headers=headers(),
    )
    run_id = resp.json()["run_id"]

    resp = client.post(
        "/v1/artifacts",
        content=b"three-views-bytes",
        headers=headers(**{
            "X-CER-Filename": "three_views.txt",
            "X-CER-Evidence-Id": evidence_id,
            "X-CER-Run-Id": run_id,
        }),
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    artifact_id = created["artifact_id"]
    assert created["evidence_id"] == evidence_id
    assert created["run_id"] == run_id

    single_get = client.get(f"/v1/artifacts/{artifact_id}", headers=headers())
    assert single_get.status_code == 200

    queried = client.get("/v1/artifacts", params={"evidence_id": evidence_id}, headers=headers())
    assert queried.status_code == 200
    [queried_record] = [a for a in queried.json() if a["artifact_id"] == artifact_id]

    for record in (created, single_get.json(), queried_record):
        assert record["evidence_id"] == evidence_id
        assert record["run_id"] == run_id


# =====================================================================
# Download (bytes path) integrity -- unaffected by the lineage fix
# =====================================================================


def test_download_still_byte_identical_and_fails_loudly_on_corrupted_blob(client, artifact_store):
    """The fix to ``GET /v1/artifacts/{id}`` changes only the metadata
    path. ``/download`` must still be served from the artifact store,
    still return byte-identical content, and still refuse to serve a
    corrupted blob -- proving the integrity path was not regressed."""
    payload = b"download-integrity-check-bytes " + b"y" * 500
    expected_sha = hashlib.sha256(payload).hexdigest()

    resp = client.post(
        "/v1/artifacts",
        content=payload,
        headers=headers(**{"X-CER-Filename": "integrity.bin"}),
    )
    assert resp.status_code == 201, resp.text
    artifact_id = resp.json()["artifact_id"]
    assert resp.json()["sha256"] == expected_sha

    resp = client.get(f"/v1/artifacts/{artifact_id}/download", headers=headers())
    assert resp.status_code == 200
    assert resp.content == payload  # byte-identical
    assert hashlib.sha256(resp.content).hexdigest() == expected_sha

    # Now corrupt the blob on disk directly (bypassing the API entirely --
    # the same mechanism test_checksum_mismatch_fails_loudly_and_stores_nothing
    # uses to reach into the real filesystem store).
    blob_path = artifact_store.blobs_dir / expected_sha[0:2] / expected_sha[2:4] / expected_sha
    assert blob_path.is_file()
    blob_path.write_bytes(b"corrupted-on-disk" + payload)

    # The metadata path (this work item's fix) is unaffected by blob
    # corruption -- it never reads bytes.
    resp = client.get(f"/v1/artifacts/{artifact_id}", headers=headers())
    assert resp.status_code == 200
    assert resp.json()["sha256"] == expected_sha

    # But /download must still refuse to serve the corrupted bytes.
    resp = client.get(f"/v1/artifacts/{artifact_id}/download", headers=headers())
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "checksum_mismatch"
