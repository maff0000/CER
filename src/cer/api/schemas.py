"""Request/response bodies for the CER HTTP API.

These are thin, HTTP-shaped wrappers around ``cer.contract.models`` — they
exist only where the wire shape genuinely differs from the contract model
(server-generated identity fields omitted, timestamps defaulted, an
idempotency key accepted out-of-band). Where a contract model can be used
as-is for a response, routes return it directly (FastAPI serialises any
pydantic model).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from cer.contract.enums import HealthState, PromotionState, RunStatus
from cer.contract.models import MetricValue

# --- Shared -------------------------------------------------------------


class APIModel(BaseModel):
    """Base for API request bodies: unknown fields fail loudly, like the
    contract models they front."""

    model_config = ConfigDict(extra="forbid")


class ErrorBody(BaseModel):
    """Structured error response body. Every CER error response has this
    shape: a stable machine-readable ``code``, a human-readable
    ``message``, and the ``request_id`` of the request that failed."""

    code: str
    message: str
    request_id: str


class VersionInfo(BaseModel):
    """Discovery payload so producers/consumers can check compatibility
    before talking to this server."""

    contract_version: str
    schema_version: int
    supported_schema_versions: list[int]


class LivenessBody(BaseModel):
    status: str = "ok"


class ReadinessBody(BaseModel):
    status: str
    failed_dependency: Optional[str] = None
    detail: Optional[str] = None


# --- Strategy / StrategyVersion ------------------------------------------


class StrategyCreateRequest(APIModel):
    strategy_id: str
    name: str
    thesis: str
    created_at: Optional[datetime] = None


class StrategyVersionCreateRequest(APIModel):
    strategy_version: str
    git_repo: str
    git_commit: str
    created_at: Optional[datetime] = None
    notes: Optional[str] = None


# --- Experiment / Run -------------------------------------------------------


class ExperimentCreateRequest(APIModel):
    objective: str
    producer: str
    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    created_at: Optional[datetime] = None


class RunCreateRequest(APIModel):
    producer: str
    status: RunStatus = RunStatus.OPEN
    started_at: Optional[datetime] = None
    producer_version: Optional[str] = None
    git_repo: Optional[str] = None
    git_commit: Optional[str] = None
    dataset_id: Optional[str] = None
    dataset_version: Optional[str] = None
    dataset_ref: Optional[str] = None
    config_hash: Optional[str] = None
    config_ref: Optional[str] = None
    environment: Optional[str] = None
    #: Optional body-level idempotency key; the ``Idempotency-Key`` header
    #: is accepted too (and takes precedence if both are given and equal;
    #: a mismatch between the two fails loudly — see routes.py).
    idempotency_key: Optional[str] = None


class RunCloseRequest(APIModel):
    status: RunStatus = RunStatus.CLOSED
    ended_at: Optional[datetime] = None


# --- Evidence ---------------------------------------------------------------


class EvidenceCreateRequest(APIModel):
    evidence_type: str
    schema_version: int
    producer: str

    idempotency_key: Optional[str] = None

    strategy_id: Optional[str] = None
    strategy_version: Optional[str] = None
    experiment_id: Optional[str] = None
    run_id: Optional[str] = None

    producer_version: Optional[str] = None

    created_at_utc: Optional[datetime] = None
    observed_at_utc: Optional[datetime] = None

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

    notes: Union[dict[str, Any], list[Any]] = Field(default_factory=dict)


# --- Artifacts ----------------------------------------------------------


class ArtifactAttachRequest(APIModel):
    evidence_id: Optional[str] = None
    run_id: Optional[str] = None


# --- Promotion / Health ------------------------------------------------


class PromotionCreateRequest(APIModel):
    strategy_id: str
    strategy_version: str
    from_state: PromotionState
    to_state: PromotionState
    at_utc: Optional[datetime] = None
    authority: str
    producer: str
    evidence_ids: list[str]
    reason: str


class HealthRecordCreateRequest(APIModel):
    strategy_id: str
    strategy_version: str
    health_state: HealthState
    observed_at_utc: Optional[datetime] = None
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

    reason: str
    evidence_ids: list[str]
