"""Structured, UTC, single-line JSON logging.

CER's PID requires structured logs and "UTC everywhere". This module
installs a JSON formatter on the root logger. Every emitted record is a
single-line JSON object carrying at least ``ts`` (ISO-8601 UTC), ``level``,
``logger``, ``msg``, ``module``, and ``line``; any additional fields passed
via ``extra={...}`` are merged in at the top level rather than nested.

Logging must never crash the service: non-JSON-serialisable extra values
are coerced to strings, and formatting itself is defensive so a bad record
degrades to a minimal fallback line instead of raising.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

from cer.runtime.clock import isoformat_utc, utcnow

__all__ = ["configure_logging", "JsonFormatter", "bind_context"]

# Attribute names present on a bare LogRecord — anything beyond these on a
# given record came from the caller's `extra={...}` and should be merged
# into the JSON payload at the top level. Derived from a live LogRecord
# (rather than hardcoded) so it stays correct across Python versions.
_RESERVED_ATTRS = set(
    logging.LogRecord(
        name="_cer_sample", level=logging.INFO, pathname="", lineno=0,
        msg="", args=(), exc_info=None,
    ).__dict__.keys()
)

# Marker attribute set on handlers we install, so configure_logging() can
# recognise its own handler and stay idempotent across repeated calls.
_HANDLER_MARKER = "_cer_json_handler"


def _json_default(value: Any) -> str:
    """Fallback for values json.dumps cannot serialise natively."""
    try:
        return str(value)
    except Exception:
        return "<unserialisable>"


class JsonFormatter(logging.Formatter):
    """Renders each LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload = self._build_payload(record)
            return json.dumps(payload, default=_json_default)
        except Exception:
            # Logging must never crash the service, no matter how odd
            # the record or its extras are.
            fallback = {
                "ts": isoformat_utc(utcnow()),
                "level": getattr(record, "levelname", "ERROR"),
                "logger": getattr(record, "name", "unknown"),
                "msg": "<log formatting failed>",
            }
            return json.dumps(fallback)

    def _build_payload(self, record: logging.LogRecord) -> dict:
        ts = isoformat_utc(datetime.fromtimestamp(record.created, tz=timezone.utc))
        payload: dict = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }

        # Merge caller-supplied extra fields (and any persistent context
        # bound via bind_context()) in at the top level.
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key in payload:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exception"] = "".join(
                traceback.format_exception(*record.exc_info)
            )
        elif record.exc_text:
            payload["exception"] = record.exc_text

        return payload


def configure_logging(level: str, stream: Optional[Any] = None) -> None:
    """Install a JSON formatter on the root logger.

    Idempotent: calling this more than once does not add duplicate
    handlers. A later call updates the installed handler's level (and,
    if a new stream is given, retargets it) rather than stacking another
    handler on top.
    """
    root = logging.getLogger()
    root.setLevel(level)

    target_stream = stream if stream is not None else sys.stderr

    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARKER, False):
            handler.setLevel(level)
            if stream is not None:
                handler.stream = stream
            return

    handler = logging.StreamHandler(target_stream)
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter())
    setattr(handler, _HANDLER_MARKER, True)
    root.addHandler(handler)


class _ContextLoggerAdapter(logging.LoggerAdapter):
    """Merges bound context fields into every record's `extra`."""

    def process(self, msg, kwargs):
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra", {}) or {})
        kwargs["extra"] = extra
        return msg, kwargs


def bind_context(logger: logging.Logger, **context: Any) -> logging.LoggerAdapter:
    """Return a logger-like adapter that stamps persistent context fields
    (e.g. request_id, producer) onto every record it emits.

    The returned adapter merges bound context with any per-call `extra=`,
    with a per-call `extra` field taking precedence over the bound value
    of the same name.
    """
    return _ContextLoggerAdapter(logger, context)
