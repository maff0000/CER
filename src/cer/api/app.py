"""CER FastAPI application factory.

``create_app`` wires a :class:`~cer.contract.stores.MetadataStore` and a
:class:`~cer.contract.stores.ArtifactStore` (dependency injection — this
module never constructs a concrete backend itself) plus runtime
:class:`~cer.runtime.config.Settings` into a FastAPI app: routes, error
mapping, and a request-id/structured-logging middleware.
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import Response

from cer.contract.stores import ArtifactStore, MetadataStore
from cer.runtime.config import Settings

from .errors import REQUEST_ID_HEADER, REQUEST_ID_STATE_KEY, install_error_handlers
from .routes import build_router, health_router

logger = logging.getLogger("cer.api.request")

#: Inbound header a caller may set to propagate its own request id.
INBOUND_REQUEST_ID_HEADER = "X-Request-Id"


def create_app(
    metadata_store: MetadataStore,
    artifact_store: ArtifactStore,
    settings: Settings,
) -> FastAPI:
    """Build and return the CER FastAPI application.

    ``metadata_store`` and ``artifact_store`` must satisfy the
    ``cer.contract.stores`` Protocols. They are stored on ``app.state`` and
    resolved per-request via FastAPI dependencies in ``routes.py`` — this
    factory does no I/O of its own.
    """
    app = FastAPI(title="CER API", version=settings.environment)

    app.state.metadata_store = metadata_store
    app.state.artifact_store = artifact_store
    app.state.settings = settings

    install_error_handlers(app)

    app.include_router(health_router)
    app.include_router(build_router(), prefix="/v1")

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        request_id = request.headers.get(INBOUND_REQUEST_ID_HEADER) or uuid.uuid4().hex
        setattr(request.state, REQUEST_ID_STATE_KEY, request_id)
        start = time.monotonic()
        response: Response = await call_next(request)
        duration_ms = (time.monotonic() - start) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )
        return response

    return app
