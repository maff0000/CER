"""Contract and schema versioning for CER.

CER's producer/consumer contract is versioned explicitly. ``CONTRACT_VERSION``
identifies the overall contract (this package's public surface);
``SCHEMA_VERSION`` identifies the shape of persisted records (e.g.
``EvidenceRecord.schema_version``).

Per the PID's contract-gate acceptance criterion, incompatible schema or
contract version inputs must fail loudly — never be coerced, defaulted, or
silently accepted with a warning.
"""

from __future__ import annotations

from .errors import IncompatibleSchemaVersionError

#: The current contract version, in semver form. Producers/consumers pin to
#: this to know which capabilities and record shapes are available.
CONTRACT_VERSION: str = "1.0.0"

#: The current schema version for persisted records. Bumped whenever the
#: shape of a persisted record changes in a way that is not purely additive
#: and backward compatible.
SCHEMA_VERSION: int = 1

#: Schema versions this build of the contract can read/write. v1 supports
#: exactly the current schema version; future versions may widen this.
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({SCHEMA_VERSION})


def validate_schema_version(schema_version: int) -> int:
    """Validate an inbound ``schema_version``.

    Raises ``IncompatibleSchemaVersionError`` loudly if the value is not an
    integer, or is not one of ``SUPPORTED_SCHEMA_VERSIONS``. Never coerces,
    defaults, or warns-and-continues.
    """
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise IncompatibleSchemaVersionError(
            f"schema_version must be an int, got {type(schema_version).__name__}"
            f" ({schema_version!r})"
        )
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise IncompatibleSchemaVersionError(
            f"unsupported schema_version {schema_version!r}; "
            f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)!r}"
        )
    return schema_version


def validate_contract_version(contract_version: str) -> str:
    """Validate an inbound ``contract_version`` string.

    A caller is considered compatible if it declares the same major version
    as ``CONTRACT_VERSION``. Anything else (missing, malformed, or a
    different major version) fails loudly.
    """
    if not isinstance(contract_version, str) or not contract_version.strip():
        raise IncompatibleSchemaVersionError(
            f"contract_version must be a non-empty string, got {contract_version!r}"
        )

    def _major(v: str) -> int:
        parts = v.strip().split(".")
        if not parts or not parts[0].isdigit():
            raise IncompatibleSchemaVersionError(
                f"contract_version is not a valid semver string: {v!r}"
            )
        return int(parts[0])

    caller_major = _major(contract_version)
    current_major = _major(CONTRACT_VERSION)
    if caller_major != current_major:
        raise IncompatibleSchemaVersionError(
            f"incompatible contract_version {contract_version!r}; "
            f"this build supports major version {current_major} "
            f"(current: {CONTRACT_VERSION!r})"
        )
    return contract_version
