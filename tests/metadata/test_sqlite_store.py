"""Tests for cer.metadata.sqlite_store.SQLiteMetadataStore against the
MetadataStore protocol contract: round-trip fidelity, restart persistence,
idempotency (including concurrent replay), immutability, referential
integrity, query filters/pagination, and health().
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cer.contract.enums import EvidenceType, HealthState, PromotionState, RunStatus
from cer.contract.errors import (
    ImmutabilityError,
    IdempotencyConflictError,
    MetadataStoreError,
    NotFoundError,
)
from cer.contract.identity import new_artifact_id, new_evidence_id, new_experiment_id, new_run_id
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
from cer.metadata import SQLiteMetadataStore

UTC = timezone.utc


# --- Model builders -----------------------------------------------------------
#
# _now() returns a fixed base instant with non-zero, non-millisecond-aligned
# microseconds (123457) specifically so round-trip tests exercise full
# microsecond precision, not just whole-millisecond timestamps.


def _now(offset_seconds: float = 0.0) -> datetime:
    base = datetime(2026, 1, 1, 12, 0, 0, 123457, tzinfo=UTC)
    return base + timedelta(seconds=offset_seconds)


def make_strategy(**overrides) -> Strategy:
    data = dict(
        strategy_id="EMA_PULLBACK",
        name="EMA Pullback",
        thesis="Buy pullbacks to the 21 EMA in an uptrend.",
        created_at=_now(),
    )
    data.update(overrides)
    return Strategy(**data)


def make_strategy_version(**overrides) -> StrategyVersion:
    data = dict(
        strategy_id="EMA_PULLBACK",
        strategy_version="v1.0.0",
        git_repo="git@example.com:org/strategies.git",
        git_commit="a" * 40,
        created_at=_now(),
        notes=None,
    )
    data.update(overrides)
    return StrategyVersion(**data)


def make_experiment(**overrides) -> Experiment:
    data = dict(
        experiment_id=new_experiment_id(),
        objective="Validate EMA pullback on EURUSD H1",
        strategy_id="EMA_PULLBACK",
        strategy_version="v1.0.0",
        producer="HSA",
        created_at=_now(),
    )
    data.update(overrides)
    return Experiment(**data)


def make_run(experiment_id: str, **overrides) -> Run:
    data = dict(
        run_id=new_run_id(),
        experiment_id=experiment_id,
        status=RunStatus.OPEN,
        started_at=_now(),
        producer="HSA",
        producer_version="1.0.0",
        git_repo="git@example.com:org/strategies.git",
        git_commit="a" * 40,
        dataset_id="ds1",
        dataset_version="1",
        dataset_ref="s3://bucket/ds1",
        config_hash="cfg1",
        config_ref="s3://bucket/cfg1",
        environment="dev",
    )
    data.update(overrides)
    return Run(**data)


def make_evidence(*, idempotency_key: str, run_id=None, experiment_id=None, **overrides) -> EvidenceRecord:
    data = dict(
        evidence_id=new_evidence_id(),
        idempotency_key=idempotency_key,
        evidence_type=EvidenceType.BACKTEST,
        schema_version=1,
        strategy_id="EMA_PULLBACK",
        strategy_version="v1.0.0",
        experiment_id=experiment_id,
        run_id=run_id,
        producer="HSA",
        producer_version="1.0.0",
        created_at_utc=_now(),
        observed_at_utc=_now(),
        git_repo="git@example.com:org/strategies.git",
        git_commit="a" * 40,
        dataset_id="ds1",
        dataset_version="1",
        dataset_ref="s3://bucket/ds1",
        config_hash="cfg1",
        config_ref="s3://bucket/cfg1",
        environment="dev",
        instruments=["EURUSD", "GBPUSD"],
        timeframes=["H1", "H4"],
        metrics={"sharpe": 1.23, "trades": 42, "win": True, "label": "ok"},
        verdict="PASS",
        status="COMPLETE",
        regime_tags=["trend", "low_vol"],
        parent_evidence_ids=[],
        parent_run_ids=[],
        artifact_ids=[],
        notes={"observation": "clean run", "count": 3},
    )
    data.update(overrides)
    return EvidenceRecord(**data)


def make_artifact(**overrides) -> ArtifactRecord:
    data = dict(
        artifact_id=new_artifact_id(),
        sha256="b" * 64,
        size_bytes=1024,
        content_type="application/json",
        filename="report.json",
        uri="file:///artifacts/report.json",
        registered_at=_now(),
        run_id=None,
        evidence_id=None,
    )
    data.update(overrides)
    return ArtifactRecord(**data)


def make_promotion(**overrides) -> PromotionTransition:
    data = dict(
        strategy_id="EMA_PULLBACK",
        strategy_version="v1.0.0",
        from_state=PromotionState.DRAFT,
        to_state=PromotionState.IMPLEMENTED,
        at_utc=_now(),
        authority="PL",
        producer="HSA",
        evidence_ids=[new_evidence_id()],
        reason="Initial implementation complete.",
    )
    data.update(overrides)
    return PromotionTransition(**data)


def make_health(**overrides) -> StrategyHealthRecord:
    data = dict(
        strategy_id="EMA_PULLBACK",
        strategy_version="v1.0.0",
        health_state=HealthState.HEALTHY,
        observed_at_utc=_now(),
        producer="NEO",
        observed_trigger_rate=0.12,
        regime_conditioned_expected_trigger_rate=0.10,
        expectancy_r=0.3,
        win_rate=0.55,
        drawdown=0.08,
        mae=0.02,
        mfe=0.05,
        holding_time=3600.0,
        execution_slippage_quality=0.99,
        regime_distribution={"trend": 0.6, "range": 0.4},
        strategy_chain_strength_distribution={"a": 0.5},
        baseline_comparison={"historical_sharpe": 1.1},
        confidence=0.8,
        reason="Consistent with historical baseline.",
        evidence_ids=[new_evidence_id()],
    )
    data.update(overrides)
    return StrategyHealthRecord(**data)


# --- Fixtures -------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "cer.db"


@pytest.fixture
def store(db_path) -> SQLiteMetadataStore:
    s = SQLiteMetadataStore(db_path)
    yield s
    s.close()


def _seeded_run(store: SQLiteMetadataStore, **run_overrides) -> tuple[Experiment, Run]:
    store.register_strategy(make_strategy())
    store.register_strategy_version(make_strategy_version())
    experiment = store.create_experiment(make_experiment())
    run = store.create_run(make_run(experiment.experiment_id, **run_overrides))
    return experiment, run


# --- Construction / migrations wiring -------------------------------------------


def test_construction_creates_db_file_and_parent_dirs(tmp_path):
    nested = tmp_path / "a" / "b" / "cer.db"
    assert not nested.parent.exists()
    s = SQLiteMetadataStore(nested)
    try:
        assert nested.exists()
        s.health()
    finally:
        s.close()


# --- Strategy / StrategyVersion ---------------------------------------------------


def test_register_strategy_roundtrip(store):
    strategy = make_strategy()
    result = store.register_strategy(strategy)
    assert result == strategy

    # Idempotent replay: identical content, same strategy_id -> same record.
    replay = store.register_strategy(make_strategy())
    assert replay == strategy


def test_register_strategy_conflict_raises_immutability_error(store):
    store.register_strategy(make_strategy())
    with pytest.raises(ImmutabilityError):
        store.register_strategy(make_strategy(name="Different Name"))


def test_register_strategy_version_requires_existing_strategy(store):
    with pytest.raises(NotFoundError):
        store.register_strategy_version(make_strategy_version())


def test_register_strategy_version_roundtrip_and_conflict(store):
    store.register_strategy(make_strategy())
    sv = store.register_strategy_version(make_strategy_version())
    assert sv == make_strategy_version()

    replay = store.register_strategy_version(make_strategy_version())
    assert replay == sv

    with pytest.raises(ImmutabilityError):
        store.register_strategy_version(make_strategy_version(git_commit="c" * 40))


# --- Experiment ---------------------------------------------------------------------


def test_create_experiment_roundtrip_and_conflict(store):
    experiment = make_experiment()
    result = store.create_experiment(experiment)
    assert result == experiment

    replay = store.create_experiment(experiment.model_copy())
    assert replay == experiment

    with pytest.raises(ImmutabilityError):
        store.create_experiment(experiment.model_copy(update={"objective": "Different objective"}))


# --- Run: create/idempotency/close/immutability -------------------------------------


def test_create_run_requires_existing_experiment(store):
    with pytest.raises(NotFoundError):
        store.create_run(make_run("exp_" + "0" * 32))


def test_create_run_roundtrip(store):
    experiment = store.create_experiment(make_experiment())
    run = make_run(experiment.experiment_id)
    result = store.create_run(run)
    assert result == run


def test_create_run_idempotent_replay_returns_existing_record(store):
    experiment = store.create_experiment(make_experiment())
    key = "run-idem-1"
    first = store.create_run(make_run(experiment.experiment_id), idempotency_key=key)

    # A retry that minted a *different* run_id but identical other content
    # must be recognised as the same logical submission and return the
    # original record, not create a second row.
    second = store.create_run(make_run(experiment.experiment_id), idempotency_key=key)
    assert second == first
    assert second.run_id == first.run_id

    conn = sqlite3.connect(str(store._db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM runs WHERE idempotency_key = ?", (key,)).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_create_run_idempotency_conflict_raises(store):
    experiment = store.create_experiment(make_experiment())
    key = "run-idem-2"
    store.create_run(make_run(experiment.experiment_id), idempotency_key=key)

    # Same producer, same key, materially different content -> conflict.
    with pytest.raises(IdempotencyConflictError):
        store.create_run(make_run(experiment.experiment_id, environment="prod"), idempotency_key=key)


def test_create_run_idempotency_key_is_scoped_per_producer(store):
    """Regression test: idempotency keys must be scoped to (producer, key),
    not the key alone. Two unrelated producers choosing the same natural
    key string must not collide -- each gets its own independently
    retrievable run, not an IdempotencyConflictError against a run it has
    nothing to do with.
    """
    experiment = store.create_experiment(make_experiment())
    shared_key = "2026-09-04-btc-sweep"

    hsa_run = store.create_run(
        make_run(experiment.experiment_id, producer="HSA", dataset_id="hsa-ds"),
        idempotency_key=shared_key,
    )
    neo_run = store.create_run(
        make_run(experiment.experiment_id, producer="NEO", dataset_id="neo-ds"),
        idempotency_key=shared_key,
    )

    assert hsa_run.run_id != neo_run.run_id
    assert hsa_run.producer == "HSA"
    assert neo_run.producer == "NEO"

    conn = sqlite3.connect(str(store._db_path))
    try:
        rows = conn.execute(
            "SELECT run_id, producer FROM runs WHERE idempotency_key = ? ORDER BY producer",
            (shared_key,),
        ).fetchall()
    finally:
        conn.close()
    assert [(r[1]) for r in rows] == ["HSA", "NEO"]

    # Each producer's own retry of its own key is still idempotent.
    hsa_replay = store.create_run(
        make_run(experiment.experiment_id, producer="HSA", dataset_id="hsa-ds"),
        idempotency_key=shared_key,
    )
    assert hsa_replay.run_id == hsa_run.run_id


def test_create_run_replay_with_differing_started_at_is_not_a_conflict(store):
    """Regression test: started_at is a server-assignable record-creation
    timestamp -- when a caller omits it, the API layer fills it with
    wall-clock time on every attempt, so a byte-identical HTTP retry has a
    different started_at on each try. That must still be recognised as a
    replay, not rejected as a conflict.
    """
    experiment = store.create_experiment(make_experiment())
    key = "run-retry-varying-started-at"

    first = store.create_run(
        make_run(experiment.experiment_id, started_at=_now(offset_seconds=1)),
        idempotency_key=key,
    )
    second = store.create_run(
        make_run(experiment.experiment_id, started_at=_now(offset_seconds=99)),
        idempotency_key=key,
    )

    assert second.run_id == first.run_id
    assert second == first  # returns the stored record, including its original started_at

    conn = sqlite3.connect(str(store._db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM runs WHERE idempotency_key = ?", (key,)).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_create_run_concurrent_replay_creates_exactly_one_row(store):
    experiment = store.create_experiment(make_experiment())
    key = "run-idem-concurrent"
    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results: list[Run] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            r = store.create_run(make_run(experiment.experiment_id), idempotency_key=key)
            with lock:
                results.append(r)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == n_threads
    assert len({r.run_id for r in results}) == 1

    conn = sqlite3.connect(str(store._db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM runs WHERE idempotency_key = ?", (key,)).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_close_run_success(store):
    experiment = store.create_experiment(make_experiment())
    run = store.create_run(make_run(experiment.experiment_id))
    ended = _now(offset_seconds=60)

    closed = store.close_run(run.run_id, status=RunStatus.CLOSED, ended_at=ended)
    assert closed.status == RunStatus.CLOSED
    assert closed.ended_at == ended
    assert closed.run_id == run.run_id
    assert closed.experiment_id == run.experiment_id


def test_close_run_unknown_raises_not_found(store):
    with pytest.raises(NotFoundError):
        store.close_run("run_" + "0" * 32, status=RunStatus.CLOSED, ended_at=_now())


def test_close_run_twice_raises_immutability_error(store):
    experiment = store.create_experiment(make_experiment())
    run = store.create_run(make_run(experiment.experiment_id))
    store.close_run(run.run_id, status=RunStatus.CLOSED, ended_at=_now(offset_seconds=1))

    with pytest.raises(ImmutabilityError):
        store.close_run(run.run_id, status=RunStatus.CLOSED, ended_at=_now(offset_seconds=1))

    with pytest.raises(ImmutabilityError):
        store.close_run(run.run_id, status=RunStatus.FAILED, ended_at=_now(offset_seconds=2))


# --- EvidenceRecord ----------------------------------------------------------------


def test_append_evidence_requires_existing_run_and_experiment(store):
    with pytest.raises(NotFoundError):
        store.append_evidence(make_evidence(idempotency_key="e1", run_id="run_" + "0" * 32))

    with pytest.raises(NotFoundError):
        store.append_evidence(make_evidence(idempotency_key="e2", experiment_id="exp_" + "0" * 32))


def test_append_evidence_full_roundtrip_fidelity(store):
    experiment, run = _seeded_run(store)
    evidence = make_evidence(
        idempotency_key="ev-roundtrip",
        run_id=run.run_id,
        experiment_id=experiment.experiment_id,
    )
    written = store.append_evidence(evidence)
    assert written == evidence

    fetched = store.get_evidence(evidence.evidence_id)
    assert fetched == evidence
    # Explicitly re-check the list/dict fields and UTC datetimes that a
    # naive implementation could easily mangle in JSON/round-trip.
    assert fetched.instruments == ["EURUSD", "GBPUSD"]
    assert fetched.timeframes == ["H1", "H4"]
    assert fetched.metrics == {"sharpe": 1.23, "trades": 42, "win": True, "label": "ok"}
    assert fetched.regime_tags == ["trend", "low_vol"]
    assert fetched.notes == {"observation": "clean run", "count": 3}
    assert fetched.created_at_utc == evidence.created_at_utc
    assert fetched.created_at_utc.microsecond == 123457
    assert fetched.created_at_utc.tzinfo is not None


def test_get_evidence_unknown_raises_not_found(store):
    with pytest.raises(NotFoundError):
        store.get_evidence("ev_" + "0" * 32)


def test_append_evidence_idempotent_replay_returns_existing_record(store):
    experiment, run = _seeded_run(store)
    key = "ev-idem-1"
    first = store.append_evidence(
        make_evidence(idempotency_key=key, run_id=run.run_id, experiment_id=experiment.experiment_id)
    )
    second = store.append_evidence(
        make_evidence(idempotency_key=key, run_id=run.run_id, experiment_id=experiment.experiment_id)
    )
    assert second == first
    assert second.evidence_id == first.evidence_id

    conn = sqlite3.connect(str(store._db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM evidence WHERE idempotency_key = ?", (key,)).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_append_evidence_idempotency_conflict_raises(store):
    experiment, run = _seeded_run(store)
    key = "ev-idem-2"
    store.append_evidence(
        make_evidence(idempotency_key=key, run_id=run.run_id, experiment_id=experiment.experiment_id)
    )
    with pytest.raises(IdempotencyConflictError):
        store.append_evidence(
            make_evidence(
                idempotency_key=key,
                run_id=run.run_id,
                experiment_id=experiment.experiment_id,
                verdict="FAIL",
            )
        )


def test_append_evidence_idempotency_key_is_scoped_per_producer(store):
    """Regression test: two unrelated producers writing distinct evidence
    under the identical idempotency key string must both succeed as two
    independently retrievable records, not collide.
    """
    experiment, run = _seeded_run(store)
    shared_key = "2026-09-04-btc-sweep"

    hsa_evidence = store.append_evidence(
        make_evidence(
            idempotency_key=shared_key,
            run_id=run.run_id,
            experiment_id=experiment.experiment_id,
            producer="HSA",
            verdict="PASS",
        )
    )
    neo_evidence = store.append_evidence(
        make_evidence(
            idempotency_key=shared_key,
            run_id=run.run_id,
            experiment_id=experiment.experiment_id,
            producer="NEO",
            evidence_type=EvidenceType.NEO_OBSERVATION,
            verdict=None,
        )
    )

    assert hsa_evidence.evidence_id != neo_evidence.evidence_id
    assert store.get_evidence(hsa_evidence.evidence_id).producer == "HSA"
    assert store.get_evidence(neo_evidence.evidence_id).producer == "NEO"

    conn = sqlite3.connect(str(store._db_path))
    try:
        rows = conn.execute(
            "SELECT producer FROM evidence WHERE idempotency_key = ? ORDER BY producer",
            (shared_key,),
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == ["HSA", "NEO"]

    # HSA's own retry of its own key is still idempotent.
    hsa_replay = store.append_evidence(
        make_evidence(
            idempotency_key=shared_key,
            run_id=run.run_id,
            experiment_id=experiment.experiment_id,
            producer="HSA",
            verdict="PASS",
        )
    )
    assert hsa_replay.evidence_id == hsa_evidence.evidence_id


def test_append_evidence_replay_with_differing_created_at_utc_is_not_a_conflict(store):
    """Regression test: created_at_utc is a server-assignable record-creation
    timestamp -- when a caller omits it, the API layer fills it with
    wall-clock time on every attempt, so a byte-identical HTTP retry has a
    different created_at_utc on each try. That must still be recognised as
    a replay, not rejected as a conflict.
    """
    experiment, run = _seeded_run(store)
    key = "ev-retry-varying-created-at"

    first = store.append_evidence(
        make_evidence(
            idempotency_key=key,
            run_id=run.run_id,
            experiment_id=experiment.experiment_id,
            created_at_utc=_now(offset_seconds=1),
        )
    )
    second = store.append_evidence(
        make_evidence(
            idempotency_key=key,
            run_id=run.run_id,
            experiment_id=experiment.experiment_id,
            created_at_utc=_now(offset_seconds=99),
        )
    )

    assert second.evidence_id == first.evidence_id
    assert second == first  # returns the stored record, including its original created_at_utc

    conn = sqlite3.connect(str(store._db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM evidence WHERE idempotency_key = ?", (key,)).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_append_evidence_replay_with_differing_observed_at_utc_still_conflicts(store):
    """observed_at_utc is caller-authored and semantically meaningful (when
    the thing was observed, not when the row was created) -- unlike
    created_at_utc, it must stay part of the fingerprint. A producer that
    reuses a key while genuinely changing the observation time must still
    get a loud IdempotencyConflictError.
    """
    experiment, run = _seeded_run(store)
    key = "ev-retry-varying-observed-at"

    store.append_evidence(
        make_evidence(
            idempotency_key=key,
            run_id=run.run_id,
            experiment_id=experiment.experiment_id,
            observed_at_utc=_now(offset_seconds=1),
        )
    )
    with pytest.raises(IdempotencyConflictError):
        store.append_evidence(
            make_evidence(
                idempotency_key=key,
                run_id=run.run_id,
                experiment_id=experiment.experiment_id,
                observed_at_utc=_now(offset_seconds=2),
            )
        )


def test_append_evidence_replay_with_materially_different_body_still_conflicts(store):
    """Sanity check that excluding the creation timestamp did not
    accidentally widen the exclusion to genuine content: a materially
    different body under the same producer/key must still raise.
    """
    experiment, run = _seeded_run(store)
    key = "ev-retry-different-environment"

    store.append_evidence(
        make_evidence(
            idempotency_key=key,
            run_id=run.run_id,
            experiment_id=experiment.experiment_id,
            environment="dev",
            metrics={"sharpe": 1.0},
        )
    )
    with pytest.raises(IdempotencyConflictError):
        store.append_evidence(
            make_evidence(
                idempotency_key=key,
                run_id=run.run_id,
                experiment_id=experiment.experiment_id,
                environment="prod",
                metrics={"sharpe": 2.0},
            )
        )


def test_append_evidence_rewrite_under_existing_evidence_id_raises_immutability(store):
    experiment, run = _seeded_run(store)
    evidence = make_evidence(idempotency_key="ev-a", run_id=run.run_id, experiment_id=experiment.experiment_id)
    store.append_evidence(evidence)

    rewrite = evidence.model_copy(update={"idempotency_key": "ev-b", "verdict": "FAIL"})
    with pytest.raises(ImmutabilityError):
        store.append_evidence(rewrite)


def test_append_evidence_concurrent_replay_creates_exactly_one_row(store):
    experiment, run = _seeded_run(store)
    key = "ev-idem-concurrent"
    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results: list[EvidenceRecord] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            r = store.append_evidence(
                make_evidence(idempotency_key=key, run_id=run.run_id, experiment_id=experiment.experiment_id)
            )
            with lock:
                results.append(r)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len({r.evidence_id for r in results}) == 1

    conn = sqlite3.connect(str(store._db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM evidence WHERE idempotency_key = ?", (key,)).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


# --- ArtifactRecord ------------------------------------------------------------------


def test_register_artifact_requires_existing_run_and_evidence(store):
    with pytest.raises(NotFoundError):
        store.register_artifact(make_artifact(run_id="run_" + "0" * 32))

    with pytest.raises(NotFoundError):
        store.register_artifact(make_artifact(evidence_id="ev_" + "0" * 32))


def test_register_artifact_roundtrip_and_idempotent_replay(store):
    artifact = make_artifact()
    result = store.register_artifact(artifact)
    assert result == artifact

    replay = store.register_artifact(make_artifact(artifact_id=artifact.artifact_id, sha256=artifact.sha256))
    assert replay == artifact


def test_register_artifact_conflict_raises_immutability(store):
    artifact = make_artifact()
    store.register_artifact(artifact)
    with pytest.raises(ImmutabilityError):
        store.register_artifact(make_artifact(artifact_id=artifact.artifact_id, sha256="c" * 64))


def test_attach_artifact_links_and_is_idempotent(store):
    experiment, run = _seeded_run(store)
    evidence = store.append_evidence(
        make_evidence(idempotency_key="ev-att", run_id=run.run_id, experiment_id=experiment.experiment_id)
    )
    artifact = store.register_artifact(make_artifact())

    attached = store.attach_artifact(artifact.artifact_id, evidence_id=evidence.evidence_id, run_id=run.run_id)
    assert attached.evidence_id == evidence.evidence_id
    assert attached.run_id == run.run_id

    # Re-attaching the same links is a no-op, not an error.
    again = store.attach_artifact(artifact.artifact_id, evidence_id=evidence.evidence_id, run_id=run.run_id)
    assert again == attached


def test_attach_artifact_relink_to_different_parent_raises_immutability(store):
    experiment, run = _seeded_run(store)
    evidence1 = store.append_evidence(
        make_evidence(idempotency_key="ev-r1", run_id=run.run_id, experiment_id=experiment.experiment_id)
    )
    evidence2 = store.append_evidence(
        make_evidence(idempotency_key="ev-r2", run_id=run.run_id, experiment_id=experiment.experiment_id)
    )
    artifact = store.register_artifact(make_artifact())
    store.attach_artifact(artifact.artifact_id, evidence_id=evidence1.evidence_id)

    with pytest.raises(ImmutabilityError):
        store.attach_artifact(artifact.artifact_id, evidence_id=evidence2.evidence_id)

    experiment2 = store.create_experiment(make_experiment(experiment_id=new_experiment_id()))
    run2 = store.create_run(make_run(experiment2.experiment_id))
    artifact2 = store.register_artifact(make_artifact())
    store.attach_artifact(artifact2.artifact_id, run_id=run.run_id)
    with pytest.raises(ImmutabilityError):
        store.attach_artifact(artifact2.artifact_id, run_id=run2.run_id)


def test_attach_artifact_unknown_ids_raise_not_found(store):
    experiment, run = _seeded_run(store)
    evidence = store.append_evidence(
        make_evidence(idempotency_key="ev-att2", run_id=run.run_id, experiment_id=experiment.experiment_id)
    )
    artifact = store.register_artifact(make_artifact())

    with pytest.raises(NotFoundError):
        store.attach_artifact("art_" + "0" * 32, evidence_id=evidence.evidence_id)
    with pytest.raises(NotFoundError):
        store.attach_artifact(artifact.artifact_id, evidence_id="ev_" + "0" * 32)
    with pytest.raises(NotFoundError):
        store.attach_artifact(artifact.artifact_id, run_id="run_" + "0" * 32)


# --- PromotionTransition / StrategyHealthRecord ---------------------------------------


def test_record_promotion_roundtrip_idempotent_and_conflict(store):
    transition = make_promotion()
    result = store.record_promotion(transition)
    assert result == transition

    replay = store.record_promotion(transition.model_copy())
    assert replay == transition

    conflicting = transition.model_copy(update={"reason": "different reason"})
    with pytest.raises(ImmutabilityError):
        store.record_promotion(conflicting)


def test_record_health_roundtrip_idempotent_and_conflict(store):
    record = make_health()
    result = store.record_health(record)
    assert result == record

    replay = store.record_health(record.model_copy())
    assert replay == record

    conflicting = record.model_copy(update={"confidence": 0.1})
    with pytest.raises(ImmutabilityError):
        store.record_health(conflicting)


# --- Queries: filters and pagination ---------------------------------------------------


def test_query_evidence_filters_and_combinations(store):
    experiment, run = _seeded_run(store)
    other_experiment = store.create_experiment(make_experiment(experiment_id=new_experiment_id()))
    other_run = store.create_run(make_run(other_experiment.experiment_id))

    e1 = store.append_evidence(
        make_evidence(
            idempotency_key="q1",
            run_id=run.run_id,
            experiment_id=experiment.experiment_id,
            evidence_type=EvidenceType.BACKTEST,
            strategy_id="EMA_PULLBACK",
            strategy_version="v1.0.0",
            created_at_utc=_now(offset_seconds=10),
        )
    )
    e2 = store.append_evidence(
        make_evidence(
            idempotency_key="q2",
            run_id=other_run.run_id,
            experiment_id=other_experiment.experiment_id,
            evidence_type=EvidenceType.OUT_OF_SAMPLE,
            strategy_id="EMA_PULLBACK",
            strategy_version="v2.0.0",
            created_at_utc=_now(offset_seconds=20),
        )
    )

    assert [r.evidence_id for r in store.query_evidence(strategy_id="EMA_PULLBACK")] == [
        e2.evidence_id,
        e1.evidence_id,
    ]
    assert [r.evidence_id for r in store.query_evidence(strategy_version="v1.0.0")] == [e1.evidence_id]
    assert [r.evidence_id for r in store.query_evidence(run_id=run.run_id)] == [e1.evidence_id]
    assert [r.evidence_id for r in store.query_evidence(experiment_id=other_experiment.experiment_id)] == [
        e2.evidence_id
    ]
    assert [r.evidence_id for r in store.query_evidence(evidence_type=EvidenceType.BACKTEST)] == [e1.evidence_id]
    assert [
        r.evidence_id
        for r in store.query_evidence(since=_now(offset_seconds=15), until=_now(offset_seconds=25))
    ] == [e2.evidence_id]

    combo = store.query_evidence(strategy_id="EMA_PULLBACK", evidence_type=EvidenceType.OUT_OF_SAMPLE)
    assert [r.evidence_id for r in combo] == [e2.evidence_id]

    assert store.query_evidence(strategy_id="NO_SUCH_STRATEGY") == []


def test_query_evidence_pagination_across_page_boundary(store):
    experiment, run = _seeded_run(store)
    written = []
    for i in range(5):
        written.append(
            store.append_evidence(
                make_evidence(
                    idempotency_key=f"page-{i}",
                    run_id=run.run_id,
                    experiment_id=experiment.experiment_id,
                    created_at_utc=_now(offset_seconds=i),
                )
            )
        )
    # Newest first.
    expected_order = [w.evidence_id for w in reversed(written)]

    page1 = store.query_evidence(run_id=run.run_id, limit=2, offset=0)
    page2 = store.query_evidence(run_id=run.run_id, limit=2, offset=2)
    page3 = store.query_evidence(run_id=run.run_id, limit=2, offset=4)

    got = [r.evidence_id for r in (page1 + page2 + page3)]
    assert got == expected_order
    assert len(set(got)) == 5  # no duplicates
    assert page3[1:] == []  # last page only has 1 row


def test_query_artifacts_filters(store):
    experiment, run = _seeded_run(store)
    evidence = store.append_evidence(
        make_evidence(idempotency_key="qa1", run_id=run.run_id, experiment_id=experiment.experiment_id)
    )
    a1 = store.register_artifact(make_artifact(registered_at=_now(offset_seconds=1)))
    a2 = store.register_artifact(make_artifact(registered_at=_now(offset_seconds=2)))
    store.attach_artifact(a1.artifact_id, evidence_id=evidence.evidence_id)
    store.attach_artifact(a2.artifact_id, run_id=run.run_id)

    assert [a.artifact_id for a in store.query_artifacts(evidence_id=evidence.evidence_id)] == [a1.artifact_id]
    assert [a.artifact_id for a in store.query_artifacts(run_id=run.run_id)] == [a2.artifact_id]
    assert store.query_artifacts(evidence_id="ev_" + "0" * 32) == []


def test_query_promotions_filters(store):
    p1 = store.record_promotion(make_promotion(strategy_version="v1.0.0", at_utc=_now(offset_seconds=1)))
    p2 = store.record_promotion(make_promotion(strategy_version="v2.0.0", at_utc=_now(offset_seconds=2)))

    assert [p.transition_id for p in store.query_promotions(strategy_id="EMA_PULLBACK")] == [
        p2.transition_id,
        p1.transition_id,
    ]
    assert [p.transition_id for p in store.query_promotions(strategy_version="v1.0.0")] == [p1.transition_id]


def test_query_health_filters(store):
    h1 = store.record_health(make_health(strategy_version="v1.0.0", observed_at_utc=_now(offset_seconds=1)))
    h2 = store.record_health(make_health(strategy_version="v2.0.0", observed_at_utc=_now(offset_seconds=2)))

    assert [h.health_id for h in store.query_health(strategy_id="EMA_PULLBACK")] == [h2.health_id, h1.health_id]
    assert [h.health_id for h in store.query_health(strategy_version="v2.0.0")] == [h2.health_id]


# --- health() -----------------------------------------------------------------------


def test_health_passes_on_good_store(store):
    assert store.health() is None


def test_health_raises_on_broken_store(store, db_path):
    store.close()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            os.remove(p)

    with pytest.raises(MetadataStoreError):
        store.health()


def test_health_raises_when_live_store_backing_directory_is_removed(store, db_path):
    """Regression test for the bug the PL's spot-check found: a live store
    holds an open file descriptor to the (now-unlinked) database inode, so
    a health check built only on that cached connection keeps succeeding
    forever even after the whole backing directory is gone (an unmounted
    volume, a deleted data dir). Deliberately does NOT call store.close()
    first -- that's the point: the connection stays live and cached.
    """
    assert store.health() is None  # sanity: healthy before the volume disappears

    shutil.rmtree(db_path.parent)

    with pytest.raises(MetadataStoreError, match="does not exist"):
        store.health()


def test_health_raises_when_db_file_truncated_under_live_store(store, db_path):
    store.health()  # sanity

    # Empty the file to zero bytes without closing the store -- SQLite
    # treats a zero-length file as a valid (but schema-less) database
    # rather than raising immediately on open, so this specifically
    # exercises the round-trip-read check, not the file-existence check.
    with open(db_path, "wb"):
        pass

    with pytest.raises(MetadataStoreError):
        store.health()


def test_health_raises_when_schema_migrations_row_missing(store, db_path):
    store.health()  # sanity

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DELETE FROM schema_migrations")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(MetadataStoreError, match="schema_migrations"):
        store.health()


def test_constructor_with_blocked_parent_path_raises_metadata_store_error(tmp_path):
    blocking_file = tmp_path / "not_a_directory"
    blocking_file.write_text("i am a file, not a directory")

    with pytest.raises(MetadataStoreError):
        SQLiteMetadataStore(blocking_file / "x.db")

    # And specifically not a raw OSError/FileExistsError escaping past the
    # CER error hierarchy -- callers must never have to catch OSError to
    # handle "the store is unusable".
    try:
        SQLiteMetadataStore(blocking_file / "x.db")
    except MetadataStoreError:
        pass
    except OSError as exc:  # pragma: no cover - failure path
        pytest.fail(f"raw OSError leaked from constructor: {exc!r}")


def test_health_does_not_recreate_directory_or_rerun_migrations(store, db_path):
    store.health()  # sanity

    parent = db_path.parent
    shutil.rmtree(parent)
    assert not parent.exists()

    with pytest.raises(MetadataStoreError):
        store.health()

    # health() must be a pure check: no side effect of recreating the
    # directory, the db file, or re-running migrations.
    assert not parent.exists()
    assert not db_path.exists()


# --- Restart persistence --------------------------------------------------------------


def test_restart_persistence_all_entities_survive(db_path):
    store1 = SQLiteMetadataStore(db_path)
    try:
        store1.register_strategy(make_strategy())
        store1.register_strategy_version(make_strategy_version())
        experiment = store1.create_experiment(make_experiment())
        run = store1.create_run(make_run(experiment.experiment_id))
        evidence = store1.append_evidence(
            make_evidence(idempotency_key="restart-ev", run_id=run.run_id, experiment_id=experiment.experiment_id)
        )
        artifact = store1.register_artifact(make_artifact())
        store1.attach_artifact(artifact.artifact_id, evidence_id=evidence.evidence_id, run_id=run.run_id)
        promotion = store1.record_promotion(make_promotion())
        health = store1.record_health(make_health())
    finally:
        store1.close()

    store2 = SQLiteMetadataStore(db_path)
    try:
        fetched_evidence = store2.get_evidence(evidence.evidence_id)
        assert fetched_evidence == evidence

        artifacts = store2.query_artifacts(evidence_id=evidence.evidence_id)
        assert len(artifacts) == 1
        assert artifacts[0].artifact_id == artifact.artifact_id
        assert artifacts[0].run_id == run.run_id

        promotions = store2.query_promotions(strategy_id="EMA_PULLBACK")
        assert promotion.transition_id in {p.transition_id for p in promotions}

        healths = store2.query_health(strategy_id="EMA_PULLBACK")
        assert health.health_id in {h.health_id for h in healths}

        # Run persisted: close_run against the second store instance must
        # find it (NotFoundError would mean the run row did not survive
        # restart) and its content must match what store1 wrote.
        closed = store2.close_run(run.run_id, status=RunStatus.CLOSED, ended_at=_now(offset_seconds=99))
        assert closed.experiment_id == run.experiment_id
        assert closed.producer == run.producer
        assert closed.dataset_id == run.dataset_id
        assert closed.started_at == run.started_at
        assert closed.status == RunStatus.CLOSED

        # A fresh idempotency-key replay of the strategy registration also
        # proves the strategy row survived restart with matching content.
        replay = store2.register_strategy(make_strategy())
        assert replay == make_strategy()
    finally:
        store2.close()


# --- General thread-safety --------------------------------------------------------------


def test_many_threads_can_write_distinct_evidence_concurrently(store):
    experiment, run = _seeded_run(store)
    n_threads = 16
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(i: int):
        try:
            store.append_evidence(
                make_evidence(
                    idempotency_key=f"distinct-{i}",
                    run_id=run.run_id,
                    experiment_id=experiment.experiment_id,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    results = store.query_evidence(run_id=run.run_id, limit=100)
    assert len(results) == n_threads
