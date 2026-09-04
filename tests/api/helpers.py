"""Shared test helpers for tests/api."""

from __future__ import annotations

from cer.contract.version import CONTRACT_VERSION

CONTRACT_HEADERS = {"X-CER-Contract-Version": CONTRACT_VERSION}


def headers(**extra: str) -> dict[str, str]:
    merged = dict(CONTRACT_HEADERS)
    merged.update(extra)
    return merged
