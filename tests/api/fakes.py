"""In-memory fakes satisfying the ``MetadataStore``/``ArtifactStore``
Protocols, for ``cer.api`` tests only.

These are deliberately independent of (and simpler than) whatever the real
``cer.metadata`` (SQLite) and ``cer.artifacts`` (filesystem) backends turn
out to be — this package's tests exist to prove the HTTP layer behaves
correctly against *any* conforming implementation of the Protocols, not to
duplicate those other Engineers' work.

Idempotency-comparison judgment call
-------------------------------------
``MetadataStore.create_run``/``append_evidence`` are documented as
idempotent on their key: "a replay of the *same* key with an *identical*
body must return the existing record". A literal field-for-field body
comparison would spuriously treat a legitimate retry as a *conflicting*
replay whenever a server-defaulted timestamp (``started_at``,
``created_at_utc``) differs between the original call and the retry
(callers generally don't pin these explicitly). These fakes therefore
compare bodies on their caller-meaningful fields only, excluding the
identity field itself and server-defaulted timestamps/derived provenance
fields. This is a fake-specific, documented judgment call — the real
SQLite ``MetadataStore`` Engineer owns their own interpretation.

Idempotency keys are scoped per producer
------------------------------------------
Conflict bookkeeping is keyed on ``(producer, idempotency_key)``, not the
bare key — mirroring the real SQLite store's ``UNIQUE (producer,
idempotency_key)`` constraint. Two different producers submitting the same
key string are unrelated submissions, each independently idempotent on
their own retries; one producer's key can never collide with, or be
rejected because of, another producer's use of the same string.

The fake must not model less than the real store
--------------------------------------------------
``create_experiment``, ``record_promotion``, ``record_health`` and
``register_artifact`` take an optional ``idempotency_key`` here too, and
honour it the same way ``create_run`` does — because the real
``SQLiteMetadataStore`` does. A fake that quietly ignored a key the real
store honours (or vice versa) is precisely how an earlier defect in this
project survived two green suites: the API tests would pass against a
fake that cannot exhibit the bug. Any future change to the real store's
idempotency semantics belongs here in the same commit.

``FakeArtifactStore`` mints a fresh artifact_id per put()
-----------------------------------------------------------
It used to derive ``artifact_id`` from the content digest
(``art_<sha256>``), so two registrations of identical bytes collapsed
into one record and one id. The real ``FilesystemArtifactStore`` does the
opposite on purpose: identical bytes share one *blob* but get one
``artifact_id`` and one sidecar *per registration* (see its module
docstring, "Every registration gets its own identity"). That divergence
is exactly what hid the missing artifact idempotency from these tests —
against this fake a duplicate POST /v1/artifacts appeared to dedup
perfectly, while against the real store it produced two indistinguishable
records for one logical registration. The fake now models the real
store: one blob per digest, one record per put().
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Optional

from cer.contract.enums import EvidenceType, RunStatus
from cer.contract.errors import (
    ArtifactStoreError,
    ChecksumMismatchError,
    ContractViolationError,
    IdempotencyConflictError,
    ImmutabilityError,
    MetadataStoreError,
    NotFoundError,
)
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
from cer.runtime.clock import utcnow

#: A producer value that makes the fakes raise a bare, non-CER exception —
#: used by tests to exercise the "unexpected internal error -> 500, no
#: traceback/path leaked" path without needing a real bug.
BOOM_PRODUCER = "__BOOM__"


def _boom_if_requested(producer: Optional[str]) -> None:
    if producer == BOOM_PRODUCER:
        raise RuntimeError("simulated unexpected failure at /srv/cer-worktrees/w2-api (should never leak)")


class FakeMetadataStore:
    """In-memory MetadataStore fake."""

    def __init__(self) -> None:
        self._strategies: dict[str, Strategy] = {}
        self._strategy_versions: dict[tuple[str, str], StrategyVersion] = {}
        self._experiments: dict[str, Experiment] = {}
        self._experiment_idempotency: dict[tuple[str, str], str] = {}
        self._runs: dict[str, Run] = {}
        #: Keyed on (producer, idempotency_key) -- idempotency keys are
        #: scoped per-producer, mirroring the real SQLite store's
        #: UNIQUE (producer, idempotency_key) constraint. Two producers
        #: using the same key string are unrelated submissions.
        self._run_idempotency: dict[tuple[str, str], str] = {}
        self._evidence: dict[str, EvidenceRecord] = {}
        self._evidence_idempotency: dict[tuple[str, str], str] = {}
        self._artifacts: dict[str, ArtifactRecord] = {}
        #: (producer, idempotency_key) -> artifact_id. Artifact keys are
        #: producer-scoped like every other key; ArtifactRecord has no
        #: producer field, so the producer arrives as an argument.
        self._artifact_idempotency: dict[tuple[str, str], str] = {}
        self._promotions: list[PromotionTransition] = []
        self._promotion_idempotency: dict[tuple[str, str], PromotionTransition] = {}
        self._health_records: list[StrategyHealthRecord] = []
        self._health_idempotency: dict[tuple[str, str], StrategyHealthRecord] = {}
        self.healthy = True
        #: When True, the next representative read/write raises
        #: MetadataStoreError — simulates an operational backend failure
        #: independent of /ready's own health() check (tests: "the
        #: dependency is unavailable, not the caller's fault" -> 503).
        self.simulate_store_error = False

    def _maybe_raise_store_error(self) -> None:
        if self.simulate_store_error:
            raise MetadataStoreError("simulated metadata store failure")

    # --- strategy / strategy version -----------------------------------

    def register_strategy(self, strategy: Strategy) -> Strategy:
        self._strategies[strategy.strategy_id] = strategy
        return strategy

    def register_strategy_version(self, sv: StrategyVersion) -> StrategyVersion:
        self._strategy_versions[(sv.strategy_id, sv.strategy_version)] = sv
        return sv

    # --- experiment -------------------------------------------------------

    @staticmethod
    def _experiment_body_key(experiment: Experiment) -> tuple:
        # Excludes the generated identity and the server-defaulted
        # created_at, mirroring the real store's _EXPERIMENT_FP_EXCLUDE.
        data = experiment.model_dump(exclude={"experiment_id", "created_at"})
        return tuple(sorted(data.items(), key=lambda kv: kv[0]))

    def create_experiment(
        self, experiment: Experiment, *, idempotency_key: Optional[str] = None
    ) -> Experiment:
        _boom_if_requested(experiment.producer)
        if idempotency_key is not None:
            scoped_key = (experiment.producer, idempotency_key)
            prior_id = self._experiment_idempotency.get(scoped_key)
            if prior_id is not None:
                prior = self._experiments[prior_id]
                if self._experiment_body_key(prior) == self._experiment_body_key(experiment):
                    return prior
                raise IdempotencyConflictError(
                    f"idempotency key {idempotency_key!r} for producer "
                    f"{experiment.producer!r} was already used with a different "
                    "experiment body"
                )
        self._experiments[experiment.experiment_id] = experiment
        if idempotency_key is not None:
            self._experiment_idempotency[(experiment.producer, idempotency_key)] = (
                experiment.experiment_id
            )
        return experiment

    # --- run ----------------------------------------------------------------

    @staticmethod
    def _run_body_key(run: Run) -> tuple:
        return (
            run.experiment_id,
            run.status,
            run.producer,
            run.producer_version,
            run.git_repo,
            run.git_commit,
            run.dataset_id,
            run.dataset_version,
            run.dataset_ref,
            run.config_hash,
            run.config_ref,
            run.environment,
        )

    def create_run(self, run: Run, *, idempotency_key: Optional[str] = None) -> Run:
        _boom_if_requested(run.producer)
        if idempotency_key is not None:
            scoped_key = (run.producer, idempotency_key)
            prior_id = self._run_idempotency.get(scoped_key)
            if prior_id is not None:
                prior = self._runs[prior_id]
                if self._run_body_key(prior) == self._run_body_key(run):
                    return prior
                raise IdempotencyConflictError(
                    f"idempotency key {idempotency_key!r} for producer {run.producer!r} "
                    "was already used with a different run body"
                )
        if run.run_id in self._runs:
            raise ImmutabilityError(f"run {run.run_id} already exists")
        self._runs[run.run_id] = run
        if idempotency_key is not None:
            self._run_idempotency[(run.producer, idempotency_key)] = run.run_id
        return run

    def close_run(self, run_id: str, *, status: RunStatus, ended_at: datetime) -> Run:
        run = self._runs.get(run_id)
        if run is None:
            raise NotFoundError(f"run {run_id} not found")
        updated = run.model_copy(update={"status": status, "ended_at": ended_at})
        self._runs[run_id] = updated
        return updated

    # --- evidence -------------------------------------------------------

    @staticmethod
    def _evidence_body_key(evidence: EvidenceRecord) -> tuple:
        data = evidence.model_dump(
            exclude={
                "evidence_id",
                "created_at_utc",
                "provenance_completeness",
                "missing_provenance",
            }
        )
        return tuple(sorted(data.items(), key=lambda kv: kv[0]))

    def append_evidence(self, evidence: EvidenceRecord) -> EvidenceRecord:
        _boom_if_requested(evidence.producer)
        scoped_key = (evidence.producer, evidence.idempotency_key)
        prior_id = self._evidence_idempotency.get(scoped_key)
        if prior_id is not None:
            prior = self._evidence[prior_id]
            if self._evidence_body_key(prior) == self._evidence_body_key(evidence):
                return prior
            raise IdempotencyConflictError(
                f"idempotency key {evidence.idempotency_key!r} for producer "
                f"{evidence.producer!r} was already used with a different evidence body"
            )
        if evidence.evidence_id in self._evidence:
            raise ImmutabilityError(f"evidence {evidence.evidence_id} already exists")
        self._evidence[evidence.evidence_id] = evidence
        self._evidence_idempotency[scoped_key] = evidence.evidence_id
        return evidence

    def get_evidence(self, evidence_id: str) -> EvidenceRecord:
        self._maybe_raise_store_error()
        try:
            return self._evidence[evidence_id]
        except KeyError:
            raise NotFoundError(f"evidence {evidence_id} not found") from None

    def query_evidence(
        self,
        *,
        strategy_id: Optional[str] = None,
        strategy_version: Optional[str] = None,
        run_id: Optional[str] = None,
        experiment_id: Optional[str] = None,
        evidence_type: Optional[EvidenceType] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EvidenceRecord]:
        results = list(self._evidence.values())
        if strategy_id is not None:
            results = [e for e in results if e.strategy_id == strategy_id]
        if strategy_version is not None:
            results = [e for e in results if e.strategy_version == strategy_version]
        if run_id is not None:
            results = [e for e in results if e.run_id == run_id]
        if experiment_id is not None:
            results = [e for e in results if e.experiment_id == experiment_id]
        if evidence_type is not None:
            results = [e for e in results if e.evidence_type == evidence_type]
        if since is not None:
            results = [e for e in results if e.created_at_utc >= since]
        if until is not None:
            results = [e for e in results if e.created_at_utc <= until]
        results.sort(key=lambda e: e.created_at_utc)
        return results[offset : offset + limit]

    # --- artifacts ------------------------------------------------------

    @staticmethod
    def _artifact_body_key(artifact: ArtifactRecord) -> tuple:
        """The caller-authored content of an artifact registration.

        Mirrors the real store's _ARTIFACT_IDEM_FP_EXCLUDE: artifact_id,
        uri and registered_at are assigned by the server per attempt, and
        run_id/evidence_id are owned by attach_artifact — so none of them
        distinguish one logical registration from another. What the
        producer actually submitted is what remains.
        """
        return (
            artifact.sha256,
            artifact.size_bytes,
            artifact.content_type,
            artifact.filename,
        )

    def find_artifact_by_idempotency_key(
        self, *, producer: str, idempotency_key: str
    ) -> Optional[ArtifactRecord]:
        if not producer or not producer.strip():
            raise ContractViolationError(
                f"idempotency_key {idempotency_key!r} requires a non-empty producer"
            )
        prior_id = self._artifact_idempotency.get((producer, idempotency_key))
        return None if prior_id is None else self._artifacts[prior_id]

    def register_artifact(
        self,
        artifact: ArtifactRecord,
        *,
        idempotency_key: Optional[str] = None,
        producer: Optional[str] = None,
    ) -> ArtifactRecord:
        if idempotency_key is not None:
            if not producer or not producer.strip():
                raise ContractViolationError(
                    f"idempotency_key {idempotency_key!r} requires a non-empty producer "
                    "to scope it (idempotency keys are scoped per-producer, not global)"
                )
            scoped_key = (producer, idempotency_key)
            prior_id = self._artifact_idempotency.get(scoped_key)
            if prior_id is not None:
                prior = self._artifacts[prior_id]
                if self._artifact_body_key(prior) == self._artifact_body_key(artifact):
                    return prior
                raise IdempotencyConflictError(
                    f"idempotency key {idempotency_key!r} for producer {producer!r} "
                    "was already used to register a materially different artifact"
                )
        existing = self._artifacts.get(artifact.artifact_id)
        if existing is not None:
            if existing == artifact:
                return existing
            raise ImmutabilityError(f"artifact {artifact.artifact_id} already registered with different metadata")
        self._artifacts[artifact.artifact_id] = artifact
        if idempotency_key is not None:
            self._artifact_idempotency[(producer, idempotency_key)] = artifact.artifact_id
        return artifact

    def attach_artifact(
        self,
        artifact_id: str,
        *,
        evidence_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> ArtifactRecord:
        record = self._artifacts.get(artifact_id)
        if record is None:
            raise NotFoundError(f"artifact {artifact_id} not found")
        if evidence_id is not None and evidence_id not in self._evidence:
            raise NotFoundError(f"evidence {evidence_id} not found")
        if run_id is not None and run_id not in self._runs:
            raise NotFoundError(f"run {run_id} not found")
        update = {}
        if evidence_id is not None:
            update["evidence_id"] = evidence_id
        if run_id is not None:
            update["run_id"] = run_id
        updated = record.model_copy(update=update)
        self._artifacts[artifact_id] = updated
        return updated

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        try:
            return self._artifacts[artifact_id]
        except KeyError:
            raise NotFoundError(f"artifact {artifact_id} not found") from None

    def query_artifacts(
        self,
        *,
        evidence_id: Optional[str] = None,
        run_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ArtifactRecord]:
        results = list(self._artifacts.values())
        if evidence_id is not None:
            results = [a for a in results if a.evidence_id == evidence_id]
        if run_id is not None:
            results = [a for a in results if a.run_id == run_id]
        results.sort(key=lambda a: a.registered_at)
        return results[offset : offset + limit]

    # --- promotion / health -------------------------------------------------

    @staticmethod
    def _promotion_body_key(transition: PromotionTransition) -> tuple:
        # Excludes the generated identity and the server-defaulted at_utc,
        # mirroring the real store's _PROMOTION_FP_EXCLUDE.
        data = transition.model_dump(exclude={"transition_id", "at_utc"})
        return tuple(sorted(data.items(), key=lambda kv: str(kv[0])))

    @staticmethod
    def _health_body_key(record: StrategyHealthRecord) -> tuple:
        # Excludes the generated identity and the server-defaulted
        # observed_at_utc, mirroring the real store's _HEALTH_FP_EXCLUDE.
        data = record.model_dump(exclude={"health_id", "observed_at_utc"})
        return tuple(sorted(data.items(), key=lambda kv: str(kv[0])))

    def record_promotion(
        self, transition: PromotionTransition, *, idempotency_key: Optional[str] = None
    ) -> PromotionTransition:
        _boom_if_requested(transition.producer)
        # Producer comes from the record itself, as it does in the real
        # store -- there is no separate producer argument here.
        if idempotency_key is not None:
            scoped_key = (transition.producer, idempotency_key)
            prior = self._promotion_idempotency.get(scoped_key)
            if prior is not None:
                if self._promotion_body_key(prior) == self._promotion_body_key(transition):
                    return prior
                raise IdempotencyConflictError(
                    f"idempotency key {idempotency_key!r} for producer "
                    f"{transition.producer!r} was already used with a different "
                    "promotion transition body"
                )
        self._promotions.append(transition)
        if idempotency_key is not None:
            self._promotion_idempotency[(transition.producer, idempotency_key)] = transition
        return transition

    def record_health(
        self, record: StrategyHealthRecord, *, idempotency_key: Optional[str] = None
    ) -> StrategyHealthRecord:
        _boom_if_requested(record.producer)
        if idempotency_key is not None:
            scoped_key = (record.producer, idempotency_key)
            prior = self._health_idempotency.get(scoped_key)
            if prior is not None:
                if self._health_body_key(prior) == self._health_body_key(record):
                    return prior
                raise IdempotencyConflictError(
                    f"idempotency key {idempotency_key!r} for producer "
                    f"{record.producer!r} was already used with a different "
                    "health record body"
                )
        self._health_records.append(record)
        if idempotency_key is not None:
            self._health_idempotency[(record.producer, idempotency_key)] = record
        return record

    def query_promotions(
        self,
        *,
        strategy_id: Optional[str] = None,
        strategy_version: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PromotionTransition]:
        results = list(self._promotions)
        if strategy_id is not None:
            results = [p for p in results if p.strategy_id == strategy_id]
        if strategy_version is not None:
            results = [p for p in results if p.strategy_version == strategy_version]
        results.sort(key=lambda p: p.at_utc)
        return results[offset : offset + limit]

    def query_health(
        self,
        *,
        strategy_id: Optional[str] = None,
        strategy_version: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[StrategyHealthRecord]:
        results = list(self._health_records)
        if strategy_id is not None:
            results = [h for h in results if h.strategy_id == strategy_id]
        if strategy_version is not None:
            results = [h for h in results if h.strategy_version == strategy_version]
        results.sort(key=lambda h: h.observed_at_utc)
        return results[offset : offset + limit]

    # --- health -----------------------------------------------------------

    def health(self) -> None:
        if not self.healthy:
            raise MetadataStoreError("fake metadata store is unhealthy")


class FakeArtifactStore:
    """In-memory, hash-addressed ArtifactStore fake."""

    def __init__(self) -> None:
        #: Keyed by content digest -- identical bytes occupy one blob,
        #: exactly as the real store's content-addressed layout does.
        self._blobs: dict[str, bytes] = {}
        #: Keyed by artifact_id -- one record per put(), as the real store
        #: writes one sidecar per registration.
        self._records: dict[str, ArtifactRecord] = {}
        self.healthy = True
        #: See FakeMetadataStore.simulate_store_error.
        self.simulate_store_error = False

    def put(
        self,
        data: bytes,
        *,
        content_type: str,
        filename: str,
        declared_sha256: Optional[str] = None,
    ) -> ArtifactRecord:
        digest = hashlib.sha256(data).hexdigest()
        if declared_sha256 is not None and declared_sha256.lower() != digest:
            raise ChecksumMismatchError(
                f"declared sha256 {declared_sha256!r} does not match computed digest"
            )
        # Every registration gets its own identity, as the real store
        # does: identical bytes share one blob but are two artifacts. A
        # fake that collapsed them would make duplicate registrations
        # invisible to these tests -- see the module docstring.
        artifact_id = f"art_{uuid.uuid4().hex}"
        record = ArtifactRecord(
            artifact_id=artifact_id,
            sha256=digest,
            size_bytes=len(data),
            content_type=content_type,
            filename=filename,
            uri=f"fake://artifacts/blobs/{digest}",
            registered_at=utcnow(),
        )
        self._blobs[digest] = data
        self._records[artifact_id] = record
        return record

    def get(self, artifact_id: str) -> bytes:
        if self.simulate_store_error:
            raise ArtifactStoreError("simulated artifact store failure")
        record = self._records.get(artifact_id)
        if record is None:
            raise NotFoundError(f"artifact {artifact_id} not found")
        try:
            return self._blobs[record.sha256]
        except KeyError:
            raise ArtifactStoreError(
                f"artifact {artifact_id} is registered but its blob is missing"
            ) from None

    def stat(self, artifact_id: str) -> ArtifactRecord:
        if self.simulate_store_error:
            raise ArtifactStoreError("simulated artifact store failure")
        try:
            return self._records[artifact_id]
        except KeyError:
            raise NotFoundError(f"artifact {artifact_id} not found") from None

    def exists(self, artifact_id: str) -> bool:
        return artifact_id in self._records

    def health(self) -> None:
        if not self.healthy:
            raise ArtifactStoreError("fake artifact store is unhealthy")
