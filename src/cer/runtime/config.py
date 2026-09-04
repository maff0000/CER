"""Runtime configuration loaded strictly from the process environment.

CER's PID is explicit that there must be no config or secrets in source
code and that runtime configuration remains external. This module is the
single place that reads environment variables; everything else in CER
receives an already-validated :class:`Settings` instance.

Values with no safe universal default (``metadata_db_path``,
``artifact_root``) are required — missing them raises :class:`ConfigError`
naming the exact variable. Values with a genuinely safe default live here
as named constants, not scattered magic numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Mapping, Optional

__all__ = ["Settings", "ConfigError", "load_settings"]


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed."""


# --- Named defaults -------------------------------------------------------
# These are the only defaults CER applies. Anything with no safe universal
# default (paths into a specific deployment's filesystem) is required
# instead of defaulted — see load_settings().
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_ENVIRONMENT = "dev"
DEFAULT_MAX_ARTIFACT_BYTES = 100 * 1024 * 1024  # 100 MiB

# Env var names, kept as constants so config.py is the one place they're
# spelled out.
ENV_METADATA_DB_PATH = "CER_METADATA_DB_PATH"
ENV_ARTIFACT_ROOT = "CER_ARTIFACT_ROOT"
ENV_HOST = "CER_HOST"
ENV_PORT = "CER_PORT"
ENV_LOG_LEVEL = "CER_LOG_LEVEL"
ENV_ENVIRONMENT = "CER_ENVIRONMENT"
ENV_MAX_ARTIFACT_BYTES = "CER_MAX_ARTIFACT_BYTES"

# A key whose name contains any of these (case-insensitive) is treated as
# secret-bearing and masked by Settings.redacted().
_SECRET_MARKERS = ("SECRET", "PASSWORD", "TOKEN", "KEY", "CREDENTIAL")
_REDACTED_VALUE = "***REDACTED***"


@dataclass(frozen=True)
class Settings:
    """Immutable, validated CER runtime configuration."""

    metadata_db_path: str
    artifact_root: str
    host: str
    port: int
    log_level: str
    environment: str
    max_artifact_bytes: int

    def redacted(self) -> dict:
        """Return a dict safe to log.

        Any field whose *name* contains a secret marker (SECRET, PASSWORD,
        TOKEN, KEY, CREDENTIAL — case-insensitive) is masked. None of
        Settings' current fields are secret-shaped, but this stays
        name-driven (not a hardcoded field list) so it keeps working if a
        secret-shaped field is ever added, and CER must never persist or
        log secrets.
        """
        result = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if _looks_secret(f.name):
                result[f.name] = _REDACTED_VALUE
            else:
                result[f.name] = value
        return result


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in _SECRET_MARKERS)


def _require(env: Mapping[str, str], var_name: str) -> str:
    value = env.get(var_name)
    if value is None or value == "":
        raise ConfigError(
            f"Missing required environment variable: {var_name}"
        )
    return value


def _get_int(env: Mapping[str, str], var_name: str, default: int) -> int:
    raw = env.get(var_name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"Invalid value for {var_name}: {raw!r} (expected an integer)"
        ) from exc


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Build a validated :class:`Settings` from environment variables.

    Reads from ``os.environ`` by default; pass an explicit mapping (e.g.
    in tests) to avoid ever mutating global process state.
    """
    source: Mapping[str, str] = env if env is not None else os.environ

    metadata_db_path = _require(source, ENV_METADATA_DB_PATH)
    artifact_root = _require(source, ENV_ARTIFACT_ROOT)

    host = source.get(ENV_HOST) or DEFAULT_HOST
    log_level = source.get(ENV_LOG_LEVEL) or DEFAULT_LOG_LEVEL
    environment = source.get(ENV_ENVIRONMENT) or DEFAULT_ENVIRONMENT

    port = _get_int(source, ENV_PORT, DEFAULT_PORT)
    max_artifact_bytes = _get_int(
        source, ENV_MAX_ARTIFACT_BYTES, DEFAULT_MAX_ARTIFACT_BYTES
    )

    return Settings(
        metadata_db_path=metadata_db_path,
        artifact_root=artifact_root,
        host=host,
        port=port,
        log_level=log_level,
        environment=environment,
        max_artifact_bytes=max_artifact_bytes,
    )
