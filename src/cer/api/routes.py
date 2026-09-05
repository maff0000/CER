"""CER API route handlers.

Each handler is a thin translation layer: parse/validate the HTTP request,
build (or look up) a ``cer.contract.models`` object, call exactly one
``MetadataStore``/``ArtifactStore`` method, and serialise the result. No
business logic lives here beyond identity generation and the idempotency
plumbing described below — the stores own everything else.

Identity generation
--------------------
``run_id``, ``evidence_id``, ``experiment_id``, ``transition_id`` and
``health_id`` are generated *deterministically* from the caller's
idempotency key when one is given (see :func:`_generate_id`). Without an
idempotency key, ids are random per the normal case.

What this buys is a *stable id across retries*: the same producer
replaying the same key derives the same id rather than a fresh one each
time, so logs, traces and any id a caller recorded from an earlier attempt
all line up.

It is deliberately not what makes replay detection work. The store keys
replays on ``(producer, idempotency_key)`` and excludes the generated id
from the content fingerprint it compares, so a replay is recognised as the
same submission regardless of how its id was derived — which is why
``POST /v1/artifacts``, whose identity is minted by the artifact store
rather than derived here, is idempotent on exactly the same terms (see
below).

Which endpoints take an idempotency key
-----------------------------------------
``POST /v1/evidence`` *requires* one. ``POST /v1/experiments/{id}/runs``,
``POST /v1/experiments``, ``POST /v1/promotions``,
``POST /v1/health-records`` and ``POST /v1/artifacts`` accept one
*optionally* — as the ``Idempotency-Key`` header or the body's
``idempotency_key`` field — and honour it identically: producer-scoped,
replay returns the stored record, materially different body under the
same producer and key is a 409. Omitting it keeps the plain-create
behaviour (fresh id, new record). The PID's clause — "ingestion must
tolerate safe retries; duplicate submissions must be detectable via
idempotency key or equivalent" — is not scoped to evidence and runs, and
accepting a header that silently does nothing is worse than rejecting
it: a retrying producer would record one intended promotion transition
several times and corrupt the promotion history that is itself a
first-class PID deliverable.

``POST /v1/artifacts`` is the newest of those and the one exception to
"deterministic id derivation": its identity is minted by the artifact
store at ``put()`` time, so the replay lookup is by ``(producer,
idempotency_key)`` against the metadata store instead — see
:func:`create_artifact`. Content-addressing deduplicates the artifact's
*bytes*, never its *registration*: without a key, two retries of one
logical registration leave two indistinguishable records pointing at the
same blob, which is exactly the "duplicate submissions must be
detectable" failure the clause forbids.

Idempotency keys are scoped to the producer, not global
---------------------------------------------------------
For ``/v1/promotions`` and ``/v1/health-records`` the producer used for
scoping is the one in the request body — the same value stored on the
record itself — consistent with the other endpoints.

``/v1/artifacts`` has no producer to take: its body is the artifact's raw
bytes and ``ArtifactRecord`` has no producer field. A producer that wants
idempotency there supplies one in the ``X-CER-Producer`` header, which is
used *only* to scope the key (it is stored on the metadata row, not on
the artifact record). A request with no ``Idempotency-Key`` needs no
producer and behaves exactly as before.

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

import hashlib
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import Response as RawResponse

from cer.contract.enums import EvidenceType
from cer.contract.errors import ChecksumMismatchError, ContractViolationError
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


def _require_producer_for_key(producer: Optional[str]) -> str:
    """Return ``producer``, or reject an unscoped idempotency key (400).

    An idempotency key is scoped to its producer (see the module
    docstring) — a key supplied without a non-empty ``producer`` is a
    contract violation, never silently treated as an unscoped/global key.
    One helper, so every endpoint that takes a key enforces the identical
    rule with the identical message.
    """
    if not producer or not producer.strip():
        raise ContractViolationError(
            "an idempotency key requires a non-empty producer to scope it "
            "(idempotency keys are scoped per-producer, not global)"
        )
    return producer


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
        _require_producer_for_key(producer)
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
    idempotency_key_header: Optional[str] = Header(default=None, alias=IDEMPOTENCY_KEY_HEADER),
) -> Experiment:
    idem_key = _resolve_idempotency_key(idempotency_key_header, body.idempotency_key)
    experiment = Experiment(
        experiment_id=_generate_id("exp_", "experiment", idem_key, body.producer),
        objective=body.objective,
        strategy_id=body.strategy_id,
        strategy_version=body.strategy_version,
        producer=body.producer,
        created_at=body.created_at or utcnow(),
    )
    return metadata_store.create_experiment(experiment, idempotency_key=idem_key)


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
#: Scopes an Idempotency-Key on this endpoint only. Not stored on the
#: ArtifactRecord (which has no producer field) — see create_artifact.
ARTIFACT_PRODUCER_HEADER = "X-CER-Producer"
#: Optional, explicit content-type declaration. Takes precedence over the
#: transport ``Content-Type`` header — see _resolve_artifact_content_type.
ARTIFACT_CONTENT_TYPE_HEADER = "X-CER-Content-Type"
_DEFAULT_ARTIFACT_CONTENT_TYPE = "application/octet-stream"
#: Transport-framing content types an HTTP client sets automatically
#: (curl's -d, requests' default for dict/tuple ``data=``) that describe
#: how the *request* was encoded, never what the artifact bytes *are*.
#: This endpoint takes the raw body verbatim and never parses form
#: encoding, so neither value can ever be a truthful declaration of an
#: artifact's content type -- see _resolve_artifact_content_type.
_FORM_ENCODED_MEDIA_TYPES = frozenset(
    {"application/x-www-form-urlencoded", "multipart/form-data"}
)


def _is_form_encoded_media_type(content_type: str) -> bool:
    """True if ``content_type``'s media type (ignoring params/case/whitespace)
    is one of the transport form-encoding defaults."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type in _FORM_ENCODED_MEDIA_TYPES


