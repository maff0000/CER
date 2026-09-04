"""Uvicorn entrypoint: ``python -m cer.api.main``.

Reads all runtime configuration from the environment via
``cer.runtime.config.load_settings()`` (fails loudly if required
configuration is missing — no defaults or secrets embedded in source) and
configures structured logging via ``cer.runtime.logging.configure_logging()``
before constructing the app.

The concrete ``MetadataStore``/``ArtifactStore`` implementations
(``cer.metadata`` / ``cer.artifacts``) are NOT present in this worktree —
they are being built in parallel by other Engineers. The import is
deliberately deferred to inside ``main()`` so importing this module (or any
other part of ``cer.api``) never requires those packages to exist; tests
exercise ``create_app`` directly against in-memory fakes instead.
"""

from __future__ import annotations

import logging

from cer.runtime.config import load_settings
from cer.runtime.logging import configure_logging

logger = logging.getLogger("cer.api.main")


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    logger.info("cer.api starting", extra={"settings": settings.redacted()})

    # TODO(PL-integration): wire the real backends once cer.metadata and
    # cer.artifacts exist in the integrated repository. Imported lazily,
    # here only, so this module (and the rest of cer.api) never depends on
    # packages absent from this worktree.
    from cer.artifacts import FilesystemArtifactStore  # type: ignore[import-not-found]
    from cer.metadata import SqliteMetadataStore  # type: ignore[import-not-found]

    metadata_store = SqliteMetadataStore(settings.metadata_db_path)
    artifact_store = FilesystemArtifactStore(settings.artifact_root)

    import uvicorn

    from cer.api.app import create_app

    app = create_app(metadata_store, artifact_store, settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
