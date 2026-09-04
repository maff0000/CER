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
"""

from __future__ import annotations

import logging
import sys
from typing import Mapping, Optional, Tuple

from cer.runtime.config import ConfigError, Settings, load_settings
from cer.runtime.logging import configure_logging

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
    # deployment even though nothing is actually wrong.
    artifact_store.initialise()

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
