"""Tests for cer.api.main — the container entrypoint.

Covers the two things W3-DOCKER fixed in ``main.py``:

* startup fails loudly (clear message, non-zero exit) on missing required
  configuration, rather than a raw traceback into uvicorn internals;
* the real ``MetadataStore``/``ArtifactStore`` backends are wired with the
  correct class names and constructor arguments, and the artifact store is
  actually ``initialise()``-d before the app would start serving (the
  ``/ready`` 503-on-fresh-deployment bug the PID called out).

Deliberately does not start uvicorn or touch Docker/the network — see
``_load_settings_or_exit``/``_build_stores`` in ``cer.api.main``, which
exist specifically to make this testable without either.
"""

from __future__ import annotations

import pytest

from cer.api.main import _build_stores, _load_settings_or_exit
from cer.artifacts import FilesystemArtifactStore
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
