"""UTC-only clock utilities.

CER's PID mandates "UTC everywhere". This module gives every other
component one place to obtain the current time, normalise a datetime
to UTC, and render a stable ISO-8601 UTC string — and it is deliberately
a thin wrapper around ``datetime`` so callers can monkeypatch
``cer.runtime.clock.utcnow`` in tests to freeze time.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utcnow", "to_utc", "isoformat_utc"]


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Kept as a plain module-level function (rather than inlined at call
    sites) so tests can monkeypatch ``cer.runtime.clock.utcnow`` to
    freeze time.
    """
    return datetime.now(timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """Normalise an aware datetime to UTC.

    Raises ``ValueError`` if ``dt`` is naive (has no tzinfo/offset) —
    callers must be explicit about the source timezone. CER never
    guesses a timezone on a caller's behalf.
    """
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(
            "to_utc() requires a timezone-aware datetime; received a naive "
            "datetime. Callers must attach an explicit tzinfo before "
            "conversion."
        )
    return dt.astimezone(timezone.utc)


def isoformat_utc(dt: datetime) -> str:
    """Render a datetime as a stable ISO-8601 UTC string with a 'Z' suffix.

    The input is first normalised via :func:`to_utc` (so a naive datetime
    raises ``ValueError`` here too), then formatted with microsecond
    precision and a trailing ``Z`` instead of ``+00:00`` for a compact,
    unambiguous, and consistently-shaped string.
    """
    utc_dt = to_utc(dt)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
