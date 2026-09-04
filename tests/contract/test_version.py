import pytest

from cer.contract.errors import IncompatibleSchemaVersionError
from cer.contract.version import (
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    validate_contract_version,
    validate_schema_version,
)


def test_current_schema_version_is_valid():
    assert validate_schema_version(SCHEMA_VERSION) == SCHEMA_VERSION


def test_current_contract_version_is_valid():
    assert validate_contract_version(CONTRACT_VERSION) == CONTRACT_VERSION


@pytest.mark.parametrize("bad", [0, -1, 2, 999, 1.0, "1", None, True])
def test_incompatible_schema_version_raises_loudly(bad):
    with pytest.raises(IncompatibleSchemaVersionError):
        validate_schema_version(bad)


@pytest.mark.parametrize("bad", ["", "   ", "not-a-version", "2.0.0", "99.0.0", None, 1])
def test_incompatible_contract_version_raises_loudly(bad):
    with pytest.raises(IncompatibleSchemaVersionError):
        validate_contract_version(bad)


def test_contract_version_same_major_is_compatible():
    assert validate_contract_version("1.0.0") == "1.0.0"
    assert validate_contract_version("1.9.3") == "1.9.3"


def test_no_silent_coercion_never_returns_default_on_bad_input():
    # An incompatible input must never be silently replaced with the
    # current version -- it must raise, full stop.
    with pytest.raises(IncompatibleSchemaVersionError):
        validate_schema_version(2)
    with pytest.raises(IncompatibleSchemaVersionError):
        validate_contract_version("2.0.0")
