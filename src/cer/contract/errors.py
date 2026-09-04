"""CER contract exception hierarchy.

All CER exceptions derive from :class:`CERError`. Every exception carries a
stable, machine-readable ``code`` attribute so an HTTP/API layer can map
errors to responses by attribute, not by string-matching exception messages
or class names.

The exception names in this module are a fixed interface: other CER
components (storage backends, the HTTP service, producer clients) import
them by name. Do not rename them.
"""

from __future__ import annotations


class CERError(Exception):
    """Base class for all CER contract errors.

    Subclasses must set a class-level ``code`` (a short, stable,
    machine-readable string, e.g. ``"immutability_violation"``). The code is
    exposed as an instance attribute for callers that catch the base class.
    """

    code: str = "cer_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class ContractViolationError(CERError):
    """A caller violated the CER producer/consumer contract."""

    code = "contract_violation"


class IncompatibleSchemaVersionError(ContractViolationError):
    """An inbound schema_version/contract_version is incompatible."""

    code = "incompatible_schema_version"


class UnknownEvidenceTypeError(ContractViolationError):
    """An evidence_type string is not one of the controlled classes.

    Unknown ad-hoc evidence types must fail loudly rather than being
    accepted or coerced to a default.
    """

    code = "unknown_evidence_type"


class IdentityError(ContractViolationError):
    """An identifier (strategy_id, run_id, evidence_id, ...) is invalid."""

    code = "identity_error"


class ValidationError(ContractViolationError):
    """A record failed contract-level validation."""

    code = "validation_error"


class ImmutabilityError(CERError):
    """An attempt was made to silently replace an immutable record.

    Evidence and artifacts are immutable once written; this is raised
    instead of allowing silent overwrite/rewrite.
    """

    code = "immutability_violation"


class IdempotencyConflictError(CERError):
    """A replayed idempotency key was reused with a conflicting body.

    A replay with an identical body under the same idempotency key must
    return the existing record; a replay with a *different* body under the
    same key raises this instead.
    """

    code = "idempotency_conflict"


class NotFoundError(CERError):
    """A requested record does not exist."""

    code = "not_found"


class MetadataStoreError(CERError):
    """The metadata store is unavailable or failed an operation."""

    code = "metadata_store_error"


class ArtifactStoreError(CERError):
    """The artifact store is unavailable or failed an operation."""

    code = "artifact_store_error"


class ChecksumMismatchError(ArtifactStoreError):
    """A declared/expected checksum did not match the computed checksum."""

    code = "checksum_mismatch"
