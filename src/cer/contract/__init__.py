"""CER versioned contract layer: the pure-domain surface every CER
component (HTTP service, storage backends, producer clients) is built
against.

This package implements no storage, no HTTP, and no I/O — only identity,
enums, errors, pydantic models and storage ``Protocol`` interfaces.
"""

from __future__ import annotations

from .enums import (
    EVIDENCE_TYPE_GROUP,
    EvidenceGroup,
    EvidenceType,
    HealthState,
    ProvenanceCompleteness,
    PromotionState,
    RunStatus,
    evidence_group_of,
    parse_evidence_type,
)
from .errors import (
    ArtifactStoreError,
    CERError,
    ChecksumMismatchError,
    ContractViolationError,
    IdempotencyConflictError,
    IdentityError,
    ImmutabilityError,
    IncompatibleSchemaVersionError,
    MetadataStoreError,
    NotFoundError,
    UnknownEvidenceTypeError,
    ValidationError,
)
from .identity import (
    ID_KINDS,
    V1_ID_KINDS,
    IdKind,
    generate_id,
    new_artifact_id,
    new_evidence_id,
    new_experiment_id,
    new_run_id,
    validate_artifact_id,
    validate_evidence_id,
    validate_experiment_id,
    validate_generated_id,
    validate_id,
    validate_logical_id,
    validate_run_id,
    validate_strategy_id,
    validate_strategy_version,
)
from .models import (
    REQUIRED_PROVENANCE_FIELDS,
    REQUIRED_RUN_PROVENANCE_FIELDS,
    ArtifactRecord,
    CERModel,
    Experiment,
    EvidenceRecord,
    MetricValue,
    PromotionTransition,
    Run,
    Strategy,
    StrategyHealthRecord,
    StrategyVersion,
    UTCDateTime,
    compute_provenance_completeness,
    compute_run_provenance_completeness,
)
from .stores import ArtifactStore, MetadataStore
from .version import (
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    validate_contract_version,
    validate_schema_version,
)

__all__ = [
    # version
    "CONTRACT_VERSION",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "validate_contract_version",
    "validate_schema_version",
    # enums
    "EvidenceType",
    "EvidenceGroup",
    "EVIDENCE_TYPE_GROUP",
    "evidence_group_of",
    "parse_evidence_type",
    "PromotionState",
    "HealthState",
    "RunStatus",
    "ProvenanceCompleteness",
    # errors
    "CERError",
    "ContractViolationError",
    "IncompatibleSchemaVersionError",
    "UnknownEvidenceTypeError",
    "IdentityError",
    "ValidationError",
    "ImmutabilityError",
    "IdempotencyConflictError",
    "NotFoundError",
    "MetadataStoreError",
    "ArtifactStoreError",
    "ChecksumMismatchError",
    # identity
    "IdKind",
    "ID_KINDS",
    "V1_ID_KINDS",
    "generate_id",
    "validate_id",
    "validate_generated_id",
    "validate_logical_id",
    "validate_strategy_id",
    "validate_strategy_version",
    "validate_experiment_id",
    "validate_run_id",
    "validate_evidence_id",
    "validate_artifact_id",
    "new_experiment_id",
    "new_run_id",
    "new_evidence_id",
    "new_artifact_id",
    # models
    "CERModel",
    "UTCDateTime",
    "MetricValue",
    "Strategy",
    "StrategyVersion",
    "Experiment",
    "Run",
    "EvidenceRecord",
    "ArtifactRecord",
    "PromotionTransition",
    "StrategyHealthRecord",
    "REQUIRED_PROVENANCE_FIELDS",
    "REQUIRED_RUN_PROVENANCE_FIELDS",
    "compute_provenance_completeness",
    "compute_run_provenance_completeness",
    # stores
    "ArtifactStore",
    "MetadataStore",
]
