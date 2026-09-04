"""CER pydantic v2 domain models.

All models forbid unknown fields (``extra="forbid"``) so malformed or
misspelled provenance fails loudly at construction time rather than being
silently dropped. All timestamps are timezone-aware UTC ``datetime``:
naive datetimes are rejected, and aware-but-non-UTC datetimes are
normalised to UTC.

Identity fields are validated through :mod:`cer.contract.identity`;
``evidence_type`` is validated through
:func:`cer.contract.enums.parse_evidence_type` (raising
``UnknownEvidenceTypeError`` loudly for unknown classes, never falling back
to a default).

Provenance completeness (see :func:`compute_provenance_completeness`) is
computed automatically for every :class:`EvidenceRecord` at construction
time and stored on the record itself (``provenance_completeness``,
``missing_provenance``) — it is a first-class, queryable property of the
record, never an inference callers have to derive themselves.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import EvidenceType, HealthState, ProvenanceCompleteness, PromotionState, RunStatus, parse_evidence_type
from .identity import (
    validate_artifact_id,
    validate_evidence_id,
    validate_experiment_id,
    validate_run_id,
    validate_strategy_id,
    validate_strategy_version,
)

# --- Shared UTC-datetime type -----------------------------------------------


def _reject_naive_and_normalize_utc(v: datetime) -> datetime:
    """Reject naive datetimes; normalise aware datetimes to UTC."""
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError(
            "datetime must be timezone-aware (naive datetimes are rejected); "
            f"got {v!r}"
        )
    return v.astimezone(timezone.utc)


#: A timezone-aware datetime, always normalised to UTC on validation. Naive
#: datetimes are rejected loudly (pydantic ValidationError).
UTCDateTime = Annotated[datetime, AfterValidator(_reject_naive_and_normalize_utc)]


class CERModel(BaseModel):
    """Base class for all CER contract models: unknown fields fail loudly."""

    model_config = ConfigDict(extra="forbid")


# --- Optional-identity validation helpers -----------------------------------


def _opt_strategy_id(v: Optional[str]) -> Optional[str]:
    return v if v is None else validate_strategy_id(v)


def _opt_strategy_version(v: Optional[str]) -> Optional[str]:
    return v if v is None else validate_strategy_version(v)


def _opt_experiment_id(v: Optional[str]) -> Optional[str]:
    return v if v is None else validate_experiment_id(v)


def _opt_run_id(v: Optional[str]) -> Optional[str]:
    return v if v is None else validate_run_id(v)


# --- Strategy / StrategyVersion ---------------------------------------------


class Strategy(CERModel):
    """A stable logical strategy identity. Never reused for a different thesis."""

    strategy_id: str
    name: str
    thesis: str
    created_at: UTCDateTime

    @field_validator("strategy_id")
    @classmethod
    def _v_strategy_id(cls, v: str) -> str:
        return validate_strategy_id(v)

    @field_validator("name", "thesis")
    @classmethod
    def _v_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be empty")
        return v


class StrategyVersion(CERModel):
    """An immutable version of one strategy. Any logic change creates a new version."""

    strategy_id: str
    strategy_version: str
    git_repo: str
    git_commit: str
    created_at: UTCDateTime
    notes: Optional[str] = None

    @field_validator("strategy_id")
    @classmethod
    def _v_strategy_id(cls, v: str) -> str:
        return validate_strategy_id(v)

    @field_validator("strategy_version")
    @classmethod
    def _v_strategy_version(cls, v: str) -> str:
        return validate_strategy_version(v)

    @field_validator("git_repo", "git_commit")
    @classmethod
    def _v_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be empty")
        return v


# --- Experiment / Run --------------------------------------------------------


class Experiment(CERModel):
    """Groups one research/validation objective."""

    experiment_id: str
    objective: str
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    producer: str
    created_at: UTCDateTime

    @field_validator("experiment_id")
    @classmethod
    def _v_experiment_id(cls, v: str) -> str:
        return validate_experiment_id(v)

    @field_validator("strategy_id")
    @classmethod
    def _v_strategy_id(cls, v: Optional[str]) -> Optional[str]:
        return _opt_strategy_id(v)

    @field_validator("strategy_version")
    @classmethod
    def _v_strategy_version(cls, v: Optional[str]) -> Optional[str]:
        return _opt_strategy_version(v)

    @field_validator("objective", "producer")
    @classmethod
    def _v_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be empty")
        return v


def _is_absent(value: object) -> bool:
    """Whether a provenance field value counts as "not recorded"."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _completeness_for(
    obj: object, fields: tuple[str, ...]
) -> tuple[ProvenanceCompleteness, list[str]]:
    """Shared core: which of ``fields`` on ``obj`` are absent (per
    :func:`_is_absent`), and the resulting overall completeness.

    Both :func:`compute_provenance_completeness` (EvidenceRecord) and
    :func:`compute_run_provenance_completeness` (Run) are thin wrappers
    around this so the two models don't duplicate the same logic.
    """
    missing = [f for f in fields if _is_absent(getattr(obj, f))]
    completeness = (
        ProvenanceCompleteness.INCOMPLETE if missing else ProvenanceCompleteness.COMPLETE
    )
    return completeness, missing


