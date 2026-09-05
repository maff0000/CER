"""Tests for cer.api.main — the container entrypoint.

Covers the two things W3-DOCKER fixed in ``main.py``:

* startup fails loudly (clear message, non-zero exit) on missing required
  configuration, rather than a raw traceback into uvicorn internals;
* the real ``MetadataStore``/``ArtifactStore`` backends are wired with the
  correct class names and constructor arguments, and the artifact store is
  actually ``initialise()``-d before the app would start serving (the
  ``/ready`` 503-on-fresh-deployment bug the PID called out).

and, added by R5-RESTART-DURABILITY, the startup decision about *whether*
initialising the artifact store is safe at all: with two separate named
volumes behind the metadata DB and the artifact root, a restart onto a
lost artifact volume looks exactly like a first deployment from the
artifact volume alone, and must not be papered over by re-creating an
empty store.

Deliberately does not start uvicorn or touch Docker/the network — see
``_load_settings_or_exit``/``_build_stores`` in ``cer.api.main``, which
exist specifically to make this testable without either.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from cer.api.main import (
    EVENT_ARTIFACT_STORE_FIRST_DEPLOYMENT,
    EVENT_ARTIFACT_STORE_VOLUME_LOST,
    _build_stores,
    _initialise_artifact_store_if_safe,
    _load_settings_or_exit,
)
from cer.artifacts import FilesystemArtifactStore
from cer.contract.errors import ArtifactStoreError, MetadataStoreError
from cer.contract.models import ArtifactRecord
from cer.metadata import SQLiteMetadataStore
from cer.runtime.config import DEFAULT_MAX_ARTIFACT_BYTES


def _required_env(tmp_path) -> dict:
    return {
        "CER_METADATA_DB_PATH": str(tmp_path / "metadata" / "cer.db"),
        "CER_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
    }


# --- _load_settings_or_exit ------------------------------------------------


def test_missing_metadata_db_path_exits_loudly(capsys, tmp_path):
    env = _required_env(tmp_path)
    del env["CER_METADATA_DB_PATH"]

    with pytest.raises(SystemExit) as excinfo:
        _load_settings_or_exit(env)

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "configuration error" in err
    assert "CER_METADATA_DB_PATH" in err


def test_missing_artifact_root_exits_loudly(capsys, tmp_path):
    env = _required_env(tmp_path)
    del env["CER_ARTIFACT_ROOT"]

    with pytest.raises(SystemExit) as excinfo:
        _load_settings_or_exit(env)

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "configuration error" in err
    assert "CER_ARTIFACT_ROOT" in err


def test_missing_config_error_never_reaches_stdout_as_a_traceback(capsys, tmp_path):
    """The failure must be a clean SystemExit + stderr message, not an
    unhandled exception that would print a Python traceback (which is what
    reaching uvicorn/ASGI internals with a bad Settings object looks like)."""
    env = {}  # nothing set at all

    with pytest.raises(SystemExit):
        _load_settings_or_exit(env)

    out, err = capsys.readouterr()
    assert out == ""
    assert "Traceback" not in err


def test_present_required_env_loads_successfully(tmp_path):
    env = _required_env(tmp_path)

    settings = _load_settings_or_exit(env)

    assert settings.metadata_db_path == env["CER_METADATA_DB_PATH"]
    assert settings.artifact_root == env["CER_ARTIFACT_ROOT"]
    assert settings.max_artifact_bytes == DEFAULT_MAX_ARTIFACT_BYTES


# --- _build_stores -----------------------------------------------------


def test_build_stores_uses_the_real_backend_classes(tmp_path):
    settings = _load_settings_or_exit(_required_env(tmp_path))

    metadata_store, artifact_store = _build_stores(settings)

    # The PL-integration bug this work item fixed: main.py previously
    # guessed "SqliteMetadataStore" (wrong casing) and constructed
    # FilesystemArtifactStore without the required max_bytes argument.
    assert isinstance(metadata_store, SQLiteMetadataStore)
    assert isinstance(artifact_store, FilesystemArtifactStore)
    assert artifact_store.max_bytes == settings.max_artifact_bytes


def test_build_stores_initialises_the_artifact_store(tmp_path):
    """health() deliberately never creates anything (see fs_store.py) — if
    _build_stores forgot to call initialise(), health() would raise on a
    fresh root exactly as /ready did before this fix (503,
    failed_dependency: artifact_store, on every clean deployment)."""
    settings = _load_settings_or_exit(_required_env(tmp_path))

    _metadata_store, artifact_store = _build_stores(settings)

    # Must not raise: the store root/blobs/index/marker already exist.
    artifact_store.health()


def test_build_stores_metadata_store_is_healthy_immediately(tmp_path):
    settings = _load_settings_or_exit(_required_env(tmp_path))

    metadata_store, _artifact_store = _build_stores(settings)

    # SQLiteMetadataStore applies its migrations synchronously in
    # __init__ (see sqlite_store.py), so it is healthy as soon as it is
    # constructed — no separate initialise() step needed for it.
    metadata_store.health()


def test_build_stores_respects_custom_max_artifact_bytes(tmp_path):
    env = _required_env(tmp_path)
    env["CER_MAX_ARTIFACT_BYTES"] = "1024"
    settings = _load_settings_or_exit(env)

    _metadata_store, artifact_store = _build_stores(settings)

    assert artifact_store.max_bytes == 1024


# --- _build_stores: restart onto a vanished artifact volume ----------------
#
# docker-compose.yml mounts the metadata DB and the artifact root from two
# SEPARATE named volumes, so the artifact volume can be lost on its own
# while the service is stopped. Startup must not paper that over by
# re-initialising an empty artifact store: /ready would report 200 while
# GET /v1/artifacts/{id} still asserted the artifact existed and only the
# download 404-ed. See cer.api.main's module docstring.


def _destroy_artifact_root(root) -> None:
    """Destroy the artifact volume the way losing a Docker volume does:
    the directory and everything under it is simply gone. Not a permission
    trick — this suite runs as root, which bypasses permission bits."""
    shutil.rmtree(root)


def _register_one_artifact(metadata_store, artifact_store) -> ArtifactRecord:
    """Register a real artifact through both stores, as the API does."""
    record = artifact_store.put(
        b"evidence bytes registered before the artifact volume is lost",
        content_type="application/octet-stream",
        filename="before-loss.bin",
    )
    return metadata_store.register_artifact(record)


def test_build_stores_initialises_on_a_genuine_first_deployment(tmp_path):
    """Both volumes empty: nothing was ever registered, so creating the
    artifact store from nothing is correct and /ready must pass."""
    settings = _load_settings_or_exit(_required_env(tmp_path))

    metadata_store, artifact_store = _build_stores(settings)
    try:
        artifact_store.health()  # must not raise
        assert (Path(settings.artifact_root) / "store.json").is_file()
        assert (Path(settings.artifact_root) / "blobs").is_dir()
        assert (Path(settings.artifact_root) / "index").is_dir()
    finally:
        metadata_store.close()


def test_build_stores_restart_with_both_volumes_intact_is_healthy(tmp_path):
    """The ordinary restart: initialise() is a no-op and the artifact
    registered before the restart is still retrievable."""
    settings = _load_settings_or_exit(_required_env(tmp_path))

    metadata_store_1, artifact_store_1 = _build_stores(settings)
    stored = _register_one_artifact(metadata_store_1, artifact_store_1)
    metadata_store_1.close()
    del metadata_store_1, artifact_store_1

    metadata_store_2, artifact_store_2 = _build_stores(settings)
    try:
        artifact_store_2.health()  # must not raise
        assert artifact_store_2.stat(stored.artifact_id).sha256 == stored.sha256
        assert artifact_store_2.get(stored.artifact_id).startswith(b"evidence bytes")
        assert metadata_store_2.get_artifact(stored.artifact_id).artifact_id == stored.artifact_id
    finally:
        metadata_store_2.close()


def test_build_stores_refuses_to_initialise_over_a_lost_artifact_volume(tmp_path):
    """THE regression test for this defect.

    Artifact volume lost while stopped, metadata volume retained. Startup
    must NOT re-create the artifact store: it must leave it uninitialised
    (so health() fails and /ready reports 503 naming artifact_store), must
    not re-create the artifact root on disk, and must leave the metadata
    record readable so an operator can see exactly what was lost.
    """
    settings = _load_settings_or_exit(_required_env(tmp_path))
    artifact_root = Path(settings.artifact_root)

    metadata_store_1, artifact_store_1 = _build_stores(settings)
    stored = _register_one_artifact(metadata_store_1, artifact_store_1)
    metadata_store_1.close()
    del metadata_store_1, artifact_store_1

    _destroy_artifact_root(artifact_root)
    assert not artifact_root.exists()

    metadata_store_2, artifact_store_2 = _build_stores(settings)
    try:
        # 1. The artifact root was NOT silently re-created.
        assert not artifact_root.exists(), (
            "startup re-created the artifact root over a lost volume"
        )

        # 2. The store reports itself unusable -> /ready 503 (artifact_store).
        with pytest.raises(ArtifactStoreError):
            artifact_store_2.health()

        # 3. The write path refuses rather than lazily re-initialising.
        #    Without mark_previously_initialised() put() would treat this
        #    as a fresh root, create the layout and return 201 for an
        #    artifact whose predecessors are already lost.
        with pytest.raises(ArtifactStoreError):
            artifact_store_2.put(
                b"a write that must not be acknowledged",
                content_type="application/octet-stream",
                filename="after-loss.bin",
            )
        assert not artifact_root.exists(), (
            "the refused write re-created the artifact root"
        )

        # 4. Reading the lost artifact is a storage failure (503), never a
        #    404 blaming the caller's artifact_id.
        with pytest.raises(ArtifactStoreError):
            artifact_store_2.stat(stored.artifact_id)

        # 5. The metadata record survives and is readable, so the operator
        #    can enumerate precisely what was lost.
        surviving = metadata_store_2.get_artifact(stored.artifact_id)
        assert surviving.artifact_id == stored.artifact_id
        assert surviving.sha256 == stored.sha256
        assert surviving.filename == "before-loss.bin"
    finally:
        metadata_store_2.close()


def test_build_stores_logs_the_lost_volume_condition_as_a_structured_error(tmp_path, caplog):
    """The condition must be greppable, not buried in free text: a single
    ERROR record carrying the stable event name and the artifact root."""
    settings = _load_settings_or_exit(_required_env(tmp_path))
    artifact_root = Path(settings.artifact_root)

    metadata_store_1, artifact_store_1 = _build_stores(settings)
    _register_one_artifact(metadata_store_1, artifact_store_1)
    metadata_store_1.close()
    del metadata_store_1, artifact_store_1

    _destroy_artifact_root(artifact_root)

    with caplog.at_level(logging.INFO, logger="cer.api.main"):
        metadata_store_2, _artifact_store_2 = _build_stores(settings)
    metadata_store_2.close()

    events = [getattr(r, "event", None) for r in caplog.records]
    assert EVENT_ARTIFACT_STORE_VOLUME_LOST in events, events
    assert EVENT_ARTIFACT_STORE_FIRST_DEPLOYMENT not in events, events

    record = next(r for r in caplog.records if getattr(r, "event", None) == EVENT_ARTIFACT_STORE_VOLUME_LOST)
    assert record.levelno == logging.ERROR
    assert record.artifact_root == str(artifact_root)


def test_build_stores_recovers_once_the_artifact_volume_is_restored(tmp_path):
    """Restore the volume (contents and all) and the next start is healthy
    again — the refusal is a live condition, not a permanent tombstone."""
    settings = _load_settings_or_exit(_required_env(tmp_path))
    artifact_root = Path(settings.artifact_root)

    metadata_store_1, artifact_store_1 = _build_stores(settings)
    stored = _register_one_artifact(metadata_store_1, artifact_store_1)
    metadata_store_1.close()
    del metadata_store_1, artifact_store_1

    backup = tmp_path / "artifact-volume-backup"
    shutil.copytree(artifact_root, backup)
    _destroy_artifact_root(artifact_root)

    metadata_store_2, artifact_store_2 = _build_stores(settings)
    with pytest.raises(ArtifactStoreError):
        artifact_store_2.health()
    metadata_store_2.close()
    del metadata_store_2, artifact_store_2

    shutil.copytree(backup, artifact_root)

    metadata_store_3, artifact_store_3 = _build_stores(settings)
    try:
        artifact_store_3.health()  # must not raise
        assert artifact_store_3.get(stored.artifact_id).startswith(b"evidence bytes")
    finally:
        metadata_store_3.close()


def test_build_stores_does_not_initialise_when_the_metadata_store_cannot_answer(tmp_path):
    """"I don't know" is not "no artifacts exist".

    If the artifact volume is absent and the metadata store cannot be
    queried, initialising would be a guess that can destroy evidence.
    Startup refuses and stays unready instead.
    """
    settings = _load_settings_or_exit(_required_env(tmp_path))
    artifact_root = Path(settings.artifact_root)

    class _BrokenMetadataStore:
        def query_artifacts(self, **kwargs):
            raise MetadataStoreError("metadata store is unavailable")

    artifact_store = FilesystemArtifactStore(
        artifact_root, max_bytes=settings.max_artifact_bytes
    )
    _initialise_artifact_store_if_safe(_BrokenMetadataStore(), artifact_store)

    assert not artifact_root.exists(), (
        "startup initialised an artifact store without being able to rule "
        "out a lost volume"
    )
    with pytest.raises(ArtifactStoreError):
        artifact_store.health()


def test_mark_previously_initialised_creates_nothing(tmp_path):
    """The flag is in-memory only: it must never touch disk (that would be
    the very re-creation this defect is about)."""
    root = tmp_path / "never-existed"
    store = FilesystemArtifactStore(root, max_bytes=1024)

    store.mark_previously_initialised()

    assert not root.exists()
    with pytest.raises(ArtifactStoreError):
        store.health()
