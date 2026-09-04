"""Controlled vocabularies for CER: evidence types/groups, lifecycle states.

All enums here are ``str, Enum`` so they serialise as plain strings (JSON,
SQLite) while remaining validated Python values. Parsing an unrecognised
evidence-type string must fail loudly via
:func:`parse_evidence_type` / :class:`cer.contract.errors.UnknownEvidenceTypeError`
— never silently fall back to a default class.
"""

from __future__ import annotations

from enum import Enum

from .errors import UnknownEvidenceTypeError


class EvidenceType(str, Enum):
    """The 20 controlled evidence classes from the PID.

    Unknown ad-hoc evidence types must fail loudly unless introduced
    through a versioned contract change (i.e. added here).
    """

    # Research
    SOURCE_ANALYSIS = "SOURCE_ANALYSIS"
    STRATEGY_HYPOTHESIS = "STRATEGY_HYPOTHESIS"

    # Historical
    BACKTEST = "BACKTEST"
    OUT_OF_SAMPLE = "OUT_OF_SAMPLE"
    WALK_FORWARD = "WALK_FORWARD"
    MONTE_CARLO = "MONTE_CARLO"
    PARAMETER_SWEEP = "PARAMETER_SWEEP"
    ABLATION = "ABLATION"

    # Forward
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    MT5_DEMO = "MT5_DEMO"
    PLUTUS = "PLUTUS"

    # Live
    TINY_LIVE = "TINY_LIVE"
    PERSONAL_LIVE = "PERSONAL_LIVE"
    FUNDED_LIVE = "FUNDED_LIVE"

    # Learning / surveillance
    NEO_OBSERVATION = "NEO_OBSERVATION"
    NEO_HYPOTHESIS = "NEO_HYPOTHESIS"
    REGIME_ANALYSIS = "REGIME_ANALYSIS"
    DRIFT_ALERT = "DRIFT_ALERT"
    STRATEGY_HEALTH = "STRATEGY_HEALTH"


class EvidenceGroup(str, Enum):
    """The PID's evidence-type headings, used to group EvidenceType values."""

    RESEARCH = "RESEARCH"
    HISTORICAL = "HISTORICAL"
    FORWARD = "FORWARD"
    LIVE = "LIVE"
    LEARNING = "LEARNING"


#: Mapping from each EvidenceType to its EvidenceGroup, per the PID's own
#: headings (Research / Historical / Forward / Live / Learning-surveillance).
EVIDENCE_TYPE_GROUP: dict[EvidenceType, EvidenceGroup] = {
    EvidenceType.SOURCE_ANALYSIS: EvidenceGroup.RESEARCH,
    EvidenceType.STRATEGY_HYPOTHESIS: EvidenceGroup.RESEARCH,
    EvidenceType.BACKTEST: EvidenceGroup.HISTORICAL,
    EvidenceType.OUT_OF_SAMPLE: EvidenceGroup.HISTORICAL,
    EvidenceType.WALK_FORWARD: EvidenceGroup.HISTORICAL,
    EvidenceType.MONTE_CARLO: EvidenceGroup.HISTORICAL,
    EvidenceType.PARAMETER_SWEEP: EvidenceGroup.HISTORICAL,
    EvidenceType.ABLATION: EvidenceGroup.HISTORICAL,
    EvidenceType.SHADOW: EvidenceGroup.FORWARD,
    EvidenceType.PAPER: EvidenceGroup.FORWARD,
    EvidenceType.MT5_DEMO: EvidenceGroup.FORWARD,
    EvidenceType.PLUTUS: EvidenceGroup.FORWARD,
    EvidenceType.TINY_LIVE: EvidenceGroup.LIVE,
    EvidenceType.PERSONAL_LIVE: EvidenceGroup.LIVE,
    EvidenceType.FUNDED_LIVE: EvidenceGroup.LIVE,
    EvidenceType.NEO_OBSERVATION: EvidenceGroup.LEARNING,
    EvidenceType.NEO_HYPOTHESIS: EvidenceGroup.LEARNING,
    EvidenceType.REGIME_ANALYSIS: EvidenceGroup.LEARNING,
    EvidenceType.DRIFT_ALERT: EvidenceGroup.LEARNING,
    EvidenceType.STRATEGY_HEALTH: EvidenceGroup.LEARNING,
}


def parse_evidence_type(value: str) -> EvidenceType:
    """Parse a string into an :class:`EvidenceType`, failing loudly.

    Raises :class:`~cer.contract.errors.UnknownEvidenceTypeError` for any
    value that is not one of the 20 controlled classes. Never coerces or
    falls back to a default.
    """
    if isinstance(value, EvidenceType):
        return value
    if not isinstance(value, str):
        raise UnknownEvidenceTypeError(
            f"evidence_type must be a string, got {type(value).__name__} ({value!r})"
        )
    try:
        return EvidenceType(value)
    except ValueError as exc:
        raise UnknownEvidenceTypeError(
            f"unknown evidence_type {value!r}; must be one of "
            f"{sorted(t.value for t in EvidenceType)!r}"
        ) from exc


def evidence_group_of(evidence_type: EvidenceType) -> EvidenceGroup:
    """Return the EvidenceGroup for a given EvidenceType."""
    return EVIDENCE_TYPE_GROUP[EvidenceType(evidence_type)]


class PromotionState(str, Enum):
    """Canonical strategy promotion lifecycle states."""

    DRAFT = "DRAFT"
    IMPLEMENTED = "IMPLEMENTED"
    BACKTESTED = "BACKTESTED"
    VALIDATED = "VALIDATED"
    FORWARD_TEST = "FORWARD_TEST"
    LIVE_CANDIDATE = "LIVE_CANDIDATE"
    TINY_LIVE = "TINY_LIVE"
    APPROVED = "APPROVED"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"


class HealthState(str, Enum):
    """Canonical strategy health/surveillance states."""

    HEALTHY = "HEALTHY"
    DORMANT = "DORMANT"
    WATCH = "WATCH"
    DEGRADED = "DEGRADED"
    SUSPENDED = "SUSPENDED"


class RunStatus(str, Enum):
    """Status of a single experiment run."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class ProvenanceCompleteness(str, Enum):
    """Whether a record's required provenance is fully present."""

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
