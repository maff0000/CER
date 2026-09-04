"""SQLite implementation of :class:`cer.contract.stores.MetadataStore`.

Thread-safety
-------------
One SQLite connection per thread (``threading.local``), each opened with
``journal_mode=WAL``, ``foreign_keys=ON`` and a busy timeout so that a
concurrent writer blocks-and-retries at the SQLite level rather than
failing immediately with ``SQLITE_BUSY``. Every multi-step write (a
lookup-then-insert for idempotency, a compare-then-reject for
immutability, the attach/close state transitions) runs inside an explicit
``BEGIN IMMEDIATE ... COMMIT`` transaction on that thread's connection.
``BEGIN IMMEDIATE`` acquires SQLite's write lock up front, so a second
thread's ``BEGIN IMMEDIATE`` blocks until the first transaction finishes
-- that serialisation, not any lock held in this Python process, is what
makes the concurrent-replay behaviour correct: by the time the second
transaction's lookup runs, the first transaction's insert is either fully
visible or never happened.

Idempotency fingerprint
------------------------
See :func:`_fingerprint` and the ``_..._FP_EXCLUDE`` sets below for
exactly which fields participate in the "is this a replay of identical
content, or a conflicting resubmission" decision for ``create_run`` and
``append_evidence`` (idempotency-key based) and ``register_artifact``
(artifact_id based immutability). In short: a record's own generated
primary identity field is excluded (a caller may legitimately mint a
fresh id per retry attempt while reusing the same idempotency key -- see
the docstrings below), fields that are purely derived/recomputed by the
pydantic model itself are excluded (comparing them would be redundant,
never additionally informative), and an artifact's ``run_id``/
``evidence_id`` are excluded because ``attach_artifact`` is the one
sanctioned path for setting or changing them, not re-registration. Every
other content field is part of the fingerprint by default -- but see the
next section for the one deliberate carve-out among timestamps.

Server-assigned creation timestamps vs. caller-authored semantic fields
--------------------------------------------------------------------------
``started_at`` (``create_run``) and ``created_at_utc`` (``append_evidence``)
are excluded from the fingerprint. These are record-creation timestamps: a
caller that omits them lets the API layer fill them with wall-clock time
at request-handling, which is regenerated on *every* attempt -- so a
byte-identical retry through HTTP would otherwise hash differently on
this field alone and could never be recognised as a replay, defeating
idempotency for exactly the callers who rely on it most (ones that don't
pin their own timestamp). Server-assigned creation timestamps are
excluded from the fingerprint because the service generates them per
attempt; caller-authored semantic fields, including
``EvidenceRecord.observed_at_utc``, remain part of the content. The same
rule -- one rule, one helper, one set of exclusions -- governs every
comparison in this store: ``Strategy.created_at``,
``StrategyVersion.created_at``, ``Experiment.created_at``,
``PromotionTransition.at_utc`` and ``StrategyHealthRecord.observed_at_utc``
are all stamped by the API layer when the caller omits them, so all are
excluded. (``StrategyHealthRecord.observed_at_utc`` is the one that reads
like a caller-authored field but is not: the model requires it, so the
API defaults it to wall-clock time per attempt, unlike
``EvidenceRecord.observed_at_utc``, which is left ``None`` when the
producer omits it and is therefore genuinely caller-authored.)
Nothing else is excluded: a real content change -- a different
``thesis``, ``git_commit``, ``verdict`` or metric -- is still detected
and still refused. ``observed_at_utc`` is deliberately *kept in*
the fingerprint: it is when the producer says the thing was observed, not
when the row happened to be created, and a producer that reuses a key
while genuinely changing the observation time must still get a loud
``IdempotencyConflictError``, not a silently-returned stale record.

Writes verify their durable backing before acknowledging
----------------------------------------------------------
Every write goes through :meth:`SQLiteMetadataStore._transaction`, which
first calls :meth:`SQLiteMetadataStore._require_durable_backing` -- the
same existence check :meth:`SQLiteMetadataStore.health` performs -- and
raises ``MetadataStoreError`` (HTTP 503) rather than committing into an
unlinked inode. Without it, a process whose data directory disappeared
underneath it goes on serving ``201 Created`` for records that are
already lost: SQLite keeps writing to its open file descriptor, reads
them straight back, and they vanish at the next restart. A producer that
receives ``201`` must be able to rely on it, so the write path asks the
same question ``/ready`` asks.

Idempotency keys are scoped to the producer
--------------------------------------------
``create_run``'s and ``append_evidence``'s idempotency lookups are keyed
on ``(producer, idempotency_key)``, never on ``idempotency_key`` alone
(enforced by a composite unique index -- see
``schema/002_producer_scoped_idempotency.sql``). Two different producers
may safely use the identical key string for two entirely unrelated
records: HSA and NEO can each choose a natural key like
``"2026-09-04-btc-sweep"`` without coordinating with one another, and
neither can deny or collide with the other's key. Only a producer's own
retry -- same ``producer`` *and* same ``idempotency_key`` -- is treated
as a replay of the same logical submission.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from cer.contract.enums import EvidenceType, HealthState, PromotionState, RunStatus
from cer.contract.errors import (
    ImmutabilityError,
    IdempotencyConflictError,
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
from cer.runtime.clock import to_utc

from .migrations import apply_migrations

__all__ = ["SQLiteMetadataStore"]

_SCHEMA_DIR = Path(__file__).parent / "schema"
_DEFAULT_BUSY_TIMEOUT_MS = 5_000

# --- Idempotency / immutability fingerprint field exclusions ----------------

#: run_id is a generated identity minted by the caller before calling
#: create_run; excluded so a caller that mints a fresh run_id per retry
#: attempt (while reusing the same idempotency_key) is still recognised as
#: a replay. provenance_completeness/missing_provenance are derived purely
#: from the other fields, so including them adds no discriminating power.
#: started_at is the record-creation timestamp -- see the module
#: docstring's "server-assigned creation timestamps" section for why it is
#: excluded too: a caller that omits it lets the API layer fill it with
#: wall-clock time at request-handling, which differs on every attempt and
#: would otherwise make every HTTP retry hash differently.
_RUN_FP_EXCLUDE = {"run_id", "started_at", "provenance_completeness", "missing_provenance"}

#: Same reasoning as _RUN_FP_EXCLUDE, for EvidenceRecord/evidence_id, with
#: created_at_utc playing the role started_at plays for Run. Note
#: observed_at_utc is deliberately NOT excluded -- see the module
#: docstring.
_EVIDENCE_FP_EXCLUDE = {"evidence_id", "created_at_utc", "provenance_completeness", "missing_provenance"}

#: Registration is idempotent on the record's own natural identity
#: (strategy_id, or strategy_id+strategy_version): re-registering
#: identical content is a no-op that returns the stored record, and only
#: genuinely different content raises ImmutabilityError. created_at is
#: excluded from that comparison for the same reason it is excluded
#: everywhere else -- a caller that omits it lets the API layer stamp
#: wall-clock time at request-handling, regenerated on every attempt, so
#: comparing it would report an honest retry as an immutability
#: violation ("your data conflicts with stored history") when nothing
#: conflicts at all. Everything a caller actually authored -- name,
#: thesis, git_repo, git_commit, notes -- still participates, so a real
#: content change is still refused.
_STRATEGY_FP_EXCLUDE = {"created_at"}
_STRATEGY_VERSION_FP_EXCLUDE = {"created_at"}

#: Same reasoning as _RUN_FP_EXCLUDE, for the three write paths whose
#: idempotency key is optional. In each case the record's own generated
#: identity field is excluded (a caller may mint a fresh id per retry
#: while reusing the key), and so is the record-creation timestamp the
#: API layer fills with wall-clock time when the caller omits it --
#: created_at (Experiment), at_utc (PromotionTransition) and
#: observed_at_utc (StrategyHealthRecord). Those three are server-assigned
#: per attempt, exactly like Run.started_at and EvidenceRecord.created_at_utc,
#: so including them would make every retry a false conflict and defeat
#: idempotency for the callers who rely on it most. Note the asymmetry
#: with EvidenceRecord.observed_at_utc, which IS part of the evidence
#: fingerprint: that field is never server-defaulted (the API leaves it
#: None when the producer omits it), so it is purely caller-authored,
#: whereas StrategyHealthRecord.observed_at_utc is required by the model
#: and defaulted to utcnow() per attempt by the API. Every other content
#: field participates.
_EXPERIMENT_FP_EXCLUDE = {"experiment_id", "created_at"}
_PROMOTION_FP_EXCLUDE = {"transition_id", "at_utc"}
_HEALTH_FP_EXCLUDE = {"health_id", "observed_at_utc"}

#: artifact_id is the record's own identity (the lookup key, so trivially
#: equal on both sides already). run_id/evidence_id are excluded because
#: attach_artifact -- not re-registration -- is the sanctioned way to set
#: or change them; register_artifact must not treat a differing
#: attachment as "different content".
_ARTIFACT_FP_EXCLUDE = {"artifact_id", "run_id", "evidence_id"}


def _dt_to_str(dt: datetime) -> str:
    """Serialise a tz-aware UTC datetime with full microsecond fidelity.

    Deliberately not ``cer.runtime.clock.isoformat_utc``: that helper
    truncates to millisecond precision for compact log/API output, which
    would silently drop precision on a datetime with non-zero
    sub-millisecond microseconds and break this store's exact round-trip
    fidelity requirement. Contract models guarantee ``dt`` is already
    timezone-aware and normalised to UTC (``cer.contract.models.UTCDateTime``),
    so a plain ``isoformat()`` (offset form, e.g. ``...+00:00``) round-trips
    exactly via ``datetime.fromisoformat`` on read.
    """
    return dt.isoformat()


def _str_to_dt(s: str) -> datetime:
    return to_utc(datetime.fromisoformat(s))


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _loads(s: str) -> Any:
    return json.loads(s)


def _fingerprint(model_dump: dict[str, Any], *, exclude: set[str]) -> str:
    payload = {k: v for k, v in model_dump.items() if k not in exclude}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SQLiteMetadataStore:
    """SQLite-backed :class:`~cer.contract.stores.MetadataStore` implementation."""

    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS) -> None:
        self._db_path = Path(db_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._local = threading.local()

        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # e.g. a misconfigured CER_METADATA_DB_PATH at container
            # startup whose parent path is blocked by a regular file --
            # a live operational path the caller must be able to handle
            # via the CER error hierarchy, not a raw OSError/FileExistsError.
            raise MetadataStoreError(
                f"failed to create metadata store directory {self._db_path.parent}: {exc}"
            ) from exc

        # Bootstrap connection: apply migrations once up front so that
        # every subsequent per-thread connection opens against an
        # already-migrated schema. _expected_schema_version is the
        # version health() checks is still recorded on every call.
        bootstrap = self._open_connection()
        try:
            self._expected_schema_version = apply_migrations(bootstrap, _SCHEMA_DIR)
        except OSError as exc:
            raise MetadataStoreError(f"failed to apply metadata store migrations: {exc}") from exc
        finally:
            bootstrap.close()

    # --- connection management --------------------------------------------

    def _open_connection(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                str(self._db_path),
                timeout=self._busy_timeout_ms / 1000.0,
                isolation_level=None,
                check_same_thread=True,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            conn.row_factory = sqlite3.Row
            return conn
        except (sqlite3.Error, OSError) as exc:
            raise MetadataStoreError(
                f"failed to open metadata store at {self._db_path}: {exc}"
            ) from exc

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open_connection()
            self._local.conn = conn
        return conn

    def _require_durable_backing(self, *, detail: str = "") -> None:
        """Raise ``MetadataStoreError`` unless the database file is still
        present at its configured path.

        This is the single cheap existence check that both the write path
        (:meth:`_transaction`) and :meth:`health` are built on, so the
        two can never disagree about whether the store's durable backing
        is still there.

        Why the write path needs it at all: SQLite keeps writing happily
        into an already-open file descriptor whose inode has been
        unlinked (an unmounted volume, a deleted data directory). Those
        writes commit, are readable back through the same connection, and
        are gone at the next restart -- so without this check CER would
        acknowledge ``201 Created`` for evidence it has already lost,
        which is the exact opposite of being authoritative for empirical
        evidence. One ``stat()`` per write is proportionate at CER's
        scale; nothing here polls, watches or runs in the background.

        This does not (and cannot) defeat POSIX: a path that disappears
        in the microseconds between this check and the ``COMMIT`` is
        still lost. It closes the operationally real case -- backing gone
        for the whole remaining life of the process -- not an
        instantaneous race.
        """
        if not self._db_path.exists():
            message = f"metadata store database file {self._db_path} does not exist"
            if detail:
                message = f"{message}: {detail}"
            raise MetadataStoreError(message)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # Every write in this store goes through this context manager, so
        # this is the one place the durable-backing check has to live for
        # the whole write path -- evidence, runs, promotions, health
        # records and everything else alike.
        self._require_durable_backing(
            detail="refusing to acknowledge a write that could not survive a restart"
        )
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise MetadataStoreError(f"failed to begin transaction: {exc}") from exc
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        """Close this thread's connection, if one is open. Best-effort."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # --- existence guards -------------------------------------------------

    def _require_strategy(self, conn: sqlite3.Connection, strategy_id: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM strategies WHERE strategy_id = ?", (strategy_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"strategy_id {strategy_id!r} does not exist")

    def _require_experiment(self, conn: sqlite3.Connection, experiment_id: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM experiments WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"experiment_id {experiment_id!r} does not exist")

    def _require_run(self, conn: sqlite3.Connection, run_id: str) -> None:
        row = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"run_id {run_id!r} does not exist")

    def _require_evidence(self, conn: sqlite3.Connection, evidence_id: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM evidence WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"evidence_id {evidence_id!r} does not exist")

    # --- Strategy / StrategyVersion ---------------------------------------

    def register_strategy(self, strategy: Strategy) -> Strategy:
        """Register a strategy. Re-registering identical content returns the
        stored record; genuinely different content raises
        ``ImmutabilityError``.

        "Identical" ignores the server-assigned ``created_at`` -- see
        ``_STRATEGY_FP_EXCLUDE``. A producer retrying the same
        registration must not be told its data conflicts with stored
        history merely because the API layer stamped a fresh timestamp on
        the second attempt.
        """
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT name, thesis, created_at FROM strategies WHERE strategy_id = ?",
                (strategy.strategy_id,),
            ).fetchone()
            if row is not None:
                existing = Strategy(
                    strategy_id=strategy.strategy_id,
                    name=row["name"],
                    thesis=row["thesis"],
                    created_at=_str_to_dt(row["created_at"]),
                )
                if _fingerprint(
                    existing.model_dump(mode="json"), exclude=_STRATEGY_FP_EXCLUDE
                ) != _fingerprint(
                    strategy.model_dump(mode="json"), exclude=_STRATEGY_FP_EXCLUDE
                ):
                    raise ImmutabilityError(
                        f"strategy_id {strategy.strategy_id!r} already registered with different content"
                    )
                return existing
            conn.execute(
                "INSERT INTO strategies (strategy_id, name, thesis, created_at) VALUES (?, ?, ?, ?)",
                (strategy.strategy_id, strategy.name, strategy.thesis, _dt_to_str(strategy.created_at)),
            )
            return strategy

    def register_strategy_version(self, sv: StrategyVersion) -> StrategyVersion:
        """Register a strategy version. Same semantics as
        :meth:`register_strategy`: identical content is a no-op returning
        the stored record, different content raises ``ImmutabilityError``,
        and the server-assigned ``created_at`` is excluded from that
        comparison (see ``_STRATEGY_VERSION_FP_EXCLUDE``).
        """
        with self._transaction() as conn:
            self._require_strategy(conn, sv.strategy_id)
            row = conn.execute(
                "SELECT git_repo, git_commit, created_at, notes FROM strategy_versions "
                "WHERE strategy_id = ? AND strategy_version = ?",
                (sv.strategy_id, sv.strategy_version),
            ).fetchone()
            if row is not None:
                existing = StrategyVersion(
                    strategy_id=sv.strategy_id,
                    strategy_version=sv.strategy_version,
                    git_repo=row["git_repo"],
                    git_commit=row["git_commit"],
                    created_at=_str_to_dt(row["created_at"]),
                    notes=row["notes"],
                )
                if _fingerprint(
                    existing.model_dump(mode="json"), exclude=_STRATEGY_VERSION_FP_EXCLUDE
                ) != _fingerprint(
                    sv.model_dump(mode="json"), exclude=_STRATEGY_VERSION_FP_EXCLUDE
                ):
                    raise ImmutabilityError(
                        f"strategy_version {sv.strategy_id!r}/{sv.strategy_version!r} "
                        "already registered with different content"
                    )
                return existing
            conn.execute(
                "INSERT INTO strategy_versions "
                "(strategy_id, strategy_version, git_repo, git_commit, created_at, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sv.strategy_id,
                    sv.strategy_version,
                    sv.git_repo,
                    sv.git_commit,
                    _dt_to_str(sv.created_at),
                    sv.notes,
                ),
            )
            return sv

    # --- Experiment ---------------------------------------------------------

    def _row_to_experiment(self, row: sqlite3.Row) -> Experiment:
        return Experiment(
            experiment_id=row["experiment_id"],
            objective=row["objective"],
            strategy_id=row["strategy_id"],
            strategy_version=row["strategy_version"],
            producer=row["producer"],
            created_at=_str_to_dt(row["created_at"]),
        )

    def create_experiment(
        self, experiment: Experiment, *, idempotency_key: str | None = None
    ) -> Experiment:
        """Create an experiment, idempotently on ``idempotency_key`` when one
        is given.

        The key is optional here (unlike ``append_evidence``, where it is
        required): with no key this behaves exactly as it always has --
        a fresh row per call. With a key it behaves exactly like
        ``create_run``: scoped to ``(producer, idempotency_key)``, an
        identical replay returns the stored experiment, and a materially
        different body under the same producer and key raises
        ``IdempotencyConflictError``.
        """
        with self._transaction() as conn:
            fp = _fingerprint(
                experiment.model_dump(mode="json"), exclude=_EXPERIMENT_FP_EXCLUDE
            )

            if idempotency_key is not None:
                # Scoped to (producer, idempotency_key), not the key alone
                # -- see the module docstring.
                by_key = conn.execute(
                    "SELECT * FROM experiments WHERE producer = ? AND idempotency_key = ?",
                    (experiment.producer, idempotency_key),
                ).fetchone()
                if by_key is not None:
                    if by_key["content_fingerprint"] != fp:
                        raise IdempotencyConflictError(
                            f"idempotency_key {idempotency_key!r} was already used by "
                            f"producer {experiment.producer!r} to create an experiment "
                            "with different content"
                        )
                    return self._row_to_experiment(by_key)

            row = conn.execute(
                "SELECT * FROM experiments WHERE experiment_id = ?",
                (experiment.experiment_id,),
            ).fetchone()
            if row is not None:
                existing = self._row_to_experiment(row)
                # Deliberately the *same* fingerprint (same exclusions,
                # same helper) the idempotency-key path above compares.
                # The two paths must never disagree about whether a given
                # submission is a replay or a conflict -- and created_at,
                # stamped per attempt by the API layer, must not turn an
                # honest retry into an immutability violation.
                if _fingerprint(
                    existing.model_dump(mode="json"), exclude=_EXPERIMENT_FP_EXCLUDE
                ) != fp:
                    raise ImmutabilityError(
                        f"experiment_id {experiment.experiment_id!r} already exists with different content"
                    )
                return existing
            conn.execute(
                "INSERT INTO experiments "
                "(experiment_id, objective, strategy_id, strategy_version, producer, "
                "created_at, idempotency_key, content_fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    experiment.experiment_id,
                    experiment.objective,
                    experiment.strategy_id,
                    experiment.strategy_version,
                    experiment.producer,
                    _dt_to_str(experiment.created_at),
                    idempotency_key,
                    fp,
                ),
            )
            return experiment

    # --- Run ------------------------------------------------------------------

    def _row_to_run(self, row: sqlite3.Row) -> Run:
        return Run(
            run_id=row["run_id"],
            experiment_id=row["experiment_id"],
            status=RunStatus(row["status"]),
            started_at=_str_to_dt(row["started_at"]),
            ended_at=_str_to_dt(row["ended_at"]) if row["ended_at"] is not None else None,
            producer=row["producer"],
            producer_version=row["producer_version"],
            git_repo=row["git_repo"],
            git_commit=row["git_commit"],
            dataset_id=row["dataset_id"],
            dataset_version=row["dataset_version"],
            dataset_ref=row["dataset_ref"],
            config_hash=row["config_hash"],
            config_ref=row["config_ref"],
            environment=row["environment"],
        )

    def _fetch_run(self, conn: sqlite3.Connection, run_id: str) -> Run:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"run_id {run_id!r} does not exist")
        return self._row_to_run(row)

    def create_run(self, run: Run, *, idempotency_key: str | None = None) -> Run:
        with self._transaction() as conn:
            self._require_experiment(conn, run.experiment_id)

            fp = _fingerprint(run.model_dump(mode="json"), exclude=_RUN_FP_EXCLUDE)

            if idempotency_key is not None:
                # Scoped to (producer, idempotency_key), not the key alone
                # -- two different producers may safely reuse the same key
                # string. See the module docstring.
                row = conn.execute(
                    "SELECT run_id, content_fingerprint FROM runs "
                    "WHERE producer = ? AND idempotency_key = ?",
                    (run.producer, idempotency_key),
                ).fetchone()
                if row is not None:
                    if row["content_fingerprint"] != fp:
                        raise IdempotencyConflictError(
                            f"idempotency_key {idempotency_key!r} was already used by "
                            f"producer {run.producer!r} to create a run with different content"
                        )
                    return self._fetch_run(conn, row["run_id"])

            existing = conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run.run_id,)
            ).fetchone()
            if existing is not None:
                raise ImmutabilityError(f"run_id {run.run_id!r} already exists")

            conn.execute(
                """
                INSERT INTO runs (
                    run_id, experiment_id, status, started_at, ended_at, producer,
                    producer_version, git_repo, git_commit, dataset_id, dataset_version,
                    dataset_ref, config_hash, config_ref, environment,
                    provenance_completeness, missing_provenance_json,
                    idempotency_key, content_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.experiment_id,
                    run.status.value,
                    _dt_to_str(run.started_at),
                    _dt_to_str(run.ended_at) if run.ended_at is not None else None,
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
                    run.provenance_completeness.value,
                    _dumps(run.missing_provenance),
                    idempotency_key,
                    fp,
                ),
            )
            return run

    def close_run(self, run_id: str, *, status: RunStatus, ended_at: datetime) -> Run:
        ended_at = to_utc(ended_at)
        with self._transaction() as conn:
            row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"run_id {run_id!r} does not exist")
            if row["status"] != RunStatus.OPEN.value:
                raise ImmutabilityError(
                    f"run_id {run_id!r} is already {row['status']!r}; a CLOSED/FAILED "
                    "run cannot be re-closed"
                )
            conn.execute(
                "UPDATE runs SET status = ?, ended_at = ? WHERE run_id = ?",
                (RunStatus(status).value, _dt_to_str(ended_at), run_id),
            )
            return self._fetch_run(conn, run_id)

    # --- EvidenceRecord ---------------------------------------------------------

    def _row_to_evidence(self, row: sqlite3.Row) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=row["evidence_id"],
            idempotency_key=row["idempotency_key"],
            evidence_type=row["evidence_type"],
            schema_version=row["schema_version"],
            strategy_id=row["strategy_id"],
            strategy_version=row["strategy_version"],
            experiment_id=row["experiment_id"],
            run_id=row["run_id"],
            producer=row["producer"],
            producer_version=row["producer_version"],
            created_at_utc=_str_to_dt(row["created_at_utc"]),
            observed_at_utc=_str_to_dt(row["observed_at_utc"]) if row["observed_at_utc"] is not None else None,
            git_repo=row["git_repo"],
            git_commit=row["git_commit"],
            dataset_id=row["dataset_id"],
            dataset_version=row["dataset_version"],
            dataset_ref=row["dataset_ref"],
            config_hash=row["config_hash"],
            config_ref=row["config_ref"],
            environment=row["environment"],
            instruments=_loads(row["instruments_json"]),
            timeframes=_loads(row["timeframes_json"]),
            metrics=_loads(row["metrics_json"]),
            verdict=row["verdict"],
            status=row["status"],
            regime_tags=_loads(row["regime_tags_json"]),
            parent_evidence_ids=_loads(row["parent_evidence_ids_json"]),
            parent_run_ids=_loads(row["parent_run_ids_json"]),
            artifact_ids=_loads(row["artifact_ids_json"]),
            notes=_loads(row["notes_json"]),
        )

    def _fetch_evidence(self, conn: sqlite3.Connection, evidence_id: str) -> EvidenceRecord:
        row = conn.execute("SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"evidence_id {evidence_id!r} does not exist")
        return self._row_to_evidence(row)

    def append_evidence(self, evidence: EvidenceRecord) -> EvidenceRecord:
        with self._transaction() as conn:
            if evidence.run_id is not None:
                self._require_run(conn, evidence.run_id)
            if evidence.experiment_id is not None:
                self._require_experiment(conn, evidence.experiment_id)

            fp = _fingerprint(evidence.model_dump(mode="json"), exclude=_EVIDENCE_FP_EXCLUDE)

            # Scoped to (producer, idempotency_key), not the key alone --
            # two different producers may safely reuse the same key
            # string. See the module docstring.
            by_key = conn.execute(
                "SELECT evidence_id, content_fingerprint FROM evidence "
                "WHERE producer = ? AND idempotency_key = ?",
                (evidence.producer, evidence.idempotency_key),
            ).fetchone()
            if by_key is not None:
                if by_key["content_fingerprint"] != fp:
                    raise IdempotencyConflictError(
                        f"idempotency_key {evidence.idempotency_key!r} was already used by "
                        f"producer {evidence.producer!r} to append evidence with different content"
                    )
                return self._fetch_evidence(conn, by_key["evidence_id"])

            by_id = conn.execute(
                "SELECT content_fingerprint FROM evidence WHERE evidence_id = ?",
                (evidence.evidence_id,),
            ).fetchone()
            if by_id is not None:
                if by_id["content_fingerprint"] != fp:
                    raise ImmutabilityError(
                        f"evidence_id {evidence.evidence_id!r} already exists with different content"
                    )
                return self._fetch_evidence(conn, evidence.evidence_id)

            conn.execute(
                """
                INSERT INTO evidence (
                    evidence_id, idempotency_key, evidence_type, schema_version,
                    strategy_id, strategy_version, experiment_id, run_id,
                    producer, producer_version, created_at_utc, observed_at_utc,
                    git_repo, git_commit, dataset_id, dataset_version, dataset_ref,
                    config_hash, config_ref, environment,
                    instruments_json, timeframes_json, metrics_json, verdict, status,
                    regime_tags_json, parent_evidence_ids_json, parent_run_ids_json,
                    artifact_ids_json, notes_json, provenance_completeness,
                    missing_provenance_json, content_fingerprint
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    evidence.evidence_id,
                    evidence.idempotency_key,
                    evidence.evidence_type.value,
                    evidence.schema_version,
                    evidence.strategy_id,
                    evidence.strategy_version,
                    evidence.experiment_id,
                    evidence.run_id,
                    evidence.producer,
                    evidence.producer_version,
                    _dt_to_str(evidence.created_at_utc),
                    _dt_to_str(evidence.observed_at_utc) if evidence.observed_at_utc is not None else None,
                    evidence.git_repo,
                    evidence.git_commit,
                    evidence.dataset_id,
                    evidence.dataset_version,
                    evidence.dataset_ref,
                    evidence.config_hash,
                    evidence.config_ref,
                    evidence.environment,
                    _dumps(evidence.instruments),
                    _dumps(evidence.timeframes),
                    _dumps(evidence.metrics),
                    evidence.verdict,
                    evidence.status,
                    _dumps(evidence.regime_tags),
                    _dumps(evidence.parent_evidence_ids),
                    _dumps(evidence.parent_run_ids),
                    _dumps(evidence.artifact_ids),
                    _dumps(evidence.notes),
                    evidence.provenance_completeness.value,
                    _dumps(evidence.missing_provenance),
                    fp,
                ),
            )
            return evidence

    def get_evidence(self, evidence_id: str) -> EvidenceRecord:
        row = self._conn.execute(
            "SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"evidence_id {evidence_id!r} does not exist")
        return self._row_to_evidence(row)

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
        clauses: list[str] = []
        params: list[Any] = []
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        if strategy_version is not None:
            clauses.append("strategy_version = ?")
            params.append(strategy_version)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if experiment_id is not None:
            clauses.append("experiment_id = ?")
            params.append(experiment_id)
        if evidence_type is not None:
            clauses.append("evidence_type = ?")
            params.append(EvidenceType(evidence_type).value)
        if since is not None:
            clauses.append("created_at_utc >= ?")
            params.append(_dt_to_str(to_utc(since)))
        if until is not None:
            clauses.append("created_at_utc <= ?")
            params.append(_dt_to_str(to_utc(until)))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM evidence {where} "
            "ORDER BY created_at_utc DESC, evidence_id DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_evidence(r) for r in rows]

    # --- ArtifactRecord ---------------------------------------------------------

    def _row_to_artifact(self, row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=row["artifact_id"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            content_type=row["content_type"],
            filename=row["filename"],
            uri=row["uri"],
            registered_at=_str_to_dt(row["registered_at"]),
            run_id=row["run_id"],
            evidence_id=row["evidence_id"],
        )

    def register_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        with self._transaction() as conn:
            if artifact.run_id is not None:
                self._require_run(conn, artifact.run_id)
            if artifact.evidence_id is not None:
                self._require_evidence(conn, artifact.evidence_id)

            fp = _fingerprint(artifact.model_dump(mode="json"), exclude=_ARTIFACT_FP_EXCLUDE)

            existing = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact.artifact_id,)
            ).fetchone()
            if existing is not None:
                if existing["content_fingerprint"] != fp:
                    raise ImmutabilityError(
                        f"artifact_id {artifact.artifact_id!r} already registered with different content"
                    )
                return self._row_to_artifact(existing)

            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, sha256, size_bytes, content_type, filename, uri,
                    registered_at, run_id, evidence_id, content_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.sha256,
                    artifact.size_bytes,
                    artifact.content_type,
                    artifact.filename,
                    artifact.uri,
                    _dt_to_str(artifact.registered_at),
                    artifact.run_id,
                    artifact.evidence_id,
                    fp,
                ),
            )
            return artifact

    def attach_artifact(
        self,
        artifact_id: str,
        *,
        evidence_id: str | None = None,
        run_id: str | None = None,
    ) -> ArtifactRecord:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"artifact_id {artifact_id!r} does not exist")

            if evidence_id is not None:
                self._require_evidence(conn, evidence_id)
            if run_id is not None:
                self._require_run(conn, run_id)

            new_evidence_id = row["evidence_id"]
            if evidence_id is not None:
                if row["evidence_id"] is not None and row["evidence_id"] != evidence_id:
                    raise ImmutabilityError(
                        f"artifact_id {artifact_id!r} is already attached to evidence_id "
                        f"{row['evidence_id']!r}; cannot relink to {evidence_id!r}"
                    )
                new_evidence_id = evidence_id

            new_run_id = row["run_id"]
            if run_id is not None:
                if row["run_id"] is not None and row["run_id"] != run_id:
                    raise ImmutabilityError(
                        f"artifact_id {artifact_id!r} is already attached to run_id "
                        f"{row['run_id']!r}; cannot relink to {run_id!r}"
                    )
                new_run_id = run_id

            if new_evidence_id != row["evidence_id"] or new_run_id != row["run_id"]:
                conn.execute(
                    "UPDATE artifacts SET evidence_id = ?, run_id = ? WHERE artifact_id = ?",
                    (new_evidence_id, new_run_id, artifact_id),
                )
                row = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
                ).fetchone()

            return self._row_to_artifact(row)

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        row = self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"artifact_id {artifact_id!r} does not exist")
        return self._row_to_artifact(row)

    def query_artifacts(
        self,
        *,
        evidence_id: Optional[str] = None,
        run_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ArtifactRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if evidence_id is not None:
            clauses.append("evidence_id = ?")
            params.append(evidence_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM artifacts {where} "
            "ORDER BY registered_at DESC, artifact_id DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_artifact(r) for r in rows]

    # --- PromotionTransition ---------------------------------------------------

    def _row_to_promotion(self, row: sqlite3.Row) -> PromotionTransition:
        return PromotionTransition(
            transition_id=row["transition_id"],
            strategy_id=row["strategy_id"],
            strategy_version=row["strategy_version"],
            from_state=PromotionState(row["from_state"]),
            to_state=PromotionState(row["to_state"]),
            at_utc=_str_to_dt(row["at_utc"]),
            authority=row["authority"],
            producer=row["producer"],
            evidence_ids=_loads(row["evidence_ids_json"]),
            reason=row["reason"],
        )

    def record_promotion(
        self, transition: PromotionTransition, *, idempotency_key: str | None = None
    ) -> PromotionTransition:
        """Record a promotion transition, idempotently on ``idempotency_key``
        when one is given.

        The key is optional: with no key this behaves exactly as it always
        has. With a key it is scoped to the transition's own ``producer``
        (the record carries it; there is no separate producer argument),
        an identical replay returns the stored transition, and a
        materially different body under the same producer and key raises
        ``IdempotencyConflictError``. Promotion history is a first-class
        PID deliverable, so one intended transition recorded three times
        by a retrying producer is corruption, not a harmless duplicate.
        """
        with self._transaction() as conn:
            fp = _fingerprint(
                transition.model_dump(mode="json"), exclude=_PROMOTION_FP_EXCLUDE
            )

            if idempotency_key is not None:
                by_key = conn.execute(
                    "SELECT * FROM promotions WHERE producer = ? AND idempotency_key = ?",
                    (transition.producer, idempotency_key),
                ).fetchone()
                if by_key is not None:
                    if by_key["content_fingerprint"] != fp:
                        raise IdempotencyConflictError(
                            f"idempotency_key {idempotency_key!r} was already used by "
                            f"producer {transition.producer!r} to record a promotion "
                            "transition with different content"
                        )
                    return self._row_to_promotion(by_key)

            row = conn.execute(
                "SELECT * FROM promotions WHERE transition_id = ?", (transition.transition_id,)
            ).fetchone()
            if row is not None:
                existing = self._row_to_promotion(row)
                if existing.model_dump(mode="json") != transition.model_dump(mode="json"):
                    raise ImmutabilityError(
                        f"transition_id {transition.transition_id!r} already recorded with different content"
                    )
                return existing
            conn.execute(
                """
                INSERT INTO promotions (
                    transition_id, strategy_id, strategy_version, from_state, to_state,
                    at_utc, authority, producer, evidence_ids_json, reason,
                    idempotency_key, content_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition.transition_id,
                    transition.strategy_id,
                    transition.strategy_version,
                    transition.from_state.value,
                    transition.to_state.value,
                    _dt_to_str(transition.at_utc),
                    transition.authority,
                    transition.producer,
                    _dumps(transition.evidence_ids),
                    transition.reason,
                    idempotency_key,
                    fp,
                ),
            )
            return transition

    def query_promotions(
        self,
        *,
        strategy_id: Optional[str] = None,
        strategy_version: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PromotionTransition]:
        clauses: list[str] = []
        params: list[Any] = []
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        if strategy_version is not None:
            clauses.append("strategy_version = ?")
            params.append(strategy_version)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM promotions {where} "
            "ORDER BY at_utc DESC, transition_id DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_promotion(r) for r in rows]

    # --- StrategyHealthRecord ---------------------------------------------------

    def _row_to_health(self, row: sqlite3.Row) -> StrategyHealthRecord:
        return StrategyHealthRecord(
            health_id=row["health_id"],
            strategy_id=row["strategy_id"],
            strategy_version=row["strategy_version"],
            health_state=HealthState(row["health_state"]),
            observed_at_utc=_str_to_dt(row["observed_at_utc"]),
            producer=row["producer"],
            observed_trigger_rate=row["observed_trigger_rate"],
            regime_conditioned_expected_trigger_rate=row["regime_conditioned_expected_trigger_rate"],
            expectancy_r=row["expectancy_r"],
            win_rate=row["win_rate"],
            drawdown=row["drawdown"],
            mae=row["mae"],
            mfe=row["mfe"],
            holding_time=row["holding_time"],
            execution_slippage_quality=row["execution_slippage_quality"],
            regime_distribution=_loads(row["regime_distribution_json"])
            if row["regime_distribution_json"] is not None
            else None,
            strategy_chain_strength_distribution=_loads(row["strategy_chain_strength_distribution_json"])
            if row["strategy_chain_strength_distribution_json"] is not None
            else None,
            baseline_comparison=_loads(row["baseline_comparison_json"])
            if row["baseline_comparison_json"] is not None
            else None,
            confidence=row["confidence"],
            reason=row["reason"],
            evidence_ids=_loads(row["evidence_ids_json"]),
        )

    def record_health(
        self, record: StrategyHealthRecord, *, idempotency_key: str | None = None
    ) -> StrategyHealthRecord:
        """Record a strategy-health observation, idempotently on
        ``idempotency_key`` when one is given.

        The key is optional: with no key this behaves exactly as it always
        has. With a key it is scoped to the record's own ``producer``,
        an identical replay returns the stored record, and a materially
        different body under the same producer and key raises
        ``IdempotencyConflictError``.
        """
        with self._transaction() as conn:
            fp = _fingerprint(record.model_dump(mode="json"), exclude=_HEALTH_FP_EXCLUDE)

            if idempotency_key is not None:
                by_key = conn.execute(
                    "SELECT * FROM health_records WHERE producer = ? AND idempotency_key = ?",
                    (record.producer, idempotency_key),
                ).fetchone()
                if by_key is not None:
                    if by_key["content_fingerprint"] != fp:
                        raise IdempotencyConflictError(
                            f"idempotency_key {idempotency_key!r} was already used by "
                            f"producer {record.producer!r} to record a health record "
                            "with different content"
                        )
                    return self._row_to_health(by_key)

            row = conn.execute(
                "SELECT * FROM health_records WHERE health_id = ?", (record.health_id,)
            ).fetchone()
            if row is not None:
                existing = self._row_to_health(row)
                if existing.model_dump(mode="json") != record.model_dump(mode="json"):
                    raise ImmutabilityError(
                        f"health_id {record.health_id!r} already recorded with different content"
                    )
                return existing
            conn.execute(
                """
                INSERT INTO health_records (
                    health_id, strategy_id, strategy_version, health_state, observed_at_utc,
                    producer, observed_trigger_rate, regime_conditioned_expected_trigger_rate,
                    expectancy_r, win_rate, drawdown, mae, mfe, holding_time,
                    execution_slippage_quality, regime_distribution_json,
                    strategy_chain_strength_distribution_json, baseline_comparison_json,
                    confidence, reason, evidence_ids_json,
                    idempotency_key, content_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.health_id,
                    record.strategy_id,
                    record.strategy_version,
                    record.health_state.value,
                    _dt_to_str(record.observed_at_utc),
                    record.producer,
                    record.observed_trigger_rate,
                    record.regime_conditioned_expected_trigger_rate,
                    record.expectancy_r,
                    record.win_rate,
                    record.drawdown,
                    record.mae,
                    record.mfe,
                    record.holding_time,
                    record.execution_slippage_quality,
                    _dumps(record.regime_distribution) if record.regime_distribution is not None else None,
                    _dumps(record.strategy_chain_strength_distribution)
                    if record.strategy_chain_strength_distribution is not None
                    else None,
                    _dumps(record.baseline_comparison) if record.baseline_comparison is not None else None,
                    record.confidence,
                    record.reason,
                    _dumps(record.evidence_ids),
                    idempotency_key,
                    fp,
                ),
            )
            return record

    def query_health(
        self,
        *,
        strategy_id: Optional[str] = None,
        strategy_version: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[StrategyHealthRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        if strategy_version is not None:
            clauses.append("strategy_version = ?")
            params.append(strategy_version)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM health_records {where} "
            "ORDER BY observed_at_utc DESC, health_id DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_health(r) for r in rows]

    # --- Health -----------------------------------------------------------------

    def health(self) -> None:
        """Raise ``MetadataStoreError`` if the backend is unusable; else return None.

        Deliberately does *not* reuse this thread's long-lived cached
        connection (``self._conn``). A process whose database file's
        directory disappeared out from under it (an unmounted volume, a
        deleted data dir) keeps a live, still-successfully-queryable file
        descriptor to the now-unlinked inode -- a check built on that
        connection alone would report healthy forever, which is exactly
        the bug this method used to have. Every check below is against
        the *current* state of the path on disk, via a fresh connection:

        1. the configured database file must still exist at its path
           (:meth:`_require_durable_backing`, the same check the write
           path runs before acknowledging anything -- readiness and the
           write path must never disagree about whether this store's
           durable backing is still there);
        2. a fresh connection must open it and run a real round-trip read;
        3. ``schema_migrations`` must still record the exact schema
           version this store applied at construction -- a present-but-
           empty (or reset/truncated) file is not a healthy store;
        4. a real write probe (``BEGIN IMMEDIATE`` then ``ROLLBACK``) must
           succeed, so a read-only filesystem or a locked database is
           caught rather than reported healthy.

        Never creates, repairs, or re-migrates anything. In particular,
        the file-existence check happens *before* any connection is
        attempted: an ordinary ``sqlite3.connect()`` against a missing
        path silently creates an empty database file, which is exactly
        the false-healthy side effect this check exists to prevent.
        """
        try:
            self._require_durable_backing()
        except MetadataStoreError as exc:
            raise MetadataStoreError(
                f"metadata store health check failed: {exc}"
            ) from exc

        conn: sqlite3.Connection | None = None
        try:
            conn = self._open_connection()

            row = conn.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchone()
            if row is None:
                raise MetadataStoreError(
                    "metadata store health check failed: schema_migrations table is "
                    "empty (schema not applied, or reset out from under a live store)"
                )

            if self._expected_schema_version is not None:
                version_row = conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?",
                    (self._expected_schema_version,),
                ).fetchone()
                if version_row is None:
                    raise MetadataStoreError(
                        "metadata store health check failed: schema_migrations does not "
                        f"record the expected applied version {self._expected_schema_version}"
                    )

            # Writability probe: a read-only filesystem or a locked
            # database must be caught here, not reported healthy.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            raise MetadataStoreError(f"metadata store health check failed: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()
