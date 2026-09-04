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
store's backing directory (metadata) or replaces it with a plain file
(artifacts) -- something no privilege level can paper over.
"""

from __future__ import annotations

import hashlib
import shutil

import pytest
from fastapi.testclient import TestClient

from cer.api.app import create_app
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


# =====================================================================
# Metadata-store failure (genuine)
# =====================================================================


def test_metadata_store_genuinely_unavailable_is_503_on_ready_and_on_write(tmp_path):
    meta_dir = tmp_path / "meta"
    db_path = meta_dir / "cer.db"
    settings = make_settings(tmp_path, metadata_db_path=str(db_path))

    metadata_store = SQLiteMetadataStore(db_path)  # creates meta_dir + applies migrations
    artifact_store = FilesystemArtifactStore(settings.artifact_root, max_bytes=settings.max_artifact_bytes)
    artifact_store.initialise()
    app = create_app(metadata_store, artifact_store, settings)

    assert meta_dir.is_dir()
    # Genuinely destroy the backing storage -- not a permission trick.
    shutil.rmtree(meta_dir)
    assert not meta_dir.exists()

    with TestClient(app, raise_server_exceptions=False) as client:
        # /ready must call health() for real and report which dependency
        # failed -- never cached, never a false positive.
        resp = client.get("/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["failed_dependency"] == "metadata_store"

        # A write attempt must surface a clean 503 CER error, never a raw
        # traceback or an unhandled 500.
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


# =====================================================================
# Artifact-store failure (genuine)
# =====================================================================


def test_artifact_store_genuinely_unavailable_is_503_on_ready_and_on_write(tmp_path):
    settings = make_settings(tmp_path)
    metadata_store = SQLiteMetadataStore(settings.metadata_db_path)
    artifact_root = tmp_path / "cer_artifacts"
    artifact_store = FilesystemArtifactStore(artifact_root, max_bytes=settings.max_artifact_bytes)
    artifact_store.initialise()
    app = create_app(metadata_store, artifact_store, settings)

    assert artifact_root.is_dir()
    # Genuinely destroy the backing storage and replace it with a plain
    # file so neither health() nor put()'s lazy initialise() can succeed --
    # this is not a permission trick (this suite runs as root).
    shutil.rmtree(artifact_root)
    artifact_root.write_bytes(b"not a directory any more")

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
        # Compare the fields the artifact store's own sidecar is
        # authoritative for (identity, checksum, size, content-type,
        # filename) -- NOT run_id/evidence_id here. See
        # test_artifact_single_get_does_not_reflect_evidence_attachment
        # below for a pre-existing (restart-independent) defect in this
        # endpoint's run_id/evidence_id reporting, found by this suite and
        # reported to the PL rather than fixed here.
        after = resp.json()
        for field in ("artifact_id", "sha256", "size_bytes", "content_type", "filename", "uri", "registered_at"):
            assert after[field] == artifact_before[field], field

        # The evidence<->artifact linkage itself (owned by the metadata
        # store, not the artifact store's sidecar) does survive restart --
        # proven via the query endpoint, which is backed by the metadata
        # store.
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


# =====================================================================
# Defect found by this suite (reported to the PL, not fixed here)
# =====================================================================


def test_artifact_single_get_does_not_reflect_evidence_run_attachment(client):
    """DEFECT, found by this real-store suite, reported to the PL rather
    than fixed here (out of this work item's scope; ``src/`` is not
    touched by this dispatch).

    ``GET /v1/artifacts/{artifact_id}`` (``routes.get_artifact_metadata``)
    is served from ``artifact_store.stat()`` -- the filesystem sidecar
    JSON written once by ``FilesystemArtifactStore.put()`` at creation
    time. It is never updated afterwards. But ``run_id``/``evidence_id``
    attachment -- whether set via the ``X-CER-Run-Id``/``X-CER-Evidence-Id``
    headers at creation, or via the separate ``POST
    /v1/artifacts/{id}/attach`` endpoint -- is recorded only in the
    metadata store's ``artifacts`` row, via
    ``metadata_store.register_artifact``/``attach_artifact``.

    The result: the single-artifact-by-id endpoint *always* reports
    ``run_id``/``evidence_id`` as whatever they were at the moment of
    ``put()`` (``None``, unless a future refactor changes that) --
    regardless of any attachment made before or after. Meanwhile
    ``GET /v1/artifacts?run_id=``/``?evidence_id=`` (``query_artifacts``,
    backed by the metadata store) and ``POST .../attach``'s own response
    (also backed by the metadata store) report the attachment correctly.
    Two endpoints of the same contract disagree about the same artifact's
    linkage -- exactly the class of bug this suite exists to catch,
    invisible to any component suite that fakes one store independently of
    the other.

    This test pins today's actual (defective) behaviour so it is visible
    and tracked, not silently reintroduced or silently "fixed" by
    accident. It is NOT a statement that this behaviour is correct.
    """
    resp = client.post(
        "/v1/evidence",
        json={
            "evidence_type": "BACKTEST",
            "schema_version": 1,
            "producer": "HSA",
            "idempotency_key": "defect-artifact-attach-1",
        },
        headers=headers(),
    )
    assert resp.status_code == 201, resp.text
    evidence_id = resp.json()["evidence_id"]

    resp = client.post(
        "/v1/artifacts",
        content=b"defect-repro-bytes",
        headers=headers(**{"X-CER-Filename": "f.txt", "X-CER-Evidence-Id": evidence_id}),
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    artifact_id = created["artifact_id"]
    # The creation response itself correctly reflects the attachment...
    assert created["evidence_id"] == evidence_id

    # ...but the single-item GET does not.
    single_get = client.get(f"/v1/artifacts/{artifact_id}", headers=headers())
    assert single_get.status_code == 200
    assert single_get.json()["evidence_id"] is None  # defect: should be evidence_id

    # ...while the query endpoint (backed by the metadata store, same as
    # the creation response) reports it correctly, proving the two
    # endpoints disagree about the same artifact.
    queried = client.get("/v1/artifacts", params={"evidence_id": evidence_id}, headers=headers())
    assert artifact_id in [a["artifact_id"] for a in queried.json()]
