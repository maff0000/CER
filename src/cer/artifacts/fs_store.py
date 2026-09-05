"""Filesystem-backed, hash-addressed :class:`~cer.contract.stores.ArtifactStore`.

Implements the CER artifact doctrine (PID "Artifact doctrine" section) on
plain local disk: no object-storage SDK, no database, no async — the
smallest robust implementation the anti-bloat directive allows.

On-disk layout
---------------
Given a store root (e.g. ``/var/lib/cer/artifacts``)::

    <root>/
        blobs/
            <sha[0:2]>/
                <sha[2:4]>/
                    <sha256 hex digest>          # raw artifact bytes
        index/
            <artifact_id>.json                    # ArtifactRecord sidecar

* **Blobs are content-addressed.** The path is derived purely from the
  SHA-256 of the bytes, sharded two levels deep (256 x 256 buckets) so no
  directory ever holds more than a manageable number of entries. Two
  registrations of byte-identical content resolve to the *same* blob path
  and therefore occupy one blob on disk — that is deliberate dedup, not a
  bug. A human can find a blob by hand: ``sha256sum`` the bytes, then look
  under ``blobs/<first 2 hex chars>/<next 2 hex chars>/<full hex digest>``.

* **Every registration gets its own identity.** Each call to :meth:`put`
  mints a fresh ``artifact_id`` (via ``cer.contract.identity.new_artifact_id``)
  and writes a small JSON sidecar at ``index/<artifact_id>.json`` holding
  the full :class:`~cer.contract.models.ArtifactRecord`. Two registrations
  of identical bytes are therefore two distinct artifacts (two sidecars)
  sharing one blob — :meth:`get`/:meth:`stat` resolve an ``artifact_id`` to
  its record via the sidecar alone, with no dependency on any metadata
  store, which is also what makes restart persistence trivial: a second
  store instance opened on the same root sees exactly what the first wrote.

``uri``
-------
:attr:`~cer.contract.models.ArtifactRecord.uri` is a ``file://`` URI
pointing at the absolute on-disk blob path (``Path.as_uri()`` of the
resolved store root joined with the sharded blob path). It is stable for a
fixed store root. Note it is a property of the *blob*, not the individual
registration: two artifact_ids sharing one blob share the same ``uri``.

Immutability
------------
A blob is written once. If the canonical blob path already exists when
:meth:`put` runs, the store compares the existing bytes to the incoming
bytes: identical content is an idempotent no-op (no second write), but
content that differs under the same digest path (on-disk corruption, or a
hash collision) raises :class:`~cer.contract.errors.ImmutabilityError`
rather than silently overwriting — this is the literal mechanism behind
the PID's "prevent silent overwrite of immutable artifacts" requirement.
Sidecars never collide in practice (``artifact_id`` is a fresh UUID4 per
call) so no such check is needed for them.

Writes are atomic: data is written to a temp file in the same directory as
the final path, ``fsync``-ed, then moved into place with ``os.replace`` —
a crash mid-write can never leave a torn blob or sidecar at its canonical
path.

Checksums are re-verified on every :meth:`get`, so on-disk bit-rot is
never silently served back as valid evidence.

Initialisation vs. health
--------------------------
Creating the on-disk layout (``blobs/``, ``index/``, and a small
``store.json`` marker at the root recording a layout version and UTC
creation timestamp) and *checking* that the store is usable are
deliberately separate operations:

* :meth:`initialise` is the only thing that creates anything. It is
  idempotent and safe to call repeatedly (e.g. once at service startup) —
  it never touches existing blobs or sidecars, and never rewrites an
  already-present marker. :meth:`put` calls it lazily on the *first* write
  to a never-yet-initialised root, so a store still works out of the box
  against a fresh root.
* :meth:`health` never creates anything. It requires the root, ``blobs/``,
  ``index/`` and the marker to already be present, then proves writability
  with a probe file created inside the existing root and removed. If the
  backing volume has vanished (unmounted, deleted, never initialised),
  ``health()`` raises :class:`~cer.contract.errors.ArtifactStoreError`
  rather than silently recreating an empty store and reporting healthy —
  an evidence registry must never manufacture a clean-looking store out of
  a disappeared one.

First-time initialisation vs. silent re-creation
--------------------------------------------------
Lazy initialisation in :meth:`put` used to be unconditional, which meant
the write path did exactly what :meth:`health` refuses to do: a store
whose backing volume had vanished was re-manufactured, empty, by the next
``put()``. The registry then acknowledged ``201 Created``, ``/ready``
flipped back from 503 to 200, and every artifact registered before the
volume disappeared was silently gone while its metadata still asserted it
existed. That is the artifact-side twin of the durable-backing guard the
metadata store already applies to every write
(``SQLiteMetadataStore._require_durable_backing``), and it is closed the
same way — with one cheap existence check on the paths that matter, not a
thread, watchdog or polling loop.

The ``store.json`` marker is what separates the two cases, together with
an instance flag (:attr:`_initialised`) recording whether *this* store
object has ever seen a fully-present backing layout:

* **Never initialised** (no marker was ever observed): :meth:`put` may
  still initialise on first write. That is legitimate startup against a
  fresh root, not resurrection.
* **Initialised, then vanished** (the marker/root was present and is not
  any more): :meth:`put` raises
  :class:`~cer.contract.errors.ArtifactStoreError` — HTTP 503 — instead of
  re-creating the layout. A producer that receives ``201`` must be able to
  rely on it.

That distinction is drawn from what *this process* has observed, which is
enough while the process is running but not across a restart: a service
starting up against an artifact volume that vanished while it was stopped
sees only an empty directory, indistinguishable from a first deployment.
:meth:`mark_previously_initialised` is how the entrypoint hands this store
the evidence it cannot get from the volume itself — the metadata store's
own artifact records — so the restart case lands in the same "initialised,
then vanished" state, with the same refusals, as the live-process case.

The same check guards the *read* path, but only on the miss branch of
:meth:`stat` so the happy path pays nothing. Without it a vanished store
reports ``NotFoundError`` → ``404 "artifact_id ... is not registered"``
for evidence that *is* registered, telling the producer to check an
``artifact_id`` that was never the problem. A server-side storage failure
must read as a server-side failure (503), never as a 404 blaming the
caller.

Like the metadata store's guard, this does not (and cannot) defeat POSIX:
a path that disappears in the microseconds between the check and the
write is still lost. It closes the operationally real case — backing gone
for the remaining life of the process — not an instantaneous race.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Optional

from cer.contract.errors import (
    ArtifactStoreError,
    ChecksumMismatchError,
    ImmutabilityError,
    NotFoundError,
)
from cer.contract.identity import new_artifact_id
from cer.contract.models import ArtifactRecord
from cer.runtime.clock import isoformat_utc, utcnow

__all__ = ["FilesystemArtifactStore"]

#: Name of the marker file written at the store root by initialise().
_MARKER_FILENAME = "store.json"

#: On-disk layout version recorded in the marker file. Bump this if the
#: blob/index layout ever changes in a way that requires migration.
_LAYOUT_VERSION = 1


class FilesystemArtifactStore:
    """Local-filesystem, content-addressed implementation of ``ArtifactStore``.

    Parameters
    ----------
    root:
        Directory the store treats as its root. Not required to exist yet
        at construction time — it (and ``blobs/``/``index/``) is created
        lazily, on first use, so that pointing a store at a bad path and
        calling :meth:`health` is a legitimate way to observe failure
        rather than crashing the constructor.
    max_bytes:
        Maximum size in bytes for a single :meth:`put`. Supplied by the
        caller (the API layer, wired from ``cer.runtime.config``) — this
        class never reads the environment itself.
    """

    def __init__(self, root: str | Path, *, max_bytes: int) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError(f"max_bytes must be a positive int, got {max_bytes!r}")
        # Path.resolve() normalises to an absolute path without requiring
        # the path to exist (strict=False is the default).
        self.root = Path(root).expanduser().resolve()
        self.blobs_dir = self.root / "blobs"
        self.index_dir = self.root / "index"
        self.max_bytes = max_bytes
        # Guards the read-compare-write sequence in _store_blob so two
        # threads writing identical content concurrently converge cleanly
        # rather than doing redundant work; correctness does not strictly
        # depend on it (os.replace is atomic and racing writers always
        # write bit-identical bytes to the same digest-derived path) but it
        # avoids wasted double I/O and keeps the reasoning simple.
        self._lock = threading.Lock()
        # True once this instance has observed (or created) a fully-present
        # backing layout. It is what distinguishes "never initialised, may
        # initialise on first write" from "was initialised and has since
        # vanished, must refuse" -- see the module docstring's "First-time
        # initialisation vs. silent re-creation" section. Seeded from disk
        # so a store constructed against an already-initialised root (the
        # ordinary restart case) is guarded from its very first call.
        self._initialised = self._backing_present()

    # -- path helpers --------------------------------------------------

    def _blob_path(self, sha256_hex: str) -> Path:
        return self.blobs_dir / sha256_hex[0:2] / sha256_hex[2:4] / sha256_hex

    def _blob_uri(self, blob_path: Path) -> str:
        return blob_path.as_uri()

    def _sidecar_path(self, artifact_id: str) -> Path:
        safe = self._safe_artifact_id(artifact_id)
        return self.index_dir / f"{safe}.json"

    def _marker_path(self) -> Path:
        return self.root / _MARKER_FILENAME

    @staticmethod
    def _safe_artifact_id(artifact_id: object) -> str:
        """Reject artifact_ids that could escape the index directory.

        A malformed/hostile id (containing a path separator, or a bare
        ``.``/``..``) can never have been produced by
        ``cer.contract.identity.new_artifact_id`` and therefore can never
        resolve to a registered artifact — treat it as simply unknown
        rather than letting it touch the filesystem outside ``index/``.
        """
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or "/" in artifact_id
            or "\\" in artifact_id
            or artifact_id in (".", "..")
        ):
            raise NotFoundError(f"artifact_id {artifact_id!r} is not a known artifact")
        return artifact_id

    # -- backing-storage presence ------------------------------------------

    def _backing_present(self) -> bool:
        """Return whether the full on-disk layout is present right now.

        The root, ``blobs/``, ``index/`` and the ``store.json`` marker must
        all exist. This is the single cheap existence check the write path
        (:meth:`put`), the read path (:meth:`stat`'s miss branch) and
        :meth:`health` are all built on, so none of them can disagree
        about whether the store's backing storage is still there.
        """
        try:
            return (
                self.root.is_dir()
                and self.blobs_dir.is_dir()
                and self.index_dir.is_dir()
                and self._marker_path().is_file()
            )
        except OSError:
            # A path that cannot even be stat-ed is, for every purpose
            # here, not present.
            return False

    def _require_live_backing(self, *, refusing_to: str) -> None:
        """Raise ``ArtifactStoreError`` if backing that was there has gone.

        A store that has *never* been initialised is left alone: it has no
        artifacts to lose, and first-write initialisation is legitimate.
        Only a store that was initialised and whose backing has since
        disappeared is refused -- silently re-manufacturing it would
        acknowledge writes that are already lost and let ``/ready`` report
        a healthy store that has quietly dropped every artifact in it.
        """
        if self._initialised and not self._backing_present():
            raise ArtifactStoreError(
                f"artifact store at {self.root} was initialised but its "
                "backing storage is no longer present (root/blobs/index/"
                f"marker not all found) — refusing to {refusing_to}; the "
                "volume may have been unmounted or deleted"
            )

    # -- atomic write ----------------------------------------------------

    def _atomic_write(self, path: Path, data: bytes) -> None:
        """Write ``data`` to ``path`` atomically: temp file + fsync + replace."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=f".tmp-{path.name}-", suffix=".part"
            )
        except OSError as exc:
            raise ArtifactStoreError(
                f"failed to prepare write for {path}: {exc}"
            ) from exc

        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, str(path))
        except OSError as exc:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise ArtifactStoreError(f"failed to write {path}: {exc}") from exc

    def _store_blob(self, blob_path: Path, data: bytes) -> None:
        """Write the blob at ``blob_path`` unless it already exists.

        Idempotent no-op if the existing blob is byte-identical;
        ``ImmutabilityError`` if it differs (corruption / hash collision).
        Never overwrites a differing blob.
        """
        with self._lock:
            if blob_path.exists():
                try:
                    existing = blob_path.read_bytes()
                except OSError as exc:
                    raise ArtifactStoreError(
                        f"failed to read existing blob at {blob_path}: {exc}"
                    ) from exc
                if existing == data:
                    return
                raise ImmutabilityError(
                    f"blob at {blob_path} already exists with different content "
                    "for this digest path (on-disk corruption or a hash "
                    "collision) — refusing to silently overwrite"
                )
            self._atomic_write(blob_path, data)

    # -- initialisation ----------------------------------------------------

    def initialise(self) -> None:
        """Create the store root, ``blobs/``, ``index/`` and the marker file.

        Idempotent: safe to call on an already-initialised store. Never
        touches existing blobs or sidecars, and never rewrites an
        already-present marker (its ``created_at_utc`` reflects the store's
        original initialisation, not the most recent call). This is the
        only method on this class that creates anything on disk — the
        service entrypoint calls it once at startup, and :meth:`put` calls
        it lazily on the first write to a never-yet-initialised root so a
        fresh root works out of the box. It is *not* called by :meth:`put`
        for a store whose backing has vanished since initialisation: see
        the module docstring's "First-time initialisation vs. silent
        re-creation" section.
        """
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.blobs_dir.mkdir(parents=True, exist_ok=True)
            self.index_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ArtifactStoreError(
                f"failed to initialise artifact store at {self.root}: {exc}"
            ) from exc

        marker_path = self._marker_path()
        if not marker_path.exists():
            payload = json.dumps(
                {
                    "layout_version": _LAYOUT_VERSION,
                    "created_at_utc": isoformat_utc(utcnow()),
                }
            ).encode("utf-8")
            self._atomic_write(marker_path, payload)

        # From here on this instance knows the layout existed, so a later
        # disappearance is a fault to refuse rather than a fresh root to
        # create.
        self._initialised = True

    def mark_previously_initialised(self) -> None:
        """Record -- on evidence from outside this volume -- that this store
        *was* initialised, without creating anything.

        This is the only way to reach the "was initialised, has since
        vanished" state described in the module docstring's "First-time
        initialisation vs. silent re-creation" section for a store that
        never observed its own backing. It touches no disk state at all:
        it sets only the instance flag :meth:`put` and :meth:`stat`
        consult, so a store whose layout is absent *right now* behaves
        exactly like one whose layout vanished mid-process -- refusing
        writes with :class:`~cer.contract.errors.ArtifactStoreError`
        (HTTP 503) and reporting a storage failure rather than a 404,
        instead of lazily re-manufacturing an empty store on the first
        ``put()``.

        The caller must actually hold that evidence. The service
        entrypoint does: a metadata store holding artifact records
        alongside an artifact root with no marker is not a first
        deployment, it is a lost artifact volume -- see
        ``cer.api.main._initialise_artifact_store_if_safe``. Without
        this call, :meth:`put`'s lazy first-write initialisation (which
        is correct for a genuinely fresh root, and cannot tell the two
        apart by looking at the volume alone) would re-create the layout
        and acknowledge ``201`` for artifacts that are already gone.

        Idempotent, and safe to call on a store whose backing *is*
        present: the flag would be set by the next :meth:`health`,
        :meth:`put` or :meth:`initialise` call anyway.
        """
        self._initialised = True

    # -- ArtifactStore protocol -------------------------------------------

    def put(
        self,
        data: bytes,
        *,
        content_type: str,
        filename: str,
        declared_sha256: Optional[str] = None,
    ) -> ArtifactRecord:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"data must be bytes, got {type(data).__name__}")
        data = bytes(data)

        if not isinstance(content_type, str) or not content_type.strip():
            raise ArtifactStoreError(
                "content_type must be a non-empty string", code="invalid_content_type"
            )
        if not isinstance(filename, str) or not filename.strip():
            raise ArtifactStoreError(
                "filename must be a non-empty string", code="invalid_filename"
            )

        size = len(data)
        if size > self.max_bytes:
            raise ArtifactStoreError(
                f"artifact size {size} bytes exceeds max_bytes {self.max_bytes}",
                code="artifact_too_large",
            )

        computed = hashlib.sha256(data).hexdigest()
        if declared_sha256 is not None and declared_sha256.lower() != computed:
            raise ChecksumMismatchError(
                f"declared_sha256 {declared_sha256!r} does not match computed "
                f"digest {computed!r} — nothing written",
            )

        blob_path = self._blob_path(computed)
        # Nothing written yet at this point: size/content_type/filename/
        # checksum are all validated. Only now do we touch disk.
        #
        # A fresh, never-initialised root is initialised here so a store
        # works out of the box and every blob/sidecar lands beside a valid
        # marker. A root that was initialised and has since disappeared is
        # NOT re-created: acknowledging this write would mean returning
        # 201 for evidence that is already lost, and would flip /ready
        # back to 200 over a store that had silently dropped everything in
        # it. This is the artifact-side twin of the metadata store's
        # _require_durable_backing check on every write.
        if self._backing_present():
            self._initialised = True
        else:
            self._require_live_backing(refusing_to="register a new artifact")
            self.initialise()
        self._store_blob(blob_path, data)

        record = ArtifactRecord(
            artifact_id=new_artifact_id(),
            sha256=computed,
            size_bytes=size,
            content_type=content_type,
            filename=filename,
            uri=self._blob_uri(blob_path),
            registered_at=utcnow(),
        )

        sidecar_path = self._sidecar_path(record.artifact_id)
        self._atomic_write(sidecar_path, record.model_dump_json().encode("utf-8"))

        return record

    def get(self, artifact_id: str) -> bytes:
        record = self.stat(artifact_id)
        blob_path = self._blob_path(record.sha256)
        try:
            data = blob_path.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactStoreError(
                f"artifact {artifact_id!r} is registered but its blob is "
                f"missing at {blob_path}"
            ) from exc
        except OSError as exc:
            raise ArtifactStoreError(
                f"failed to read blob for artifact {artifact_id!r}: {exc}"
            ) from exc

        computed = hashlib.sha256(data).hexdigest()
        if computed != record.sha256:
            raise ChecksumMismatchError(
                f"blob for artifact {artifact_id!r} failed checksum "
                f"verification on read: expected {record.sha256}, "
                f"computed {computed} — refusing to serve corrupted evidence"
            )
        return data

    def stat(self, artifact_id: str) -> ArtifactRecord:
        sidecar_path = self._sidecar_path(artifact_id)
        try:
            raw = sidecar_path.read_bytes()
        except FileNotFoundError as exc:
            # Before blaming the caller's artifact_id, check whether the
            # store itself still exists. A vanished backing volume makes
            # *every* lookup miss; reporting that as "not registered" is
            # false (the artifact is registered — the storage is gone) and
            # sends the producer to debug an id that was never the
            # problem. Only reached on the miss branch, so a successful
            # read pays nothing for this.
            self._require_live_backing(
                refusing_to=f"report artifact {artifact_id!r} as unregistered"
            )
            raise NotFoundError(
                f"artifact_id {artifact_id!r} is not registered"
            ) from exc
        except OSError as exc:
            raise ArtifactStoreError(
                f"failed to read artifact index for {artifact_id!r}: {exc}"
            ) from exc

        try:
            return ArtifactRecord.model_validate_json(raw)
        except Exception as exc:  # pydantic ValidationError / json decode error
            raise ArtifactStoreError(
                f"artifact index for {artifact_id!r} is corrupt: {exc}"
            ) from exc

    def exists(self, artifact_id: str) -> bool:
        try:
            sidecar_path = self._sidecar_path(artifact_id)
        except NotFoundError:
            return False
        try:
            return sidecar_path.is_file()
        except OSError:
            return False

    def health(self) -> None:
        """Raise ``ArtifactStoreError`` if the store is not usable.

        Never creates anything (see the module docstring's "Initialisation
        vs. health" section). Requires the root, ``blobs/``, ``index/`` and
        the marker file to already exist — a never-initialised store, or
        one whose backing volume has vanished since initialisation, is
        reported as unhealthy rather than silently recreated as an empty,
        "healthy" store that has quietly lost every previously registered
        artifact.
        """
        if not self._backing_present():
            raise ArtifactStoreError(
                f"artifact store at {self.root} is not initialised, or its "
                "backing storage is missing (root/blobs/index/marker not "
                "all present) — call initialise() first, or the volume may "
                "have been unmounted or deleted"
            )

        # Observing a fully-present layout is itself proof this store was
        # initialised, so a later disappearance is refused by put()/stat()
        # even if this instance never called initialise() itself. Nothing
        # is created here — only an in-memory flag is set.
        self._initialised = True

        probe_path = self.root / f".health-probe-{uuid.uuid4().hex}"
        try:
            probe_path.write_bytes(b"ok")
            probe_path.read_bytes()
        except OSError as exc:
            raise ArtifactStoreError(
                f"artifact store root {self.root} is not writable: {exc}"
            ) from exc
        finally:
            try:
                probe_path.unlink()
            except OSError:
                pass