#: Fields on Run that capture reproducibility context rather than identity/
#: ownership. Not every run has all of these applicable at creation time
#: (e.g. a NEO surveillance run, a SOURCE_ANALYSIS run, or a DRIFT_ALERT
#: sweep may have no dataset at all) -- per the PID, an absent value here
#: must be recorded as explicitly absent (``None``), never guessed or
#: filled with a placeholder. See :func:`compute_run_provenance_completeness`.
REQUIRED_RUN_PROVENANCE_FIELDS: tuple[str, ...] = (
    "producer_version",
    "git_repo",
    "git_commit",
    "dataset_id",
    "dataset_version",
    "dataset_ref",
    "config_hash",
    "config_ref",
    "environment",
)


class Run(CERModel):
    """A unique execution of an experiment.

    ``run_id``, ``experiment_id``, ``status``, ``started_at`` and
    ``producer`` are identity/ownership -- a run cannot be meaningfully
    created without them, so they are required. The reproducibility-context
    fields (see :data:`REQUIRED_RUN_PROVENANCE_FIELDS`) are optional: not
    every run has a dataset, config or even a git commit (e.g. a NEO
    surveillance run). Their completeness is computed automatically and
    stored on ``provenance_completeness`` / ``missing_provenance`` -- see
    :func:`compute_run_provenance_completeness`. ``ended_at`` is also
    optional (a run may still be open).
    """

    run_id: str
    experiment_id: str
    status: RunStatus
    started_at: UTCDateTime
    ended_at: Optional[UTCDateTime] = None
    producer: str
    producer_version: Optional[str] = None
    git_repo: Optional[str] = None
    git_commit: Optional[str] = None
    dataset_id: Optional[str] = None
    dataset_version: Optional[str] = None
    dataset_ref: Optional[str] = None
    config_hash: Optional[str] = None
    config_ref: Optional[str] = None
    environment: Optional[str] = None

    provenance_completeness: ProvenanceCompleteness = ProvenanceCompleteness.INCOMPLETE
    missing_provenance: list[str] = Field(default_factory=list)

    @field_validator("run_id")
    @classmethod
    def _v_run_id(cls, v: str) -> str:
        return validate_run_id(v)

    @field_validator("experiment_id")
    @classmethod
    def _v_experiment_id(cls, v: str) -> str:
        return validate_experiment_id(v)

    @field_validator("producer")
    @classmethod
    def _v_producer_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be empty")
        return v

    @field_validator(
        "producer_version",
        "git_repo",
        "git_commit",
        "dataset_id",
        "dataset_version",
        "dataset_ref",
        "config_hash",
        "config_ref",
        "environment",
    )
    @classmethod
    def _v_optional_non_empty_if_present(cls, v: Optional[str]) -> Optional[str]:
        # Absence is expressed as None; an explicitly-passed empty string is
        # not a silent blank -- it still fails loudly.
        if v is None:
            return None
        if not v.strip():
            raise ValueError("must not be empty when present (use None for absent)")
        return v

    @model_validator(mode="after")
    def _apply_provenance_completeness(self) -> "Run":
        completeness, missing = compute_run_provenance_completeness(self)
        self.provenance_completeness = completeness
        self.missing_provenance = missing
        return self