def _resolve_artifact_content_type(
    transport_content_type: Optional[str], declared_content_type: Optional[str]
) -> str:
    """Return the content type to record for a new artifact.

    The transport ``Content-Type`` header is populated automatically by
    every HTTP client, often to a form-encoding default
    (``application/x-www-form-urlencoded``, ``multipart/form-data``,
    with or without parameters) that describes how the *request* was
    framed, not what the artifact *is*. ``POST /v1/artifacts`` never
    parses form encoding -- it takes the raw body verbatim as the
    artifact's bytes -- so a form-encoding value can never be a truthful
    declaration of an artifact's content type. Recording it anyway would
    burn a client default into a permanent, immutable evidence record
    (PID Artifact doctrine: CER must "preserve content/type metadata" --
    a client default is not preserved metadata, it's wrong metadata that
    looks authoritative and, once written, cannot be corrected).

    ``X-CER-Content-Type`` (declared_content_type) is an optional,
    explicit declaration channel for a producer whose HTTP client
    controls the transport header and won't let it be set to the real
    type. When present -- and not itself a form-encoding default -- it
    takes precedence over the transport header.

    Any other, non-form-encoded value from either source is a genuine
    declaration and is returned exactly as given: no stripping,
    normalising, or case-folding of the value that gets recorded --
    only the classification check above is case/whitespace/parameter
    insensitive. Absent, empty, or form-encoded input from both sources
    falls back to the existing honest default for "the producer did not
    tell us".
    """
    for candidate in (declared_content_type, transport_content_type):
        if not candidate:
            continue
        if not candidate.strip():
            continue
        if _is_form_encoded_media_type(candidate):
            continue
        return candidate
    return _DEFAULT_ARTIFACT_CONTENT_TYPE


