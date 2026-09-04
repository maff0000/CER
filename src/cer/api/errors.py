"""Exception -> HTTP mapping for the CER API.

Every response this API returns for an error case is a structured JSON
body (:class:`cer.api.schemas.ErrorBody`): ``code`` (the CERError's stable,
machine-readable ``code`` attribute — never a string-match on the
exception's message), ``message`` (human-readable), and ``request_id``.

Mapping is by exception *type* (``isinstance``), most-specific first, since
several CERError subclasses share a base whose default mapping would be
wrong for the subclass (e.g. ``ChecksumMismatchError`` is an
``ArtifactStoreError`` but must map to 422, not 503).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from cer.contract.errors import (
    ArtifactStoreError,
    CERError,
    ChecksumMismatchError,
    ContractViolationError,
    IdempotencyConflictError,
    ImmutabilityError,
    MetadataStoreError,
    NotFoundError,
)

logger = logging.getLogger("cer.api")

REQUEST_ID_HEADER = "X-Request-Id"
REQUEST_ID_STATE_KEY = "cer_request_id"

#: Ordered (most-specific-first) exception-type -> HTTP status mapping.
#: ``ChecksumMismatchError`` (a subclass of ``ArtifactStoreError``) and
#: ``IdempotencyConflictError``/``ImmutabilityError`` (independent of
#: ``ContractViolationError``) are listed ahead of their more general
#: bases so ``isinstance``-based dispatch picks the right status.
_ERROR_STATUS_ORDER: tuple[tuple[type[CERError], int], ...] = (
    (ChecksumMismatchError, 422),
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (IdempotencyConflictError, status.HTTP_409_CONFLICT),
    (ImmutabilityError, status.HTTP_409_CONFLICT),
    (ContractViolationError, status.HTTP_400_BAD_REQUEST),
    (MetadataStoreError, status.HTTP_503_SERVICE_UNAVAILABLE),
    (ArtifactStoreError, status.HTTP_503_SERVICE_UNAVAILABLE),
    # Base CERError catch-all: anything raised as a bare CERError (not one
    # of the above) is treated as a caller-side contract problem.
    (CERError, status.HTTP_400_BAD_REQUEST),
)


def status_for(exc: CERError) -> int:
    """Return the HTTP status code for a CERError instance."""
    for exc_type, code in _ERROR_STATUS_ORDER:
        if isinstance(exc, exc_type):
            return code
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def get_request_id(request: Request) -> str:
    """Return the request id bound to this request (set by the request-id
    middleware in ``app.py``; falls back to minting one if, somehow, the
    middleware did not run — an exception handler must never crash for
    want of a request id)."""
    rid = getattr(request.state, REQUEST_ID_STATE_KEY, None)
    if rid:
        return rid
    return uuid.uuid4().hex


def _error_response(request: Request, *, status_code: int, code: str, message: str) -> JSONResponse:
    request_id = get_request_id(request)
    body = {"code": code, "message": message, "request_id": request_id}
    return JSONResponse(status_code=status_code, content=body, headers={REQUEST_ID_HEADER: request_id})


def install_error_handlers(app: FastAPI) -> None:
    """Register CER's exception -> HTTP mapping on ``app``."""

    @app.exception_handler(CERError)
    async def _cer_error_handler(request: Request, exc: CERError) -> JSONResponse:
        code_ = status_for(exc)
        if code_ >= 500:
            logger.error(
                "unhandled cer error",
                exc_info=exc,
                extra={"request_id": get_request_id(request), "cer_error_code": exc.code},
            )
        return _error_response(
            request,
            status_code=code_,
            code=exc.code,
            message=exc.message or str(exc),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI/pydantic request-body validation failures (malformed
        # provenance, wrong types, missing required fields) — a caller-side
        # contract violation, same family as CER's own ValidationError.
        return _error_response(
            request,
            status_code=422,
            code="validation_error",
            message=f"request validation failed: {exc.errors()!r}",
        )

    @app.exception_handler(PydanticValidationError)
    async def _pydantic_error_handler(request: Request, exc: PydanticValidationError) -> JSONResponse:
        # Raised when we construct a contract model ourselves (routes.py)
        # from an already-parsed request body and the contract model's own
        # validators reject it (e.g. an identity-shape violation not
        # caught by the API schema).
        return _error_response(
            request,
            status_code=status.HTTP_400_BAD_REQUEST,
            code="validation_error",
            message=f"contract model validation failed: {exc.errors()!r}",
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Framework-raised HTTP errors (404 route not found, 405, etc.) —
        # keep the structured body shape even for these.
        return _error_response(
            request,
            status_code=exc.status_code,
            code="http_error",
            message=str(exc.detail),
        )

    @app.exception_handler(Exception)
    async def _unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Anything unexpected: log the full traceback server-side, but
        # never leak it (or a filesystem path) into the response body.
        logger.error(
            "unhandled exception",
            exc_info=exc,
            extra={"request_id": get_request_id(request)},
        )
        return _error_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="an unexpected internal error occurred",
        )
