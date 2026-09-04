from typing import Protocol, runtime_checkable

from cer.contract.stores import ArtifactStore, MetadataStore


def test_artifact_store_is_runtime_checkable_protocol():
    assert issubclass(ArtifactStore, Protocol)

    class FakeArtifactStore:
        def put(self, data, *, content_type, filename, declared_sha256=None):
            raise NotImplementedError

        def get(self, artifact_id):
            raise NotImplementedError

        def stat(self, artifact_id):
            raise NotImplementedError

        def exists(self, artifact_id):
            raise NotImplementedError

        def health(self):
            raise NotImplementedError

    assert isinstance(FakeArtifactStore(), ArtifactStore)


def test_metadata_store_is_runtime_checkable_protocol():
    assert issubclass(MetadataStore, Protocol)

    expected_methods = {
        "register_strategy",
        "register_strategy_version",
        "create_experiment",
        "create_run",
        "close_run",
        "append_evidence",
        "register_artifact",
        "attach_artifact",
        "record_promotion",
        "record_health",
        "get_evidence",
        "get_artifact",
        "query_evidence",
        "query_artifacts",
        "query_promotions",
        "query_health",
        "health",
    }
    for method in expected_methods:
        assert hasattr(MetadataStore, method), f"MetadataStore missing {method}"


def test_incomplete_implementation_is_not_an_instance():
    class IncompleteStore:
        def put(self, *a, **kw):
            raise NotImplementedError

    assert not isinstance(IncompleteStore(), ArtifactStore)