def compute_run_provenance_completeness(run: Run) -> tuple[ProvenanceCompleteness, list[str]]:
    """Compute provenance completeness for a run.

    Returns ``(ProvenanceCompleteness.COMPLETE, [])`` if every field in
    :data:`REQUIRED_RUN_PROVENANCE_FIELDS` is present and non-empty,
    otherwise ``(ProvenanceCompleteness.INCOMPLETE, [<missing field
    names>])``. Never guesses or backfills a missing value — it only
    reports absence. Identity/ownership fields (run_id, experiment_id,
    status, started_at, producer) are not part of this check: they are
    required by the schema itself and cannot be absent on a validly
    constructed ``Run``.
    """
    return _completeness_for(run, REQUIRED_RUN_PROVENANCE_FIELDS)


# --- EvidenceRecord -----------------------------------------------------------

#: Metric values a producer may record: numeric, string, or boolean.
MetricValue = Union[float, int, str, bool]

#: Fields the PID's "Required provenance" section lists that are not
#: guaranteed to be non-empty by the schema itself (the always-present core
#: fields — evidence_id, evidence_type, created_at_utc, producer,
#: schema_version, idempotency_key — are excluded because they cannot be
#: absent on a validly-constructed record). This is the set that
#: :func:`compute_provenance_completeness` inspects.
#:
#: Deliberately excluded: ``parent_evidence_ids``, ``parent_run_ids`` and
#: ``artifact_ids``. The PID lists "parent evidence/run references" and
#: "artifact references" under required provenance, but its own wording is
#: "where applicable" -- a root evidence record with no parents, or one
#: registered before any artifact is attached, has legitimately empty
#: lineage/attachment links rather than a reproducibility gap. Treating an
#: empty link list as "missing" would flag the overwhelming majority of
#: valid root evidence as incomplete and swamp the genuinely useful signal
#: (missing code commit, dataset ref, config identity, environment,
#: metrics, ...). See the Engineer's report for this judgment call.
REQUIRED_PROVENANCE_FIELDS: tuple[str, ...] = (
    "strategy_id",
    "strategy_version",
    "experiment_id",
    "run_id",
    "producer_version",
    "observed_at_utc",
    "git_repo",
    "git_commit",
    "dataset_id",
    "dataset_version",
    "dataset_ref",
    "config_hash",
    "config_ref",
    "environment",
    "instruments",
    "timeframes",
    "metrics",
    "verdict",
    "status",
    "regime_tags",
    "notes",
)


