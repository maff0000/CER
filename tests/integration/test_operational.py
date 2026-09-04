"""PID acceptance criterion: "Operational proof" -- the in-process portion.

The container-level startup and ``docker compose`` restart proof belongs
to another Engineer (containerisation work item) and is out of scope here.
This module proves:

* structured JSON logs are emitted, with UTC timestamps, during real HTTP
  request handling against the real app/middleware (``cer.api.app``'s
  ``_request_context`` middleware) -- captured and parsed, not merely
  unit-tested against a hand-called logger (that's ``tests/runtime/test_logging.py``,
  which never touches the actual request path);
* persisted records and registered artifacts surviving a store restart is
  proven end to end in ``test_faults.py::test_restart_preserves_evidence_and_artifact_bytes_through_the_api``
  -- referenced here rather than duplicated.
"""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from cer.api.app import create_app
from cer.runtime.logging import configure_logging

from .conftest import headers, make_settings


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """Isolate the root logger's handlers/level around each test in this
    module, mirroring tests/runtime/test_logging.py's fixture -- this
    package must not leak a JSON handler onto the root logger for the rest
    of the test session, and must not inherit one from another module."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    for h in original_handlers:
        root.removeHandler(h)
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in original_handlers:
        root.addHandler(h)
    root.setLevel(original_level)


def test_real_request_handling_emits_structured_utc_json_logs(tmp_path):
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    settings = make_settings(tmp_path)
    from cer.artifacts import FilesystemArtifactStore
    from cer.metadata import SQLiteMetadataStore

    metadata_store = SQLiteMetadataStore(settings.metadata_db_path)
    artifact_store = FilesystemArtifactStore(settings.artifact_root, max_bytes=settings.max_artifact_bytes)
    artifact_store.initialise()
    app = create_app(metadata_store, artifact_store, settings)

    before = datetime.now(timezone.utc)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/strategies",
            json={"strategy_id": "LOG_PROOF", "name": "n", "thesis": "t"},
            headers=headers(**{"X-Request-Id": "log-proof-request-1"}),
        )
    after = datetime.now(timezone.utc)
    assert resp.status_code == 201, resp.text

    lines = [ln for ln in stream.getvalue().splitlines() if ln.strip()]
    assert lines, "expected at least one log line to be emitted during request handling"

    records = [json.loads(ln) for ln in lines]  # every line must parse as JSON
    request_records = [r for r in records if r.get("logger") == "cer.api.request"]
    assert request_records, f"no 'cer.api.request' log record found among: {records!r}"

    record = next(r for r in request_records if r.get("request_id") == "log-proof-request-1")

    # Required shape (see cer.runtime.logging.JsonFormatter._build_payload).
    for key in ("ts", "level", "logger", "msg", "module", "line"):
        assert key in record, (key, record)

    # The request-context fields the middleware adds via extra=... are
    # merged at the top level, not nested.
    assert record["method"] == "POST"
    assert record["path"] == "/v1/strategies"
    assert record["status"] == 201
    assert isinstance(record["duration_ms"], (int, float))
    assert record["duration_ms"] >= 0

    # ts is UTC (Z or +00:00 offset) and parses to a real instant that
    # falls within the request's actual wall-clock window.
    ts = record["ts"]
    assert ts.endswith("Z") or ts.endswith("+00:00")
    normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    parsed = datetime.fromisoformat(normalized)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    assert before <= parsed <= after

    metadata_store.close()


def test_multiple_real_requests_each_get_their_own_log_line(tmp_path):
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    settings = make_settings(tmp_path)
    from cer.artifacts import FilesystemArtifactStore
    from cer.metadata import SQLiteMetadataStore

    metadata_store = SQLiteMetadataStore(settings.metadata_db_path)
    artifact_store = FilesystemArtifactStore(settings.artifact_root, max_bytes=settings.max_artifact_bytes)
    artifact_store.initialise()
    app = create_app(metadata_store, artifact_store, settings)

    with TestClient(app) as client:
        client.get("/health")
        client.get("/v1/version")
        client.get("/ready")

    lines = [ln for ln in stream.getvalue().splitlines() if ln.strip()]
    records = [json.loads(ln) for ln in lines]
    request_records = [r for r in records if r.get("logger") == "cer.api.request"]
    paths_logged = [r["path"] for r in request_records]
    assert paths_logged.count("/health") == 1
    assert paths_logged.count("/v1/version") == 1
    assert paths_logged.count("/ready") == 1
    # Distinct request ids (none supplied) -- each request minted its own.
    request_ids = {r["request_id"] for r in request_records}
    assert len(request_ids) == len(request_records)

    metadata_store.close()
