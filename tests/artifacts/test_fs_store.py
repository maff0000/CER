"""Tests for :class:`cer.artifacts.fs_store.FilesystemArtifactStore`."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading

import pytest

from cer.artifacts.fs_store import FilesystemArtifactStore
from cer.contract.errors import (
    ArtifactStoreError,
    ChecksumMismatchError,
    ImmutabilityError,
    NotFoundError,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_store(tmp_path, max_bytes: int = 10_000_000) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(tmp_path / "store", max_bytes=max_bytes)


def _all_blob_files(store: FilesystemArtifactStore) -> list:
    if not store.blobs_dir.exists():
        return []
    return [p for p in store.blobs_dir.rglob("*") if p.is_file()]


def _all_sidecar_files(store: FilesystemArtifactStore) -> list:
    if not store.index_dir.exists():
        return []
    return [p for p in store.index_dir.rglob("*") if p.is_file()]


# --- round-trip -------------------------------------------------------------


def test_put_get_roundtrip_byte_identical(tmp_path):
    store = _make_store(tmp_path)
    data = b"canonical evidence bytes \x00\x01\x02" * 100

    record = store.put(data, content_type="application/octet-stream", filename="evidence.bin")

    assert record.sha256 == _sha256(data)
    assert record.size_bytes == len(data)
    assert record.content_type == "application/octet-stream"
    assert record.filename == "evidence.bin"
    assert record.artifact_id.startswith("art_")
    assert record.uri.startswith("file://")

    fetched = store.get(record.artifact_id)
    assert fetched == data

    stat_record = store.stat(record.artifact_id)
    assert stat_record == record


def test_put_with_correct_declared_sha256_succeeds(tmp_path):
    store = _make_store(tmp_path)
    data = b"some evidence payload"
    record = store.put(
        data,
        content_type="text/plain",
        filename="notes.txt",
        declared_sha256=_sha256(data),
    )
    assert store.get(record.artifact_id) == data


# --- checksum mismatch -------------------------------------------------------


def test_declared_sha256_mismatch_raises_and_writes_nothing(tmp_path):
    store = _make_store(tmp_path)
    data = b"real content"
    wrong_digest = _sha256(b"different content")

    with pytest.raises(ChecksumMismatchError):
        store.put(
            data,
            content_type="text/plain",
            filename="x.txt",
            declared_sha256=wrong_digest,
        )

    assert _all_blob_files(store) == []
    assert _all_sidecar_files(store) == []


# --- oversize -----------------------------------------------------------------


def test_oversize_input_rejected_nothing_written(tmp_path):
    store = _make_store(tmp_path, max_bytes=10)
    data = b"this payload is definitely longer than ten bytes"

    with pytest.raises(ArtifactStoreError):
        store.put(data, content_type="text/plain", filename="big.txt")

    assert _all_blob_files(store) == []
    assert _all_sidecar_files(store) == []


# --- dedup --------------------------------------------------------------------


def test_identical_content_twice_one_blob_two_artifact_ids(tmp_path):
    store = _make_store(tmp_path)
    data = b"duplicate me"

    record1 = store.put(data, content_type="text/plain", filename="a.txt")
    record2 = store.put(data, content_type="text/plain", filename="b.txt")

    assert record1.artifact_id != record2.artifact_id
    assert record1.sha256 == record2.sha256
    assert record1.uri == record2.uri

    blob_files = _all_blob_files(store)
    assert len(blob_files) == 1

    assert store.get(record1.artifact_id) == data
    assert store.get(record2.artifact_id) == data


# --- bit-rot on read ------------------------------------------------------------


def test_corrupted_blob_raises_checksum_mismatch_on_get(tmp_path):
    store = _make_store(tmp_path)
    data = b"trustworthy evidence"
    record = store.put(data, content_type="text/plain", filename="c.txt")

    blob_path = store._blob_path(record.sha256)
    blob_path.write_bytes(b"corrupted!!" + data)

    with pytest.raises(ChecksumMismatchError):
        store.get(record.artifact_id)


# --- immutability ----------------------------------------------------------------


def test_existing_blob_with_different_bytes_raises_immutability_error(tmp_path):
    store = _make_store(tmp_path)
    data = b"the real bytes for this digest"
    digest = _sha256(data)

    # Simulate a corrupted / colliding blob already sitting at the
    # canonical path for this digest, written outside the store's put().
    blob_path = store._blob_path(digest)
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(b"some other bytes entirely, not the real content")

    with pytest.raises(ImmutabilityError):
        store.put(data, content_type="text/plain", filename="d.txt")

    # The bogus content must not have been silently replaced.
    assert blob_path.read_bytes() == b"some other bytes entirely, not the real content"


def test_blob_collision_error_names_the_digest_and_never_the_on_disk_path(tmp_path, caplog):
    """The collision error reaches a producer as a 409 body, so it must
    identify the conflict by digest and must not hand back the absolute
    blob path (storage internals producers are told not to depend on).
    The path still has to reach the operator -- via the log."""
    import logging

    from cer.artifacts.fs_store import EVENT_BLOB_DIGEST_COLLISION

    store = _make_store(tmp_path)
    data = b"the real bytes whose canonical path is already occupied"
    digest = _sha256(data)

    blob_path = store._blob_path(digest)
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(b"different content sitting under the same digest")

    with caplog.at_level(logging.ERROR, logger="cer.artifacts.fs_store"):
        with pytest.raises(ImmutabilityError) as excinfo:
            store.put(data, content_type="text/plain", filename="d.txt")

    message = str(excinfo.value)

    # Identifies the conflict...
    assert digest in message
    # ...without leaking where it lives on disk.
    assert str(blob_path) not in message
    assert str(store.root) not in message
    assert "/" not in message, f"the error message leaked a path: {message!r}"

    # The operator still gets the full detail, on a greppable event.
    [record] = [r for r in caplog.records if getattr(r, "event", None) == EVENT_BLOB_DIGEST_COLLISION]
    assert record.blob_path == str(blob_path)
    assert record.sha256 == digest


def test_identical_content_written_twice_via_manual_blob_is_idempotent(tmp_path):
    store = _make_store(tmp_path)
    data = b"identical bytes, written once by hand first"
    digest = _sha256(data)

    blob_path = store._blob_path(digest)
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(data)

    # put() should treat pre-existing identical content as a no-op, not an error.
    record = store.put(data, content_type="text/plain", filename="e.txt")
    assert store.get(record.artifact_id) == data


# --- not found / missing blob -----------------------------------------------------


def test_unknown_artifact_id_raises_not_found(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(NotFoundError):
        store.get("art_does_not_exist")
    with pytest.raises(NotFoundError):
        store.stat("art_does_not_exist")
    assert store.exists("art_does_not_exist") is False


def test_exists_never_raises_for_hostile_ids(tmp_path):
    store = _make_store(tmp_path)
    assert store.exists("../../etc/passwd") is False
    assert store.exists("") is False


def test_sidecar_present_but_blob_missing_raises_artifact_store_error(tmp_path):
    store = _make_store(tmp_path)
    data = b"evidence that will vanish"
    record = store.put(data, content_type="text/plain", filename="f.txt")

    blob_path = store._blob_path(record.sha256)
    blob_path.unlink()

    with pytest.raises(ArtifactStoreError):
        store.get(record.artifact_id)
    # But stat (index-only) still works since the sidecar is intact.
    assert store.stat(record.artifact_id).sha256 == record.sha256


# --- restart persistence -----------------------------------------------------------


def test_restart_persistence_second_instance_same_root(tmp_path):
    root = tmp_path / "store"
    store1 = FilesystemArtifactStore(root, max_bytes=10_000_000)
    data = b"survives a restart"
    record = store1.put(data, content_type="text/plain", filename="g.txt")

    store2 = FilesystemArtifactStore(root, max_bytes=10_000_000)
    assert store2.get(record.artifact_id) == data
    assert store2.stat(record.artifact_id) == record


# --- health -------------------------------------------------------------------------


def test_health_passes_on_good_root(tmp_path):
    store = _make_store(tmp_path)
    store.initialise()
    assert store.health() is None


def test_health_raises_when_root_is_under_a_file(tmp_path):
    blocker = tmp_path / "not_a_directory"
    blocker.write_bytes(b"i am a regular file, not a directory")
    store = FilesystemArtifactStore(blocker / "store_root", max_bytes=10_000_000)

    with pytest.raises(ArtifactStoreError):
        store.health()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses filesystem permission bits")
def test_health_raises_on_unwritable_root(tmp_path):
    store = _make_store(tmp_path)
    store.initialise()
    os.chmod(store.root, 0o500)
    try:
        with pytest.raises(ArtifactStoreError):
            store.health()
    finally:
        os.chmod(store.root, 0o700)


# --- health / initialise separation (PL repair: health() must never create) ---------


def test_health_raises_on_never_initialised_root_then_passes_after_initialise(tmp_path):
    store = _make_store(tmp_path)

    with pytest.raises(ArtifactStoreError):
        store.health()
    # health() must not have created anything.
    assert not store.root.exists()

    store.initialise()
    assert store.health() is None


def test_health_after_backing_volume_removed_does_not_recreate_root(tmp_path):
    store = _make_store(tmp_path)
    data = b"evidence registered before the volume disappears"
    record = store.put(data, content_type="text/plain", filename="h.txt")
    assert store.health() is None

    # Simulate the backing volume vanishing (unmount / deletion) after
    # artifacts were already registered.
    shutil.rmtree(store.root)
    assert not store.root.exists()

    with pytest.raises(ArtifactStoreError):
        store.health()

    # The regression this guards against: health() must never silently
    # recreate an empty store root and report healthy.
    assert not store.root.exists()

    # A read of a previously-registered artifact must report a SERVER-side
    # storage failure, not "not registered". The artifact *is* registered;
    # the storage is gone. NotFoundError here would surface as HTTP 404
    # ("artifact_id ... is not registered"), telling the producer to check
    # an id that was never the problem while the fault is entirely
    # server-side. ArtifactStoreError surfaces as 503, which is the truth.
    with pytest.raises(ArtifactStoreError) as exc_info:
        store.get(record.artifact_id)
    assert not isinstance(exc_info.value, NotFoundError)
    with pytest.raises(ArtifactStoreError):
        store.stat(record.artifact_id)

    # ...and the reads must not have recreated anything either.
    assert not store.root.exists()


def test_put_after_backing_volume_removed_refuses_and_does_not_recreate_root(tmp_path):
    """The write path must not re-manufacture a store that has vanished.

    Regression test for the defect this repair closes: ``put()`` used to
    call ``initialise()`` unconditionally, so the first write after a
    backing volume disappeared silently recreated an empty store, returned
    a fresh record as though nothing were wrong, and flipped ``health()``
    back to passing over a store that had quietly lost every artifact in
    it -- precisely what ``health()`` itself refuses to do.
    """
    store = _make_store(tmp_path)
    first = store.put(b"registered before the volume vanishes", content_type="text/plain", filename="k.txt")
    assert store.health() is None

    shutil.rmtree(store.root)
    assert not store.root.exists()

    with pytest.raises(ArtifactStoreError):
        store.put(b"a write that must not be acknowledged", content_type="text/plain", filename="l.txt")

    # Nothing recreated: no root, and therefore no "clean-looking" store.
    assert not store.root.exists()

    # And readiness must still be failing -- it must not have been
    # repaired by the refused write.
    with pytest.raises(ArtifactStoreError):
        store.health()

    # The earlier artifact is still reported as a storage failure, not 404.
    with pytest.raises(ArtifactStoreError):
        store.get(first.artifact_id)


def test_put_after_marker_removed_refuses_even_though_directories_remain(tmp_path):
    """Losing just the marker is still a vanished store, not a fresh one.

    ``health()`` already treats a missing ``store.json`` as unhealthy
    (test_health_raises_when_marker_missing_but_directories_present); the
    write path must agree, or the two disagree about the same store.
    """
    store = _make_store(tmp_path)
    store.initialise()
    store._marker_path().unlink()

    with pytest.raises(ArtifactStoreError):
        store.put(b"nope", content_type="text/plain", filename="m.txt")

    assert not store._marker_path().exists()


def test_never_initialised_store_still_initialises_on_first_write(tmp_path):
    """The guard must not break legitimate first-time initialisation.

    A store that has never been initialised has no artifacts to lose, so
    lazily creating its layout on the first write is startup, not
    resurrection -- and must keep working.
    """
    store = _make_store(tmp_path)
    assert not store.root.exists()

    record = store.put(b"first ever write", content_type="text/plain", filename="n.txt")

    assert store.health() is None
    assert store.get(record.artifact_id) == b"first ever write"


def test_second_instance_on_a_vanished_root_refuses_to_write(tmp_path):
    """A store constructed against an already-initialised root is guarded
    from its first call, without needing initialise() in this process.

    This is the restart case: a new process opens the same root, sees a
    valid layout, and must then refuse writes if that layout disappears
    underneath it -- exactly as the process that created it would.
    """
    root = tmp_path / "store"
    store1 = FilesystemArtifactStore(root, max_bytes=10_000_000)
    store1.initialise()

    store2 = FilesystemArtifactStore(root, max_bytes=10_000_000)
    shutil.rmtree(root)

    with pytest.raises(ArtifactStoreError):
        store2.put(b"nope", content_type="text/plain", filename="o.txt")
    assert not root.exists()


def test_health_raises_when_marker_missing_but_directories_present(tmp_path):
    store = _make_store(tmp_path)
    store.initialise()
    store._marker_path().unlink()

    with pytest.raises(ArtifactStoreError):
        store.health()


def test_initialise_is_idempotent_and_preserves_existing_data(tmp_path):
    store = _make_store(tmp_path)
    store.initialise()
    marker_contents_before = store._marker_path().read_bytes()

    data = b"data present before a second initialise() call"
    record = store.put(data, content_type="text/plain", filename="i.txt")

    store.initialise()  # second call: must be a no-op beyond ensuring dirs exist

    assert store._marker_path().read_bytes() == marker_contents_before
    assert store.get(record.artifact_id) == data
    assert store.stat(record.artifact_id) == record


def test_put_on_fresh_root_lazily_initialises_and_leaves_valid_marker(tmp_path):
    store = _make_store(tmp_path)
    assert not store.root.exists()

    record = store.put(b"first write", content_type="text/plain", filename="j.txt")

    marker_path = store._marker_path()
    assert marker_path.is_file()
    marker = json.loads(marker_path.read_text())
    assert marker["layout_version"] == 1
    assert "created_at_utc" in marker

    assert store.health() is None
    assert store.get(record.artifact_id) == b"first write"


# --- concurrency -----------------------------------------------------------------


def test_concurrent_identical_put_converges_to_one_blob(tmp_path):
    store = _make_store(tmp_path)
    data = b"contended payload " * 1000
    n_threads = 16

    results: list = [None] * n_threads
    errors: list = []

    def worker(i: int) -> None:
        try:
            results[i] = store.put(
                data, content_type="application/octet-stream", filename=f"t{i}.bin"
            )
        except Exception as exc:  # pragma: no cover - failure path surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert all(r is not None for r in results)

    shas = {r.sha256 for r in results}
    assert shas == {_sha256(data)}

    artifact_ids = {r.artifact_id for r in results}
    assert len(artifact_ids) == n_threads  # each put() mints its own identity

    blob_files = _all_blob_files(store)
    assert len(blob_files) == 1
    assert blob_files[0].read_bytes() == data

    for r in results:
        assert store.get(r.artifact_id) == data
