"""Tests for cer.runtime.clock."""

from datetime import datetime, timedelta, timezone

import pytest

from cer.runtime.clock import isoformat_utc, to_utc, utcnow


def test_utcnow_is_timezone_aware_utc():
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_to_utc_raises_on_naive_datetime():
    naive = datetime(2026, 9, 4, 12, 0, 0)
    with pytest.raises(ValueError):
        to_utc(naive)


def test_to_utc_normalizes_aware_non_utc_datetime():
    eastern = timezone(timedelta(hours=-5))
    dt = datetime(2026, 9, 4, 7, 30, 0, tzinfo=eastern)

    result = to_utc(dt)

    assert result.tzinfo == timezone.utc
    assert result == datetime(2026, 9, 4, 12, 30, 0, tzinfo=timezone.utc)


def test_to_utc_is_noop_for_already_utc_datetime():
    dt = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    result = to_utc(dt)
    assert result == dt
    assert result.tzinfo == timezone.utc


def test_isoformat_utc_produces_stable_z_suffixed_string():
    dt = datetime(2026, 9, 4, 12, 30, 45, 123456, tzinfo=timezone.utc)
    s = isoformat_utc(dt)
    assert s == "2026-09-04T12:30:45.123Z"


def test_isoformat_utc_converts_non_utc_input():
    eastern = timezone(timedelta(hours=-5))
    dt = datetime(2026, 9, 4, 7, 30, 0, tzinfo=eastern)
    s = isoformat_utc(dt)
    assert s == "2026-09-04T12:30:00.000Z"


def test_isoformat_utc_raises_on_naive_datetime():
    naive = datetime(2026, 9, 4, 12, 0, 0)
    with pytest.raises(ValueError):
        isoformat_utc(naive)


def test_utcnow_is_monkeypatchable(monkeypatch):
    fixed = datetime(2020, 1, 1, tzinfo=timezone.utc)

    import cer.runtime.clock as clock_module

    monkeypatch.setattr(clock_module, "utcnow", lambda: fixed)

    assert clock_module.utcnow() == fixed
