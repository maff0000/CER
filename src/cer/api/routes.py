"""CER API route handlers.

Each handler is a thin translation layer: parse/validate the HTTP request,
build (or look up) a ``cer.contract.models`` object, call exactly one
``MetadataStore``/``ArtifactStore`` method, and serialise the result. No
business logic lives here beyond identity generation and the idempotency
plumbing described below — the stores own everything else.

Identity generation
--------------------
``experiment_id`` is always freshly generated (``create_experiment`` is not
documented as idempotent in ``cer.contract.stores``). ``run_id`` and
``evidence_id`` are generated *deterministically* from the caller's
idempotency key when one is given (see :func:`_generate_id`): this is what
makes "replay the same key with the same body" actually produce a
byte-identical record for the store to recognise as the same submission,
rather than a fresh id every time defeating idempotency at the API layer
before the store ever sees it. Without an idempotency key, ids are random
per the normal case.

Idempotency keys are scoped to the producer, not global
---------------------------------------------------------
An idempotency key is only unique *within* the producer that supplied it:
the deterministic derivation is ``uuid5(namespace, f"{kind}:{producer}:{key}")``,
not ``f"{kind}:{key}"``. Two different producers may safely use the exact
same key string (e.g. both happen to key on a date) without colliding —
each producer's own retry of its own key is idempotent, but one producer's
key can never collide with, or be shadowed by, another's. This is required
by the PID's multi-producer proof ("distinct valid records ... without
ambiguity") and the general idempotency/immutability rule that duplicate
submissions must be detectable *without* letting one producer's key choice
deny another's. Consequently, supplying an idempotency key without a
non-empty producer is itself a contract violation (400) — the producer is
not optional context, it is part of the key's identity.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import Response as RawResponse

from cer.contract.enums import EvidenceType
from cer.contract.errors import ContractViolationError
from cer.contract.models import (
    ArtifactRecord,
    Experiment,
    EvidenceRecord,
    PromotionTransition,
    Run,
    Strategy,
    StrategyHealthRecord,
    StrategyVersion,
)
from cer.contract.stores import ArtifactStore, MetadataStore
from cer.contract.version import (
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    validate_contract_version,
    validate_schema_version,
)
from cer.runtime.clock import utcnow

from . import schemas as sch

CONTRACT_VERSION_HEADER = "X-CER-Contract-Version"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

#: Fixed namespace for deterministic id derivation from an idempotency key.
#: Any stable UUID works here; it only has to be constant across process
#: restarts so a replay after a restart still derives the same id.
_DETERMINISTIC_ID_NAMESPACE = uuid.UUID("b7e285c6-6b58-4b1a-9e2b-9d6a9c7a2f10")


def _generate_id(
    prefix: str, kind: str, idempotency_key: Optional[str], producer: Optional[str] = None
) -> str:
    """Generate an id, deterministically from ``(kind, producer,
    idempotency_key)`` when a key is given, else randomly.

    An idempotency key is scoped to its producer (see the module
    docstring) — a key supplied without a non-empty ``producer`` is a
    contract violation, never silently treated as an unscoped/global key.
    """
    if idempotency_key:
        if not producer or not producer.strip():
            raise ContractViolationError(
                "an idempotency key requires a non-empty producer to scope it "
                "(idempotency keys are scoped per-producer, not global)"
            )
        digest = uuid.uuid5(
            _DETERMINISTIC_ID_NAMESPACE, f"{kind}:{producer}:{idempotency_key}"
        ).hex
    else:
        digest = uuid.uuid4().hex
    return f"{prefix}{digest}"


def _resolve_idempotency_key(
    header_value: Optional[str], body_value: Optional[str]
) -> Optional[str]:
    """Reconcile a header-supplied and body-supplied idempotency key.

    A caller may supply either, or both (if both, they must agree — a
    mismatch is a contract violation, not a silent pick-one).
    """
    if header_value and body_value and header_value != body_value:
        raise ContractViolationError(
            "Idempotency-Key header and body idempotency_key disagree"
        )
    return header_value or body_value


# --- Dependencies -------------------------------------------------------


def get_metadata_store(request: Request) -> MetadataStore:
    return request.app.state.metadata_store


def get_artifact_store(request: Request) -> ArtifactStore:
    return request.app.state.artifact_store


def require_contract_version(
    x_cer_contract_version: Optional[str] = Header(default=None, alias=CONTRACT_VERSION_HEADER),
) -> str:
    """Fail loudly (400, via ContractViolationError) if the caller's
    declared contract version is missing or incompatible."""
    if x_cer_contract_version is None:
        raise ContractViolationError(
            f"missing required header {CONTRACT_VERSION_HEADER}"
        )
    return validate_contract_version(x_cer_contract_version)


# --- Health / readiness / discovery --------------------------------------

health_router = APIRouter(tags=["health"])


@health_router.get("/health", response_model=sch.LivenessBody)
def liveness() -> sch.LivenessBody:
    """Liveness: the process is up. No dependency calls, no I/O."""
    return sch.LivenessBody()


@health_router.get("/ready")
def readiness(
    response: Response,
    metadata_store: MetadataStore = Depends(get_metadata_store),
    artifact_store: ArtifactStore = Depends(get_artifact_store),
) -> sch.ReadinessBody:
    """Readiness: actually calls both stores' health(). 200 only if both
    pass; 503 naming the failed dependency if either raises. Never cached."""
    try:
        metadata_store.health()
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure means not ready
        response.status_code = 503
        return sch.ReadinessBody(status="unavailable", failed_dependency="metadata_store", detail=str(exc))
    try:
        artifact_store.health()
    except Exception as exc:  # noqa: BLE001
        response.status_code = 503
        return sch.ReadinessBody(status="unavailable", failed_dependency="artifact_store", detail=str(exc))
    return sch.ReadinessBody(status="ok")


version_router = APIRouter(tags=["discovery"])


@version_router.get("/version", response_model=sch.VersionInfo)
def get_version() -> sch.VersionInfo:
    """Discovery endpoint: the server's own contract/schema version, so a
    producer can check compatibility before sending anything else."""
    return sch.VersionInfo(
        contract_version=CONTRACT_VERSION,
        schema_version=SCHEMA_VERSION,
        supported_schema_versions=sorted(SUPPORTED_SCHEMA_VERSIONS),
    )


# --- Everything else: requires a compatible X-CER-Contract-Version ------

api_router = APIRouter(dependencies=[Depends(require_contract_version)])


# --- Strategy / StrategyVersion ------------------------------------------


@api_router.post("/strategies", response_model=Strategy, status_code=201)
def create_strategy(
    body: sch.StrategyCreateRequest,
    metadata_store: MetadataStore = Depends(get_metadata_store),
) -> Strategy:
    strategy = Strategy(
        strategy_id=body.strategy_id,
        name=body.name,
        thesis=body.thesis,
        created_at=body.created_at or utcnow(),
    )
    return metadata_store.register_strategy(strategy)


@api_router.post("/strategies/{strategy_id}/versions", response_model=StrategyVersion, status_code=201)
def create_strategy_version(
    strategy_id: str,
    body: sch.StrategyVersionCreateRequest,
    metadata_store: MetadataStore = Depends(get_metadata_store),
) -> StrategyVersion:
    sv = StrategyVersion(
        strategy_id=strategy_id,
        strategy_version=body.strategy_version,
        git_repo=body.git_repo,
        git_commit=body.git_commit,
        created_at=body.created_at or utcnow(),
        notes=body.notes,
    )
    return metadata_store.register_strategy_version(sv)


# --- Experiment / Run -------------------------------------------------------


@api_router.post("/experiments", response_model=Experiment, status_code=201)
def create_experiment(
    body: sch.ExperimentCreateRequest,
    metadata_store: MetadataStore = Depends(get_metadata_store),
) -> Experiment:
    experiment = Experiment(
        experiment_id=_generate_id("exp_", "experiment", None),
        objective=body.objective,
        strategy_id=body.strategy_id,
        strategy_version=body.strategy_version,
        producer=body.producer,
        created_at=body.created_at or utcnow(),
    )
    return metadata_store.create_experiment(experiment)


@api_router.post("/experiments/{experiment_id}/runs", response_model=Run, status_code=201)
def create_run(
    experiment_id: str,
    body: sch.RunCreateRequest,
    metadata_store: MetadataStore = Depends(get_metadata_store),
    idempotency_key_header: Optional[str] = Header(default=None, alias=IDEMPOTENCY_KEY_HEADER),
) -> Run:
    idem_key = _resolve_idempotency_key(idempotency_key_header, body.idempotency_key)
    run = Run(
        run_id=_generate_id("run_", "run", idem_key, body.producer),
        experiment_id=experiment_id,
        status=body.status,
        started_at=body.started_at or utcnow(),
        producer=body.producer,
        producer_version=body.producer_version,
        git_repo=body.git_repo,
        git_commit=body.git_commit,
        dataset_id=body.dataset_id,
        dataset_version=body.dataset_version,
        dataset_ref=body.dataset_ref,
        config_hash=body.config_hash,
        config_ref=body.config_ref,
        environment=body.environment,
    )
    return metadata_store.create_run(run, idempotency_key=idem_key)


@api_router.post("/runs/{run_id}/close", response_model=Run)
def close_run(
    run_id: str,
    body: sch.RunCloseRequest,
    metadata_store: MetadataStore = Depends(get_metadata_store),
) -> Run:
    return metadata_store.close_run(run_id, status=body.status, ended_at=body.ended_at or utcnow())


# --- Evidence ----------------------------------------------------------


@api_router.post("/evidence", response_model=EvidenceRecord, status_code=201)
def append_evidence(
    body: sch.EvidenceCreateRequest,
    metadata_store: MetadataStore = Depends(get_metadata_store),
    idempotency_key_header: Optional[str] = Header(default=None, alias=IDEMPOTENCY_KEY_HEADER),
) -> EvidenceRecord:
    idem_key = _resolve_idempotency_key(idempotency_key_header, body.idempotency_key)
    if not idem_key:
        raise ContractViolationError(
            "an idempotency key is required: send the Idempotency-Key header "
            "or the idempotency_key body field"
        )
    # Fail loudly on an incompatible schema_version before touching the
    # store — a PID contract-gate acceptance criterion.
    validate_schema_version(body.schema_version)

    evidence = EvidenceRecord(
        evidence_id=_generate_id("ev_", "evidence", idem_key, body.producer),
        idempotency_key=idem_key,
        evidence_type=body.evidence_type,  # validated by EvidenceRecord itself
        schema_version=body.schema_version,
        strategy_id=body.strategy_id,
        strategy_version=body.strategy_version,
        experiment_id=body.experiment_id,
        run_id=body.run_id,
        producer=body.producer,
        producer_version=body.producer_version,
        created_at_utc=body.created_at_utc or utcnow(),
        observed_at_utc=body.observed_at_utc,
        git_repo=body.git_repo,
        git_commit=body.git_commit,
        dataset_id=body.dataset_id,
        dataset_version=body.dataset_version,
        dataset_ref=body.dataset_ref,
        config_hash=body.config_hash,
        config_ref=body.config_ref,
        environment=body.environment,
        instruments=body.instruments,
        timeframes=body.timeframes,
        metrics=body.metrics,
        verdict=body.verdict,
        status=body.status,
        regime_tags=body.regime_tags,
        parent_evidence_ids=body.parent_evidence_ids,
        parent_run_ids=body.parent_run_ids,
        artifact_ids=body.artifact_ids,
        notes=body.notes,
    )
    return metadata_store.append_evidence(evidence)


@api_router.get("/evidence/{evidence_id}", response_model=EvidenceRecord)
def get_evidence(
    evidence_id: str,
    metadata_store: MetadataStore = Depends(get_metadata_store),
) -> EvidenceRecord:
    return metadata_store.get_evidence(evidence_id)


@api_router.get("/evidence", response_model=list[EvidenceRecord])
def query_evidence(
    metadata_store: MetadataStore = Depends(get_metadata_store),
    strategy_id: Optional[str] = Query(default=None),
    strategy_version: Optional[str] = Query(default=None),
    run_id: Optional[str] = Query(default=None),
    experiment_id: Optional[str] = Query(default=None),
    evidence_type: Optional[EvidenceType] = Query(default=None),
    since: Optional[datetime] = Query(default=None),
    until: Optional[datetime] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[EvidenceRecord]:
    return metadata_store.query_evidence(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        run_id=run_id,
        experiment_id=experiment_id,
        evidence_type=evidence_type,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


# --- Artifacts ------------------------------------------------------------

#: Header names for the raw-bytes artifact upload (see create_artifact).
ARTIFACT_FILENAME_HEADER = "X-CER-Filename"
ARTIFACT_SHA256_HEADER = "X-CER-Declared-Sha256"
ARTIFACT_RUN_ID_HEADER = "X-CER-Run-Id"
ARTIFACT_EVIDENCE_ID_HEADER = "X-CER-Evidence-Id"
_DEFAULT_ARTIFACT_CONTENT_TYPE = "application/octet-stream"


@api_router.post("/artifacts", response_model=ArtifactRecord, status_code=201)
async def create_artifact(
    request: Request,
    metadata_store: MetadataStore = Depends(get_metadata_store),
    artifact_store: ArtifactStore = Depends(get_artifact_store),
    filename: Optional[str] = Header(default=None, alias=ARTIFACT_FILENAME_HEADER),
    declared_sha256: Optional[str] = Header(default=None, alias=ARTIFACT_SHA256_HEADER),
    run_id: Optional[str] = Header(default=None, alias=ARTIFACT_RUN_ID_HEADER),
    evidence_id: Optional[str] = Header(default=None, alias=ARTIFACT_EVIDENCE_ID_HEADER),
) -> ArtifactRecord:
    """Register an artifact.

    Accepts the artifact's raw bytes as the request body (not a multipart
    upload — see the Engineer report for why: it keeps this endpoint
    dependency-free). Metadata travels as headers: ``X-CER-Filename``
    (required), ``Content-Type`` (optional, defaults to
    ``application/octet-stream``), ``X-CER-Declared-Sha256`` (optional
    integrity check), ``X-CER-Run-Id``/``X-CER-Evidence-Id`` (optional
    immediate attachment).
    """
    if not filename:
        raise ContractViolationError(f"missing required header {ARTIFACT_FILENAME_HEADER}")

    data = await request.body()
    settings = request.app.state.settings
    if len(data) > settings.max_artifact_bytes:
        raise ContractViolationError(
            f"artifact body of {len(data)} bytes exceeds max_artifact_bytes "
            f"({settings.max_artifact_bytes})"
        )

    content_type = request.headers.get("content-type") or _DEFAULT_ARTIFACT_CONTENT_TYPE

    record = artifact_store.put(
        data,
        content_type=content_type,
        filename=filename,
        declared_sha256=declared_sha256,
    )
    if run_id is not None or evidence_id is not None:
        record = record.model_copy(update={"run_id": run_id, "evidence_id": evidence_id})
    return metadata_store.register_artifact(record)


@api_router.get("/artifacts/{artifact_id}", response_model=ArtifactRecord)
def get_artifact_metadata(
    artifact_id: str,
    artifact_store: ArtifactStore = Depends(get_artifact_store),
) -> ArtifactRecord:
    return artifact_store.stat(artifact_id)


@api_router.get("/artifacts/{artifact_id}/download")
def download_artifact(
    artifact_id: str,
    artifact_store: ArtifactStore = Depends(get_artifact_store),
) -> RawResponse:
    record = artifact_store.stat(artifact_id)
    data = artifact_store.get(artifact_id)
    return RawResponse(
        content=data,
        media_type=record.content_type,
        headers={"Content-Disposition": f'attachment; filename="{record.filename}"'},
    )


@api_router.get("/artifacts", response_model=list[ArtifactRecord])
def query_artifacts(
    metadata_store: MetadataStore = Depends(get_metadata_store),
    evidence_id: Optional[str] = Query(default=None),
    run_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[ArtifactRecord]:
    return metadata_store.query_artifacts(
        evidence_id=evidence_id, run_id=run_id, limit=limit, offset=offset
    )


@api_router.post("/artifacts/{artifact_id}/attach", response_model=ArtifactRecord)
def attach_artifact(
    artifact_id: str,
    body: sch.ArtifactAttachRequest,
    metadata_store: MetadataStore = Depends(get_metadata_store),
) -> ArtifactRecord:
    return metadata_store.attach_artifact(
        artifact_id, evidence_id=body.evidence_id, run_id=body.run_id
    )


# --- Promotion history ----------------------------------------------------


@api_router.post("/promotions", response_model=PromotionTransition, status_code=201)
def create_promotion(
    body: sch.PromotionCreateRequest,
    metadata_store: MetadataStore = Depends(get_metadata_store),
) -> PromotionTransition:
    transition = PromotionTransition(
        strategy_id=body.strategy_id,
        strategy_version=body.strategy_version,
        from_state=body.from_state,
        to_state=body.to_state,
        at_utc=body.at_utc or utcnow(),
        authority=body.authority,
        producer=body.producer,
        evidence_ids=body.evidence_ids,
        reason=body.reason,
    )
    return metadata_store.record_promotion(transition)


@api_router.get("/promotions", response_model=list[PromotionTransition])
def query_promotions(
    metadata_store: MetadataStore = Depends(get_metadata_store),
    strategy_id: Optional[str] = Query(default=None),
    strategy_version: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[PromotionTransition]:
    return metadata_store.query_promotions(
        strategy_id=strategy_id, strategy_version=strategy_version, limit=limit, offset=offset
    )


# --- Strategy health history ------------------------------------------------


@api_router.post("/health-records", response_model=StrategyHealthRecord, status_code=201)
def create_health_record(
    body: sch.HealthRecordCreateRequest,
    metadata_store: MetadataStore = Depends(get_metadata_store),
) -> StrategyHealthRecord:
    record = StrategyHealthRecord(
        strategy_id=body.strategy_id,
        strategy_version=body.strategy_version,
        health_state=body.health_state,
        observed_at_utc=body.observed_at_utc or utcnow(),
        producer=body.producer,
        observed_trigger_rate=body.observed_trigger_rate,
        regime_conditioned_expected_trigger_rate=body.regime_conditioned_expected_trigger_rate,
        expectancy_r=body.expectancy_r,
        win_rate=body.win_rate,
        drawdown=body.drawdown,
        mae=body.mae,
        mfe=body.mfe,
        holding_time=body.holding_time,
        execution_slippage_quality=body.execution_slippage_quality,
        regime_distribution=body.regime_distribution,
        strategy_chain_strength_distribution=body.strategy_chain_strength_distribution,
        baseline_comparison=body.baseline_comparison,
        confidence=body.confidence,
        reason=body.reason,
        evidence_ids=body.evidence_ids,
    )
    return metadata_store.record_health(record)


@api_router.get("/health-records", response_model=list[StrategyHealthRecord])
def query_health_records(
    metadata_store: MetadataStore = Depends(get_metadata_store),
    strategy_id: Optional[str] = Query(default=None),
    strategy_version: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[StrategyHealthRecord]:
    return metadata_store.query_health(
        strategy_id=strategy_id, strategy_version=strategy_version, limit=limit, offset=offset
    )


def build_router() -> APIRouter:
    """Combine the version-discovery router (no contract-version gate —
    a producer must be able to ask this before it knows what to send) and
    the main API router (contract-version gated) into one, for a single
    ``app.include_router(build_router(), prefix="/v1")`` call."""
    combined = APIRouter()
    combined.include_router(version_router)
    combined.include_router(api_router)
    return combined