class EvidenceRecord(CERModel):
    """An immutable, append-oriented evidence record.

    Provenance completeness is computed automatically (see
    :func:`compute_provenance_completeness`) and stored on
    ``provenance_completeness`` / ``missing_provenance``; any value passed
    in for those two fields at construction time is ignored and
    recomputed, since they are derived, never authored.
    """

    evidence_id: str
    idempotency_key: str
    evidence_type: EvidenceType
    schema_version: int

    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    experiment_id: Optional[str] = None
    run_id: Optional[str] = None

    producer: str
    producer_version: Optional[str] = None

    created_at_utc: UTCDateTime
    observed_at_utc: Optional[UTCDateTime] = None

    git_repo: Optional[str] = None
    git_commit: Optional[str] = None

    dataset_id: Optional[str] = None
    dataset_version: Optional[str] = None
    dataset_ref: Optional[str] = None

    config_hash: Optional[str] = None
    config_ref: Optional[str] = None

    environment: Optional[str] = None

    instruments: list[str] = Field(default_factory=list)
    timeframes: list[str] = Field(default_factory=list)

    metrics: dict[str, MetricValue] = Field(default_factory=dict)

    verdict: Optional[str] = None
    status: Optional[str] = None

    regime_tags: list[str] = Field(default_factory=list)

    parent_evidence_ids: list[str] = Field(default_factory=list)
    parent_run_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)

    #: Structured notes/observations — a dict or list of structured
    #: observations, never a free-text-only blob.
    notes: Union[dict[str, Any], list[Any]] = Field(default_factory=dict)

    provenance_completeness: ProvenanceCompleteness = ProvenanceCompleteness.INCOMPLETE
    missing_provenance: list[str] = Field(default_factory=list)

    @field_validator("evidence_id")
    @classmethod
    def _v_evidence_id(cls, v: str) -> str:
        return validate_evidence_id(v)

    @field_validator("evidence_type", mode="before")
    @classmethod
    def _v_evidence_type(cls, v: object) -> EvidenceType:
        return parse_evidence_type(v)  # type: ignore[arg-type]

    @field_validator("strategy_id")
    @classmethod
    def _v_strategy_id(cls, v: Optional[str]) -> Optional[str]:
        return _opt_strategy_id(v)

    @field_validator("strategy_version")
    @classmethod
    def _v_strategy_version(cls, v: Optional[str]) -> Optional[str]:
        return _opt_strategy_version(v)

    @field_validator("experiment_id")
    @classmethod
    def _v_experiment_id(cls, v: Optional[str]) -> Optional[str]:
        return _opt_experiment_id(v)

    @field_validator("run_id")
    @classmethod
    def _v_run_id(cls, v: Optional[str]) -> Optional[str]:
        return _opt_run_id(v)

    @field_validator("parent_evidence_ids")
    @classmethod
    def _v_parent_evidence_ids(cls, v: list[str]) -> list[str]:
        return [validate_evidence_id(x) for x in v]

    @field_validator("parent_run_ids")
    @classmethod
    def _v_parent_run_ids(cls, v: list[str]) -> list[str]:
        return [validate_run_id(x) for x in v]

    @field_validator("artifact_ids")
    @classmethod
    def _v_artifact_ids(cls, v: list[str]) -> list[str]:
        return [validate_artifact_id(x) for x in v]

    @field_validator("idempotency_key", "producer")
    @classmethod
    def _v_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be empty")
        return v

    @model_validator(mode="after")
    def _apply_provenance_completeness(self) -> "EvidenceRecord":
        completeness, missing = compute_provenance_completeness(self)
        self.provenance_completeness = completeness
        self.missing_provenance = missing
        return self


def compute_provenance_completeness(
    evidence: EvidenceRecord,
) -> tuple[ProvenanceCompleteness, list[str]]:
    """Compute provenance completeness for an evidence record.

    Returns ``(ProvenanceCompleteness.COMPLETE, [])`` if every field in
    :data:`REQUIRED_PROVENANCE_FIELDS` is present and non-empty, otherwise
    ``(ProvenanceCompleteness.INCOMPLETE, [<missing field names>])``.
    Never guesses or backfills a missing value — it only reports absence.
    """
    return _completeness_for(evidence, REQUIRED_PROVENANCE_FIELDS)


# --- ArtifactRecord -----------------------------------------------------------


