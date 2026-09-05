"""Uvicorn entrypoint: ``python -m cer.api.main``.

Reads all runtime configuration from the environment via
``cer.runtime.config.load_settings()`` (fails loudly if required
configuration is missing — no defaults or secrets embedded in source) and
configures structured logging via ``cer.runtime.logging.configure_logging()``
before constructing the app.

The concrete ``MetadataStore``/``ArtifactStore`` implementations live in
``cer.metadata``/``cer.artifacts``. The import is still deferred to inside
``main()`` (rather than at module import time) so importing this module —
or any other part of ``cer.api`` — never requires those packages, which
keeps ``cer.api`` testable against in-memory fakes without pulling in a
real SQLite/filesystem dependency.

Startup and a lost artifact volume
----------------------------------
``docker-compose.yml`` mounts the metadata database and the artifact root
from two *separate* named volumes, so they can be lost independently. Lose
only the artifact volume while the service is stopped and an unconditional
``artifact_store.initialise()`` at startup silently re-manufactures an
empty artifact store: ``/ready`` reports 200, ``GET
/v1/artifacts/{id}`` still asserts the artifact exists (its metadata is on
the *other* volume, intact), and only the download 404s. The registry
would be lying about evidence it no longer holds.

Looking at the artifact volume alone cannot distinguish that from a
genuine first deployment — both are an empty directory. CER has a second
source of truth: the metadata store knows whether any artifact was ever
registered. :func:`_initialise_artifact_store_if_safe` uses it to decide,
and deliberately refuses to initialise in the one case that is data loss,
leaving the store uninitialised so the existing machinery reports the
truth (``/ready`` 503 naming ``artifact_store``, writes refused, reads
503 rather than a caller-blaming 404). It does not exit or crash-loop: a
service that reports itself honestly unready is more useful to an
operator than one that will not start, and readiness is the PID's stated
mechanism for exactly this ("required dependencies reflected in
readiness").
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Mapping, Optional, Tuple

from cer.runtime.config import ConfigError, Settings, load_settings
from cer.runtime.logging import configure_logging

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from cer.artifacts import FilesystemArtifactStore
    from cer.contract.stores import MetadataStore

logger = logging.getLogger("cer.api.main")


def _load_settings_or_exit(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Load settings, or exit(1) with a clear one-line message.

    Isolated from ``main()`` so it is directly unit-testable (via ``env``)
    without also invoking uvicorn — a missing required environment
    variable must fail loudly and clearly on stderr, never as a raw stack
    trace into uvicorn internals, and never silently swallowed.
    """
    try:
        return load_settings(env)
    except ConfigError as exc:
        print(f"cer.api: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


#: Stable ``event`` values on the startup artifact-store decision's log
#: records, so an operator can grep or alert on the data-loss one without
#: matching free text. Every path logs exactly one of these.
EVENT_ARTIFACT_STORE_PRESENT = "artifact_store.already_initialised"
EVENT_ARTIFACT_STORE_FIRST_DEPLOYMENT = "artifact_store.initialised_first_deployment"
EVENT_ARTIFACT_STORE_VOLUME_LOST = "artifact_store.volume_lost_refusing_to_initialise"
EVENT_ARTIFACT_STORE_UNDETERMINED = "artifact_store.state_undetermined_refusing_to_initialise"


def _metadata_holds_artifact_records(
    metadata_store: "MetadataStore",
) -> Optional[bool]:
    """Return whether the metadata store holds *any* artifact record.

    ``None`` means the question could not be answered — the metadata
    store itself failed. That is deliberately distinct from ``False``:
    "no artifacts were ever registered" licenses creating an artifact
    store from nothing, "I don't know" must not.

    Uses the existing ``query_artifacts`` (with ``limit=1``, so it never
    pages the whole table) rather than a new store method: the
    ``MetadataStore`` Protocol already answers this question, and this is
    not a reason to widen the storage boundary.
    """
    try:
        return bool(metadata_store.query_artifacts(limit=1))
    except Exception as exc:  # noqa: BLE001 - any failure means "unknown"
        logger.error(
            "could not determine whether artifacts were previously registered",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None


def _initialise_artifact_store_if_safe(
    metadata_store: "MetadataStore",
    artifact_store: "FilesystemArtifactStore",
) -> None:
    """Initialise the artifact store only when doing so cannot destroy evidence.

    Three cases, decided from the artifact store's own health plus the
    metadata store's artifact records (see the module docstring for why
    the artifact volume alone is not enough):

    1. **Already initialised** — ``health()`` passes. An ordinary
       restart; nothing to do.
    2. **Uninitialised, and no artifact was ever registered** — a genuine
       first deployment. ``initialise()`` as before.
    3. **Uninitialised, but artifact records exist** — the artifact
       volume was lost while the service was stopped. Refuse to
       initialise, and tell the store it *was* initialised
       (:meth:`~cer.artifacts.fs_store.FilesystemArtifactStore.mark_previously_initialised`)
       so the write and read paths refuse exactly as they already do for
       a volume that vanishes under a live process. ``/ready`` then
       reports 503 naming ``artifact_store``, and the metadata records
       stay readable so an operator can see precisely what was lost.

    Case 3 adds no new failure mode: it reuses machinery that already
    exists and already behaves correctly. The change is the decision
    *not* to paper over the condition.

    A failing ``health()`` that is not "never initialised" (e.g. the root
    exists but is not writable) also lands here, and lands correctly: the
    underlying error is logged, and either the store is left alone
    (artifacts exist) or ``initialise()`` runs as a harmless no-op over
    the existing layout and ``/ready`` still reports the real fault.
    """
    artifact_root = str(artifact_store.root)
    try:
        artifact_store.health()
    except Exception as exc:  # noqa: BLE001 - any failure means "not usable as-is"
        health_error = exc
    else:
        logger.info(
            "artifact store already initialised",
            extra={
                "event": EVENT_ARTIFACT_STORE_PRESENT,
                "artifact_root": artifact_root,
            },
        )
        return

    holds_artifacts = _metadata_holds_artifact_records(metadata_store)

    if holds_artifacts is False:
        artifact_store.initialise()
        logger.info(
            "artifact store initialised (first deployment: no artifact "
            "records exist in the metadata store)",
            extra={
                "event": EVENT_ARTIFACT_STORE_FIRST_DEPLOYMENT,
                "artifact_root": artifact_root,
                "artifact_store_error": str(health_error),
            },
        )
        return

    if holds_artifacts is True:
        artifact_store.mark_previously_initialised()
        logger.error(
            "artifact store is not initialised but the metadata store "
            "holds registered artifact records — this is a lost artifact "
            "volume, not a first deployment; refusing to initialise an "
            "empty artifact store over it. The service will start and "
            "report itself NOT READY (/ready 503, failed_dependency "
            "artifact_store) and refuse artifact writes until the "
            "artifact volume is restored. Artifact metadata remains "
            "readable, so the lost artifacts can be identified.",
            extra={
                "event": EVENT_ARTIFACT_STORE_VOLUME_LOST,
                "artifact_root": artifact_root,
                "artifact_store_error": str(health_error),
            },
        )
        return

    logger.error(
        "artifact store is not initialised and the metadata store could "
        "not be queried to tell a first deployment from a lost artifact "
        "volume; refusing to initialise. The service will start and "
        "report itself NOT READY until this is resolved.",
        extra={
            "event": EVENT_ARTIFACT_STORE_UNDETERMINED,
            "artifact_root": artifact_root,
            "artifact_store_error": str(health_error),
        },
    )


def _build_stores(settings: Settings) -> Tuple[object, object]:
    """Construct and initialise the concrete metadata/artifact stores.

    Isolated from ``main()`` so the PL-integration wiring itself (correct
    class names, correct constructor arguments, ``initialise()`` called
    before serving) is directly unit-testable against a real temporary
    filesystem, without also starting uvicorn.
    """
    from cer.artifacts import FilesystemArtifactStore
    from cer.metadata import SQLiteMetadataStore

    metadata_store = SQLiteMetadataStore(settings.metadata_db_path)
    artifact_store = FilesystemArtifactStore(
        settings.artifact_root, max_bytes=settings.max_artifact_bytes
    )
    # health() deliberately never creates anything (see fs_store.py) — the
    # store must be explicitly initialised once at startup, or /ready
    # reports 503 (failed_dependency: artifact_store) on every fresh
    # deployment even though nothing is actually wrong. That initialisation
    # is conditional, not unconditional: see
    # _initialise_artifact_store_if_safe and this module's docstring.
    _initialise_artifact_store_if_safe(metadata_store, artifact_store)

    return metadata_store, artifact_store


def main() -> None:
    # Configuration is read before logging is configured, so a missing
    # required variable is still reported clearly (see
    # _load_settings_or_exit) even though structured logging isn't up yet.
    settings = _load_settings_or_exit()

    configure_logging(settings.log_level)
    logger.info("cer.api starting", extra={"settings": settings.redacted()})

    metadata_store, artifact_store = _build_stores(settings)

    import uvicorn

    from cer.api.app import create_app

    app = create_app(metadata_store, artifact_store, settings)
    logger.info(
        "cer.api ready",
        extra={"host": settings.host, "port": settings.port},
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
