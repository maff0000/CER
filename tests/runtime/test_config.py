"""Tests for cer.runtime.config."""

import pytest

from cer.runtime.config import (
    DEFAULT_ENVIRONMENT,
    DEFAULT_HOST,
    DEFAULT_LOG_LEVEL,
    DEFAULT_MAX_ARTIFACT_BYTES,
    DEFAULT_PORT,
    ConfigError,
    Settings,
    load_settings,
)

REQUIRED_ENV = {
    "CER_METADATA_DB_PATH": "/var/lib/cer/metadata.sqlite3",
    "CER_ARTIFACT_ROOT": "/var/lib/cer/artifacts",
}


def test_load_settings_with_required_only_applies_defaults():
    settings = load_settings(REQUIRED_ENV)

    assert settings.metadata_db_path == "/var/lib/cer/metadata.sqlite3"
    assert settings.artifact_root == "/var/lib/cer/artifacts"
    assert settings.host == DEFAULT_HOST
    assert settings.port == DEFAULT_PORT
    assert settings.log_level == DEFAULT_LOG_LEVEL
    assert settings.environment == DEFAULT_ENVIRONMENT
    assert settings.max_artifact_bytes == DEFAULT_MAX_ARTIFACT_BYTES


def test_load_settings_overrides_defaults_from_env():
    env = dict(REQUIRED_ENV)
    env.update(
        {
            "CER_HOST": "127.0.0.1",
            "CER_PORT": "9001",
            "CER_LOG_LEVEL": "DEBUG",
            "CER_ENVIRONMENT": "prod",
            "CER_MAX_ARTIFACT_BYTES": "12345",
        }
    )
    settings = load_settings(env)

    assert settings.host == "127.0.0.1"
    assert settings.port == 9001
    assert settings.log_level == "DEBUG"
    assert settings.environment == "prod"
    assert settings.max_artifact_bytes == 12345


@pytest.mark.parametrize("missing_var", ["CER_METADATA_DB_PATH", "CER_ARTIFACT_ROOT"])
def test_missing_required_variable_raises_config_error_naming_it(missing_var):
    env = dict(REQUIRED_ENV)
    del env[missing_var]

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env)

    assert missing_var in str(exc_info.value)


def test_missing_required_variable_empty_string_also_raises():
    env = dict(REQUIRED_ENV)
    env["CER_ARTIFACT_ROOT"] = ""

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env)

    assert "CER_ARTIFACT_ROOT" in str(exc_info.value)


def test_bad_port_raises_config_error_naming_variable_and_value():
    env = dict(REQUIRED_ENV)
    env["CER_PORT"] = "not-a-port"

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env)

    message = str(exc_info.value)
    assert "CER_PORT" in message
    assert "not-a-port" in message


def test_bad_max_artifact_bytes_raises_config_error():
    env = dict(REQUIRED_ENV)
    env["CER_MAX_ARTIFACT_BYTES"] = "huge"

    with pytest.raises(ConfigError) as exc_info:
        load_settings(env)

    message = str(exc_info.value)
    assert "CER_MAX_ARTIFACT_BYTES" in message
    assert "huge" in message


def test_load_settings_does_not_mutate_os_environ(monkeypatch):
    # Ensure the real environment is untouched by using an explicit mapping
    # and confirming os.environ never gets these keys.
    import os

    monkeypatch.delenv("CER_METADATA_DB_PATH", raising=False)
    load_settings(REQUIRED_ENV)
    assert "CER_METADATA_DB_PATH" not in os.environ


def test_settings_is_frozen():
    settings = load_settings(REQUIRED_ENV)
    with pytest.raises(Exception):
        settings.host = "changed"  # type: ignore[misc]


def test_redacted_returns_all_fields_unmasked_when_none_are_secret_shaped():
    settings = load_settings(REQUIRED_ENV)
    # None of Settings' real fields are secret-shaped today; redacted()
    # must still return every field, none masked, proving it's name-driven
    # rather than hardcoded to mask everything.
    redacted = settings.redacted()
    assert redacted["metadata_db_path"] == settings.metadata_db_path
    assert redacted["host"] == settings.host
    assert redacted["port"] == settings.port


def test_looks_secret_classifies_field_names_by_marker():
    from cer.runtime.config import _looks_secret

    assert _looks_secret("api_key")
    assert _looks_secret("CER_SECRET_TOKEN")
    assert _looks_secret("password")
    assert _looks_secret("db_credential")
    assert not _looks_secret("host")
    assert not _looks_secret("metadata_db_path")


def test_redacted_masks_a_secret_shaped_field_through_settings_own_method():
    """The masking branch of the REAL ``Settings.redacted()``.

    No field on ``Settings`` is secret-shaped by name today, so on a plain
    ``Settings`` instance that branch never executes and the test above
    only covers the pass-through half.

    This exercises it on a *subclass* that adds a secret-shaped field.
    ``redacted()`` is inherited, not reimplemented, so the method under
    test is genuinely ``Settings.redacted`` -- and because it is
    ``dataclasses.fields``-driven rather than a hardcoded field list, it
    sees the added field. That is precisely the situation the method's own
    docstring promises to handle ("keeps working if a secret-shaped field
    is ever added").

    An earlier version of this test copied ``redacted()``'s body into a
    standalone dataclass, so a bug introduced in ``Settings.redacted``
    itself -- masking nothing, or masking everything -- could not fail it.
    """
    from dataclasses import dataclass, fields

    from cer.runtime.config import _REDACTED_VALUE

    @dataclass(frozen=True)
    class _SettingsWithSecret(Settings):
        api_key: str = "super-secret-value"

    base = load_settings(REQUIRED_ENV)
    settings = _SettingsWithSecret(**{f.name: getattr(base, f.name) for f in fields(base)})

    # The real method, not a copy of it.
    assert type(settings).redacted is Settings.redacted
    redacted = settings.redacted()

    # The secret-shaped field is masked...
    assert redacted["api_key"] == _REDACTED_VALUE
    assert "super-secret-value" not in str(redacted)

    # ...and the same call leaves every non-secret field untouched, which
    # is what distinguishes real name-driven masking from "mask
    # everything".
    assert redacted["host"] == settings.host
    assert redacted["port"] == settings.port
    assert redacted["metadata_db_path"] == settings.metadata_db_path
    assert redacted["artifact_root"] == settings.artifact_root

    # Every field is reported, none dropped.
    assert set(redacted) == {f.name for f in fields(settings)}