class ArtifactRecord(CERModel):
    """A registered, hash-addressed artifact."""

    artifact_id: str
    sha256: str
    size_bytes: int = Field(ge=0)
    content_type: str
    filename: str
    uri: str
    registered_at: UTCDateTime
    run_id: Optional[str] = None
    evidence_id: Optional[str] = None

    @field_validator("artifact_id")
    @classmethod
    def _v_artifact_id(cls, v: str) -> str:
        return validate_artifact_id(v)

    @field_validator("sha256")
    @classmethod
    def _v_sha256(cls, v: str) -> str:
        if not isinstance(v, str) or len(v) != 64 or not all(c in "0123456789abcdef" for c in v.lower()):
            raise ValueError(f"sha256 must be a 64-character hex digest, got {v!r}")
        return v.lower()

    @field_validator("content_type", "filename", "uri")
    @classmethod
    def _v_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be empty")
        return v

    @field_validator("run_id")
    @classmethod
    def _v_run_id(cls, v: Optional[str]) -> Optional[str]:
        return _opt_run_id(v)

    @field_validator("evidence_id")
    @classmethod
    def _v_evidence_id(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return validate_evidence_id(v)


# --- PromotionTransition -------------------------------------------------------


def _new_transition_id() -> str:
    return f"trn_{uuid.uuid4().hex}"


class PromotionTransition(CERModel):
    """One recorded promotion-lifecycle transition.

    CER records transitions; it does not decide them. Every transition
    must preserve strategy/version, from/to state, UTC timestamp,
    authority/producer, supporting evidence references and reason — none
    of these are optional per the PID's promotion section.
    """

    transition_id: str = Field(default_factory=_new_transition_id)
    strategy_id: str
    strategy_version: str
    from_state: PromotionState
    to_state: PromotionState
    at_utc: UTCDateTime
    authority: str
    producer: str
    evidence_ids: list[str] = Field(min_length=1)
    reason: str

    @field_validator("strategy_id")
    @classmethod
    def _v_strategy_id(cls, v: str) -> str:
        return validate_strategy_id(v)

    @field_validator("strategy_version")
    @classmethod
    def _v_strategy_version(cls, v: str) -> str:
        return validate_strategy_version(v)

    @field_validator("evidence_ids")
    @classmethod
    def _v_evidence_ids(cls, v: list[str]) -> list[str]:
        return [validate_evidence_id(x) for x in v]

    @field_validator("authority", "producer", "reason")
    @classmethod
    def _v_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be empty")
        return v


# --- StrategyHealthRecord -------------------------------------------------------


def _new_health_id() -> str:
    return f"hlt_{uuid.uuid4().hex}"


class StrategyHealthRecord(CERModel):
    """One NEO surveillance observation for a strategy/version.

    CER records observations; NEO performs analysis and hypotheses. Every
    metric field is individually optional — not every producer has every
    metric — and absent metrics are recorded as absent (``None``), never
    defaulted to zero.
    """

    health_id: str = Field(default_factory=_new_health_id)
    strategy_id: str
    strategy_version: str
    health_state: HealthState
    observed_at_utc: UTCDateTime
    producer: str

    observed_trigger_rate: Optional[float] = None
    regime_conditioned_expected_trigger_rate: Optional[float] = None
    expectancy_r: Optional[float] = None
    win_rate: Optional[float] = None
    drawdown: Optional[float] = None
    mae: Optional[float] = None
    mfe: Optional[float] = None
    holding_time: Optional[float] = None
    execution_slippage_quality: Optional[float] = None
    regime_distribution: Optional[dict[str, float]] = None
    strategy_chain_strength_distribution: Optional[dict[str, float]] = None
    baseline_comparison: Optional[dict[str, Any]] = None
    confidence: Optional[float] = None

    #: Evidence-backed reason for this health observation; required, and
    #: must cite at least one supporting evidence record.
    reason: str
    evidence_ids: list[str] = Field(min_length=1)

    @field_validator("strategy_id")
    @classmethod
    def _v_strategy_id(cls, v: str) -> str:
        return validate_strategy_id(v)

    @field_validator("strategy_version")
    @classmethod
    def _v_strategy_version(cls, v: str) -> str:
        return validate_strategy_version(v)

    @field_validator("evidence_ids")
    @classmethod
    def _v_evidence_ids(cls, v: list[str]) -> list[str]:
        return [validate_evidence_id(x) for x in v]

    @field_validator("producer", "reason")
    @classmethod
    def _v_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be empty")
        return v
