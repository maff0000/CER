from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cer.api.app import create_app
from cer.runtime.config import Settings

from .fakes import FakeArtifactStore, FakeMetadataStore


def make_settings(**overrides) -> Settings:
    defaults = dict(
        metadata_db_path="unused-in-tests",
        artifact_root="unused-in-tests",
        host="127.0.0.1",
        port=8000,
        log_level="INFO",
        environment="test",
        max_artifact_bytes=10 * 1024 * 1024,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def metadata_store() -> FakeMetadataStore:
    return FakeMetadataStore()


@pytest.fixture
def artifact_store() -> FakeArtifactStore:
    return FakeArtifactStore()


@pytest.fixture
def app(metadata_store, artifact_store):
    return create_app(metadata_store, artifact_store, make_settings())


@pytest.fixture
def client(app):
    # raise_server_exceptions=False: we test that unexpected exceptions
    # produce a well-formed 500 response, not that TestClient re-raises
    # them into the test process.
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