@api_router.post("/artifacts", response_model=ArtifactRecord, status_code=201)
async def create_artifact(
    request: Request,
    metadata_store: MetadataStore = Depends(get_metadata_store),
    artifact_store: ArtifactStore = Depends(get_artifact_store),
    filename: Optional[str] = Header(default=None, alias=ARTIFACT_FILENAME_HEADER),
    declared_sha256: Optional[str] = Header(default=None, alias=ARTIFACT_SHA256_HEADER),
    run_id: Optional[str] = Header(default=None, alias=ARTIFACT_RUN_ID_HEADER),
    evidence_id: Optional[str] = Header(default=None, alias=ARTIFACT_EVIDENCE_ID_HEADER),
    producer: Optional[str] = Header(default=None, alias=ARTIFACT_PRODUCER_HEADER),
    idempotency_key_header: Optional[str] = Header(default=None, alias=IDEMPOTENCY_KEY_HEADER),
    content_type_declared: Optional[str] = Header(
        default=None, alias=ARTIFACT_CONTENT_TYPE_HEADER
    ),
) -> ArtifactRecord:
    """Register an artifact.

    Accepts the artifact's raw bytes as the request body rather than a
    multipart upload: multipart would pull in an extra parsing dependency
    (``python-multipart``) for no gain, since exactly one file is ever
    registered per call. Metadata travels as headers: ``X-CER-Filename``
    (required), ``Content-Type`` (optional, defaults to
    ``application/octet-stream`` — see below), ``X-CER-Content-Type``
    (optional, explicit override — see below), ``X-CER-Declared-Sha256``
    (optional integrity check), ``X-CER-Run-Id``/``X-CER-Evidence-Id``
    (optional immediate attachment), and ``Idempotency-Key`` +
    ``X-CER-Producer`` (optional retry-safety, see below).

    Content type
    ------------
    The transport ``Content-Type`` header is populated automatically by
    every HTTP client — often to a form-encoding default
    (``application/x-www-form-urlencoded``, ``multipart/form-data``) that
    describes how the request was framed, not what the artifact is. This
    endpoint never parses form encoding, so such a value is treated as
    "not declared" and recorded as ``application/octet-stream`` instead
    of verbatim. ``X-CER-Content-Type`` lets a producer declare the real
    type explicitly when its client won't let it set the transport header
    directly; it takes precedence when present. See
    :func:`_resolve_artifact_content_type` for the exact rule.

    Idempotency
    ------------
    ``Idempotency-Key`` is optional here, as it is on ``/v1/experiments``,
    ``/v1/promotions`` and ``/v1/health-records``. When supplied it must
    be accompanied by ``X-CER-Producer`` — a key is only unique within the
    producer that chose it, so an unscoped key is refused (400) rather
    than quietly given global reach. The producer scopes the key and
    nothing else: it is recorded on the metadata row, never on the
    ``ArtifactRecord``, which has no producer field.

    A replay under the same ``(producer, key)`` returns the stored
    ``ArtifactRecord`` — the *same* ``artifact_id``, not a second record
    of the same bytes. A materially different body (different content,
    size, content-type or filename) under the same key is a 409
    ``idempotency_conflict``. Without a key, behaviour is exactly as
    before: every call registers a new artifact.

    Note the replay is resolved against the metadata store *before* the
    bytes reach the artifact store. Doing it the other way round would
    mint a fresh ``artifact_id`` and write a sidecar for every retry, then
    discard it on discovering the replay — leaving artifacts on disk that
    ``/download`` would serve but that this registry never issued and
    ``GET /v1/artifacts/{id}`` reports as unknown. A conflicting
    resubmission likewise stores nothing at all.
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

    content_type = _resolve_artifact_content_type(
        request.headers.get("content-type"), content_type_declared
    )

    idem_key = idempotency_key_header or None
    if idem_key:
        scoping_producer = _require_producer_for_key(producer)
        prior = metadata_store.find_artifact_by_idempotency_key(
            producer=scoping_producer, idempotency_key=idem_key
        )
        if prior is not None:
            computed_sha256 = hashlib.sha256(data).hexdigest()
            # ArtifactStore.put() normally enforces this (see
            # cer.contract.stores, "Checksums"), but the replay path
            # deliberately never reaches it. A declared checksum that does
            # not match the bytes must still fail loudly rather than be
            # waved through as "a retry" — otherwise supplying a used key
            # would be a way to bypass the integrity check entirely.
            if declared_sha256 is not None and declared_sha256.lower() != computed_sha256:
                raise ChecksumMismatchError(
                    f"declared_sha256 {declared_sha256!r} does not match computed "
                    f"digest {computed_sha256!r} — nothing written"
                )
            # A resubmission under a key already used by this producer.
            # Overlay only what this request actually carries onto the
            # stored record: the store-assigned fields (artifact_id, uri,
            # registered_at) and the attachment fields belong to the
            # original registration and are excluded from the idempotency
            # comparison anyway (attach_artifact is the sanctioned path
            # for re-linking, and registered_at is stamped per attempt).
            # register_artifact then decides — faithful replay, or 409.
            candidate = prior.model_copy(
                update={
                    "sha256": computed_sha256,
                    "size_bytes": len(data),
                    "content_type": content_type,
                    "filename": filename,
                }
            )
            return metadata_store.register_artifact(
                candidate, idempotency_key=idem_key, producer=scoping_producer
            )

    record = artifact_store.put(
        data,
        content_type=content_type,
        filename=filename,
        declared_sha256=declared_sha256,
    )
    if run_id is not None or evidence_id is not None:
        record = record.model_copy(update={"run_id": run_id, "evidence_id": evidence_id})
    return metadata_store.register_artifact(
        record, idempotency_key=idem_key, producer=producer if idem_key else None
    )


@api_router.get("/artifacts/{artifact_id}", response_model=ArtifactRecord)
def get_artifact_metadata(
    artifact_id: str,
    metadata_store: MetadataStore = Depends(get_metadata_store),
) -> ArtifactRecord:
    """Return the authoritative ``ArtifactRecord`` for ``artifact_id``.

    The metadata store — not the artifact store's ``stat()`` sidecar — is
    authoritative for lineage (``evidence_id``/``run_id``): attachment can
    happen after registration (see ``attach_artifact``), and only the
    metadata store observes that. The filesystem sidecar is written once
    at ``put()`` time and never updated, so reading it here would silently
    report stale lineage forever after the first attach.

    ``/download`` continues to be served from the artifact store
    (``stat()`` + ``get()``, with its read-time checksum verification)
    unchanged — that is the bytes path, and this is the metadata path.
    """
    return metadata_store.get_artifact(artifact_id)


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
    idempotency_key_header: Optional[str] = Header(default=None, alias=IDEMPOTENCY_KEY_HEADER),
) -> PromotionTransition:
    idem_key = _resolve_idempotency_key(idempotency_key_header, body.idempotency_key)
    transition = PromotionTransition(
        transition_id=_generate_id("trn_", "promotion", idem_key, body.producer),
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
    return metadata_store.record_promotion(transition, idempotency_key=idem_key)


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
    idempotency_key_header: Optional[str] = Header(default=None, alias=IDEMPOTENCY_KEY_HEADER),
) -> StrategyHealthRecord:
    idem_key = _resolve_idempotency_key(idempotency_key_header, body.idempotency_key)
    record = StrategyHealthRecord(
        health_id=_generate_id("hlt_", "health", idem_key, body.producer),
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
    return metadata_store.record_health(record, idempotency_key=idem_key)


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
