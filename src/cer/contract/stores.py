"""Storage interfaces CER backends implement.

These are ``typing.Protocol`` definitions only — no implementations. They
are the fixed boundary between the contract layer (this package) and the
storage backends (a hash-addressed filesystem artifact store, a SQLite
metadata store) and the HTTP service, all built by other Engineers against
these exact names and signatures.

Semantics implementers must honour
---------------------------------

* **Idempotency.** :meth:`MetadataStore.create_run`,
  :meth:`MetadataStore.append_evidence`,
  :meth:`MetadataStore.create_experiment`,
  :meth:`MetadataStore.record_promotion` and
  :meth:`MetadataStore.record_health` are idempotent on
  ``idempotency_key`` (for ``append_evidence``, the key lives on the
  ``EvidenceRecord`` itself). A replay of the *same* key with an identical
  body must return the existing record unchanged (no new record, no
  error). A replay of the *same* key with a *different* body must raise
  :class:`~cer.contract.errors.IdempotencyConflictError` — duplicate
  submissions must be detectable, and duplicate run creation must never
  create ambiguous identities.

  The key is *required* only for ``append_evidence``. On the other four
  it is optional: given no key, an implementation behaves as it would
  for any ordinary create (a fresh identity, a new record). Accepting a
  key and then ignoring it is not a permitted implementation — a
  producer that supplies one must get the retry-safety it asked for.

  Keys are scoped to the producer, never global: the identity of a key
  is ``(producer, idempotency_key)``. For ``record_promotion`` and
  ``record_health`` the producer is taken from the record itself
  (``PromotionTransition.producer`` / ``StrategyHealthRecord.producer``).

* **Immutability.** Evidence and artifacts are immutable once written.
  Silent replacement is forbidden: writing a second time to the same
  ``evidence_id``/``artifact_id`` with different content (i.e. not an
  idempotent replay — see above) must raise
  :class:`~cer.contract.errors.ImmutabilityError`, never overwrite.
  Historical evidence must never be silently rewritten to match a newer
  strategy version.

* **Not found.** Any query/get by id for a record that does not exist
  raises :class:`~cer.contract.errors.NotFoundError`.

* **Health.** ``health()`` returns ``None`` on success and raises
  :class:`~cer.contract.errors.MetadataStoreError` /
  :class:`~cer.contract.errors.ArtifactStoreError` (as appropriate) when
  the backend is unavailable — it never returns a falsy/boolean sentinel.

* **Checksums.** :meth:`ArtifactStore.put` computes the artifact's
  ``sha256`` from ``data``. If ``declared_sha256`` is given and does not
  match the computed digest, implementations must raise
  :class:`~cer.contract.errors.ChecksumMismatchError` rather than storing
  the artifact under a mismatched identity.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .enums import EvidenceType, RunStatus
from .models import (
    ArtifactRecord,
    Experiment,
    EvidenceRecord,
    PromotionTransition,
    Run,
    Strategy,
    StrategyHealthRecord,
    StrategyVersion,
)


@runtime_checkable
class ArtifactStore(Protocol):
    """Hash-addressed bulk artifact storage, backend-neutral."""

    def put(
        self,
        data: bytes,
        *,
        content_type: str,
        filename: str,
        declared_sha256: str | None = None,
    ) -> ArtifactRecord:
        """Register and store ``data``, returning its ``ArtifactRecord``.

        Raises ``ChecksumMismatchError`` if ``declared_sha256`` is given
        and does not match the computed digest of ``data``.
        """
        ...

    def get(self, artifact_id: str) -> bytes:
        """Return the raw bytes of a previously-registered artifact.

        Raises ``NotFoundError`` if ``artifact_id`` is unknown.
        """
        ...

    def stat(self, artifact_id: str) -> ArtifactRecord:
        """Return the ``ArtifactRecord`` for ``artifact_id`` without its bytes.

        Raises ``NotFoundError`` if ``artifact_id`` is unknown.
        """
        ...

    def exists(self, artifact_id: str) -> bool:
        """Return whether ``artifact_id`` is registered. Never raises for a miss."""
        ...

    def health(self) -> None:
        """Raise ``ArtifactStoreError`` if the backend is unhealthy; else return None."""
        ...


@runtime_checkable
class MetadataStore(Protocol):
    """Structured metadata storage: strategies, experiments, runs, evidence,
    artifacts, promotions and health — backend-neutral (SQLite or otherwise)."""

    def register_strategy(self, strategy: Strategy) -> Strategy: ...

    def register_strategy_version(self, sv: StrategyVersion) -> StrategyVersion: ...

    def create_experiment(
        self, experiment: Experiment, *, idempotency_key: str | None = None
    ) -> Experiment:
        """Create an experiment. Idempotent on ``idempotency_key`` when one is
        given — see module docstring."""
        ...

    def create_run(self, run: Run, *, idempotency_key: str | None = None) -> Run:
        """Create a run. Idempotent on ``idempotency_key`` — see module docstring."""
        ...

    def close_run(self, run_id: str, *, status: RunStatus, ended_at: datetime) -> Run:
        """Finalise a run. Raises ``NotFoundError`` if ``run_id`` is unknown."""
        ...

    def append_evidence(self, evidence: EvidenceRecord) -> EvidenceRecord:
        """Append an evidence record. Idempotent on ``evidence.idempotency_key``;
        immutable thereafter — see module docstring."""
        ...

    def register_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        """Register artifact metadata. Immutable thereafter — see module docstring."""
        ...

    def attach_artifact(
        self,
        artifact_id: str,
        *,
        evidence_id: str | None = None,
        run_id: str | None = None,
    ) -> ArtifactRecord:
        """Link a registered artifact to evidence and/or a run.

        Raises ``NotFoundError`` if ``artifact_id`` (or the referenced
        ``evidence_id``/``run_id``) is unknown.
        """
        ...

    def record_promotion(
        self, transition: PromotionTransition, *, idempotency_key: str | None = None
    ) -> PromotionTransition:
        """Record a promotion transition. Idempotent on ``idempotency_key``
        when one is given, scoped to ``transition.producer`` — see module
        docstring."""
        ...

    def record_health(
        self, record: StrategyHealthRecord, *, idempotency_key: str | None = None
    ) -> StrategyHealthRecord:
        """Record a strategy-health observation. Idempotent on
        ``idempotency_key`` when one is given, scoped to ``record.producer``
        — see module docstring."""
        ...

    def get_evidence(self, evidence_id: str) -> EvidenceRecord:
        """Raises ``NotFoundError`` if ``evidence_id`` is unknown."""
        ...

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        """Return the authoritative ``ArtifactRecord`` for ``artifact_id``,
        including any evidence/run attachment recorded since registration.

        Raises ``NotFoundError`` if ``artifact_id`` is unknown.
        """
        ...

    def query_evidence(
        self,
        *,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        run_id: str | None = None,
        experiment_id: str | None = None,
        evidence_type: EvidenceType | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EvidenceRecord]: ...

    def query_artifacts(
        self,
        *,
        evidence_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ArtifactRecord]: ...

    def query_promotions(
        self,
        *,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PromotionTransition]: ...

    def query_health(
        self,
        *,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[StrategyHealthRecord]: ...

    def health(self) -> None:
        """Raise ``MetadataStoreError`` if the backend is unhealthy; else return None."""
        ...
