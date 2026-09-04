"""Shared fixtures for the real-store integration suite.

Every fixture here wires the REAL ``SQLiteMetadataStore`` and REAL
``FilesystemArtifactStore`` against real temp-directory paths (pytest's
``tmp_path``), driven through the actual FastAPI app
(``cer.api.app.create_app``) via ``TestClient`` -- no in-memory fakes
anywhere in this package.

Note the wiring the dispatch called out explicitly: ``FilesystemArtifactStore``
needs an explicit :meth:`~cer.artifacts.fs_store.FilesystemArtifactStore.initialise`
call before ``/ready`` will pass -- ``health()`` deliberately never creates
anything (see the class docstring). The ``artifact_store`` fixture below
calls it.

Several fault/restart tests in ``test_faults.py`` deliberately do *not* use
these fixtures -- they need to construct stores whose backing paths they
then break or reopen, so they build their own ``metadata_store``/
``artifact_store``/``app``/``client`` locally. This module is for the
"happy path, real stores" tests (vertical slice, multi-producer,
lifecycle/health).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cer.api.app import create_app
from cer.artifacts import FilesystemArtifactStore
from cer.contract.version import CONTRACT_VERSION
from cer.metadata import SQLiteMetadataStore
from cer.runtime.config import Settings

#: Every request against the contract-version-gated API needs this header.
CONTRACT_HEADERS = {"X-CER-Contract-Version": CONTRACT_VERSION}


def headers(**extra: str) -> dict[str, str]:
    merged = dict(CONTRACT_HEADERS)
    merged.update(extra)
    return merged


def make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        metadata_db_path=str(tmp_path / "cer_metadata" / "cer.db"),
        artifact_root=str(tmp_path / "cer_artifacts"),
        host="127.0.0.1",
        port=8000,
        log_level="INFO",
        environment="test",
        max_artifact_bytes=10 * 1024 * 1024,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def metadata_store(settings) -> SQLiteMetadataStore:
    store = SQLiteMetadataStore(settings.metadata_db_path)
    yield store
    store.close()


@pytest.fixture
def artifact_store(settings) -> FilesystemArtifactStore:
    store = FilesystemArtifactStore(settings.artifact_root, max_bytes=settings.max_artifact_bytes)
    # The dispatch's called-out wiring requirement: initialise() must be
    # called explicitly -- health() never creates anything itself.
    store.initialise()
    return store


@pytest.fixture
def app(metadata_store, artifact_store, settings):
    return create_app(metadata_store, artifact_store, settings)


@pytest.fixture
def client(app):
    # raise_server_exceptions=False: several tests in this suite deliberately
    # trigger CER error paths and assert on the resulting HTTP response, not
    # a re-raised Python exception in the test process.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
