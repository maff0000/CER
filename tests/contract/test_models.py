from datetime import datetime, timedelta, timezone

import pydantic
import pytest

from cer.contract.enums import (
    EvidenceType,
    HealthState,
    ProvenanceCompleteness,
    PromotionState,
    RunStatus,
)
from cer.contract.errors import IdentityError, UnknownEvidenceTypeError
from cer.contract.identity import (
    new_artifact_id,
    new_evidence_id,
    new_experiment_id,
    new_run_id,
)
from cer.contract.models import (
    REQUIRED_PROVENANCE_FIELDS,
    REQUIRED_RUN_PROVENANCE_FIELDS,
    ArtifactRecord,
    Experiment,
    EvidenceRecord,
    PromotionTransition,
    Run,
    Strategy,
    StrategyHealthRecord,
    StrategyVersion,
    compute_provenance_completeness,
    compute_run_provenance_completeness,
)

UTC_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def minimal_evidence(**overrides) -> dict:
    base = dict(
        evidence_id=new_evidence_id(),
        idempotency_key="idem-1",
        evidence_type=EvidenceType.BACKTEST,
        schema_version=1,
        producer="HSA",
        created_at_utc=UTC_NOW,
    )
    base.update(overrides)
    return base


def minimal_run(**overrides) -> dict:
    base = dict(
        run_id=new_run_id(),
        experiment_id=new_experiment_id(),
        status=RunStatus.OPEN,
        started_at=UTC_NOW,
        producer="HSA",
    )
    base.update(overrides)
    return base


def full_run(**overrides) -> dict:
    base = minimal_run(
        producer_version="1.0.0",
        git_repo="git@example.com:org/repo.git",
        git_commit="a" * 40,
        dataset_id="ds1",
        dataset_version="v1",
        dataset_ref="s3://bucket/ds1",
        config_hash="deadbeef",
        config_ref="configs/backtest.yaml",
        environment="dell-debian",
    )
    base.update(overrides)
    return base


def full_evidence(**overrides) -> dict:
    base = minimal_evidence(
        strategy_id="EMA_PULLBACK",
        strategy_version="v1.0.0",
        experiment_id=new_experiment_id(),
        run_id=new_run_id(),
        producer_version="1.0.0",
        observed_at_utc=UTC_NOW,
        git_repo="git@example.com:org/repo.git",
        git_commit="a" * 40,
        dataset_id="ds1",
        dataset_version="v1",
        dataset_ref="s3://bucket/ds1",
        config_hash="deadbeef",
        config_ref="configs/backtest.yaml",
        environment="dell-debian",
        instruments=["EURUSD"],
        timeframes=["H1"],
        metrics={"sharpe": 1.2, "trades": 42},
        verdict="PASS",
        status="COMPLETE",
        regime_tags=["trending"],
        parent_evidence_ids=[],
        parent_run_ids=[],
        artifact_ids=[],
        notes={"observation": "clean run"},
    )
    base.update(overrides)
    return base


# --- extra="forbid" ---------------------------------------------------------


def test_strategy_rejects_unknown_field():
    with pytest.raises(pydantic.ValidationError):
        Strategy(
            strategy_id="EMA_PULLBACK",
            name="EMA Pullback",
            thesis="mean reversion",
            created_at=UTC_NOW,
            bogus_field="nope",
        )


def test_evidence_record_rejects_unknown_field():
    with pytest.raises(pydantic.ValidationError):
        EvidenceRecord(**minimal_evidence(), typo_field="x")


# --- naive datetime rejection / UTC normalisation ---------------------------


def test_naive_datetime_rejected():
    with pytest.raises(pydantic.ValidationError):
        Strategy(
            strategy_id="EMA_PULLBACK",
            name="EMA Pullback",
            thesis="mean reversion",
            created_at=datetime(2026, 1, 1, 12, 0, 0),  # naive
        )


def test_non_utc_aware_datetime_is_normalised_to_utc():
    tz = timezone(timedelta(hours=5))
    local_dt = datetime(2026, 1, 1, 17, 0, 0, tzinfo=tz)  # == 12:00 UTC
    s = Strategy(
        strategy_id="EMA_PULLBACK",
        name="EMA Pullback",
        thesis="mean reversion",
        created_at=local_dt,
    )
    assert s.created_at.tzinfo == timezone.utc
    assert s.created_at == UTC_NOW


# --- unknown evidence type fails loudly -------------------------------------


def test_evidence_record_rejects_unknown_evidence_type():
    with pytest.raises(UnknownEvidenceTypeError):
        EvidenceRecord(**minimal_evidence(evidence_type="NOT_A_REAL_TYPE"))


def test_evidence_record_accepts_known_evidence_type_string():
    rec = EvidenceRecord(**minimal_evidence(evidence_type="BACKTEST"))
    assert rec.evidence_type is EvidenceType.BACKTEST


# --- identity validation flows through models -------------------------------


def test_evidence_record_rejects_malformed_evidence_id():
    with pytest.raises(IdentityError):
        EvidenceRecord(**minimal_evidence(evidence_id="not-a-valid-id"))


def test_strategy_version_rejects_malformed_strategy_id():
    with pytest.raises(IdentityError):
        StrategyVersion(
            strategy_id="bad id with spaces",
            strategy_version="v1",
            git_repo="repo",
            git_commit="a" * 40,
            created_at=UTC_NOW,
        )


# --- Strategy / StrategyVersion / Experiment / Run --------------------------


def test_strategy_round_trip():
    s = Strategy(
        strategy_id="EMA_PULLBACK",
        name="EMA Pullback",
        thesis="mean reversion on pullbacks",
        created_at=UTC_NOW,
    )
    assert s.strategy_id == "EMA_PULLBACK"


def test_experiment_optional_strategy_fields_default_none():
    exp = Experiment(
        experiment_id=new_experiment_id(),
        objective="validate EMA pullback",
        producer="HSA",
        created_at=UTC_NOW,
    )
    assert exp.strategy_id is None
    assert exp.strategy_version is None


def test_run_requires_only_identity_and_ownership_fields():
    # A run with no reproducibility context at all (e.g. a NEO surveillance
    # run) must construct cleanly -- CER records the absence explicitly
    # rather than forcing the caller to fabricate a filler value.
    run = Run(**minimal_run())
    assert run.git_repo is None
    assert run.dataset_id is None
    assert run.producer_version is None


def test_run_missing_identity_field_still_fails_loudly():
    with pytest.raises(pydantic.ValidationError):
        Run(
            experiment_id=new_experiment_id(),
            status=RunStatus.OPEN,
            started_at=UTC_NOW,
            producer="HSA",
            # missing run_id
        )


def test_run_ended_at_is_optional():
    run = Run(**full_run())
    assert run.ended_at is None


def test_run_optional_reproducibility_field_rejects_explicit_empty_string():
    # Absence must be expressed as None, not as a silently-recorded blank.
    with pytest.raises(pydantic.ValidationError):
        Run(**minimal_run(dataset_id=""))
    with pytest.raises(pydantic.ValidationError):
        Run(**minimal_run(git_repo="   "))


# --- Run: provenance completeness -------------------------------------------


def test_minimal_run_is_incomplete_with_exact_missing_list():
    run = Run(**minimal_run())
    assert run.provenance_completeness == ProvenanceCompleteness.INCOMPLETE
    assert set(run.missing_provenance) == set(REQUIRED_RUN_PROVENANCE_FIELDS)


def test_full_run_is_complete():
    run = Run(**full_run())
    assert run.provenance_completeness == ProvenanceCompleteness.COMPLETE
    assert run.missing_provenance == []


def test_partial_run_reports_exact_missing_fields():
    data = full_run()
    data["dataset_id"] = None
    data["config_hash"] = None
    run = Run(**data)
    assert run.provenance_completeness == ProvenanceCompleteness.INCOMPLETE
    assert set(run.missing_provenance) == {"dataset_id", "config_hash"}


def test_run_caller_supplied_completeness_is_recomputed_not_trusted():
    run = Run(
        **minimal_run(),
        provenance_completeness=ProvenanceCompleteness.COMPLETE,
        missing_provenance=[],
    )
    assert run.provenance_completeness == ProvenanceCompleteness.INCOMPLETE
    assert len(run.missing_provenance) > 0


def test_compute_run_provenance_completeness_matches_stored_fields():
    run = Run(**full_run())
    completeness, missing = compute_run_provenance_completeness(run)
    assert completeness == ProvenanceCompleteness.COMPLETE
    assert missing == []

    run2 = Run(**minimal_run())
    completeness2, missing2 = compute_run_provenance_completeness(run2)
    assert completeness2 == run2.provenance_completeness
    assert missing2 == run2.missing_provenance


def test_shared_completeness_helper_used_by_both_models():
    # Exercise the shared core through both public entry points to prove
    # they aren't independently duplicated logic.
    evidence = EvidenceRecord(**minimal_evidence())
    run = Run(**minimal_run())
    ev_completeness, ev_missing = compute_provenance_completeness(evidence)
    run_completeness, run_missing = compute_run_provenance_completeness(run)
    assert ev_completeness == ProvenanceCompleteness.INCOMPLETE
    assert run_completeness == ProvenanceCompleteness.INCOMPLETE
    assert set(ev_missing) == set(REQUIRED_PROVENANCE_FIELDS)
    assert set(run_missing) == set(REQUIRED_RUN_PROVENANCE_FIELDS)


# --- EvidenceRecord: metrics types ------------------------------------------


def test_evidence_record_metrics_accept_mixed_value_types():
    rec = EvidenceRecord(
        **minimal_evidence(metrics={"sharpe": 1.5, "trades": 10, "note": "ok", "passed": True})
    )
    assert rec.metrics["sharpe"] == 1.5
    assert rec.metrics["trades"] == 10
    assert rec.metrics["note"] == "ok"
    assert rec.metrics["passed"] is True


# --- Provenance completeness -------------------------------------------------


def test_minimal_evidence_is_incomplete_with_explicit_missing_list():
    rec = EvidenceRecord(**minimal_evidence())
    assert rec.provenance_completeness == ProvenanceCompleteness.INCOMPLETE
    assert set(rec.missing_provenance) == set(REQUIRED_PROVENANCE_FIELDS)


def test_full_evidence_is_complete():
    rec = EvidenceRecord(**full_evidence())
    assert rec.provenance_completeness == ProvenanceCompleteness.COMPLETE
    assert rec.missing_provenance == []


def test_partial_evidence_reports_exact_missing_fields():
    data = full_evidence()
    # Remove a handful of fields to simulate partial provenance.
    data["git_commit"] = None
    data["dataset_ref"] = None
    data["metrics"] = {}
    rec = EvidenceRecord(**data)
    assert rec.provenance_completeness == ProvenanceCompleteness.INCOMPLETE
    assert set(rec.missing_provenance) == {"git_commit", "dataset_ref", "metrics"}


def test_compute_provenance_completeness_never_guesses():
    # Calling the function directly must reflect exactly the record's own
    # field state -- it must never backfill or infer a present value.
    rec = EvidenceRecord(**minimal_evidence())
    completeness, missing = compute_provenance_completeness(rec)
    assert completeness == ProvenanceCompleteness.INCOMPLETE
    assert missing == list(rec.missing_provenance)
    # None of the actual field values were mutated/guessed.
    assert rec.git_repo is None
    assert rec.dataset_id is None


def test_caller_supplied_provenance_completeness_is_recomputed_not_trusted():
    # Passing a (wrong) provenance_completeness/missing_provenance explicitly
    # must not be trusted -- it is always derived from the other fields.
    rec = EvidenceRecord(
        **minimal_evidence(),
        provenance_completeness=ProvenanceCompleteness.COMPLETE,
        missing_provenance=[],
    )
    assert rec.provenance_completeness == ProvenanceCompleteness.INCOMPLETE
    assert len(rec.missing_provenance) > 0


# --- ArtifactRecord -----------------------------------------------------------


def test_artifact_record_round_trip():
    art = ArtifactRecord(
        artifact_id=new_artifact_id(),
        sha256="a" * 64,
        size_bytes=1024,
        content_type="application/json",
        filename="results.json",
        uri="file:///data/results.json",
        registered_at=UTC_NOW,
    )
    assert art.sha256 == "a" * 64
    assert art.run_id is None


def test_artifact_record_rejects_bad_sha256():
    with pytest.raises(pydantic.ValidationError):
        ArtifactRecord(
            artifact_id=new_artifact_id(),
            sha256="not-a-hash",
            size_bytes=10,
            content_type="text/plain",
            filename="f.txt",
            uri="file:///f.txt",
            registered_at=UTC_NOW,
        )


def test_artifact_record_rejects_negative_size():
    with pytest.raises(pydantic.ValidationError):
        ArtifactRecord(
            artifact_id=new_artifact_id(),
            sha256="a" * 64,
            size_bytes=-1,
            content_type="text/plain",
            filename="f.txt",
            uri="file:///f.txt",
            registered_at=UTC_NOW,
        )


# --- PromotionTransition ------------------------------------------------------


def test_promotion_transition_requires_reason_and_evidence():
    with pytest.raises(pydantic.ValidationError):
        PromotionTransition(
            strategy_id="EMA_PULLBACK",
            strategy_version="v1.0.0",
            from_state=PromotionState.DRAFT,
            to_state=PromotionState.IMPLEMENTED,
            at_utc=UTC_NOW,
            authority="human:alice",
            producer="HSA",
            evidence_ids=[],  # empty -- not allowed
            reason="looks good",
        )


def test_promotion_transition_round_trip():
    ev_id = new_evidence_id()
    t = PromotionTransition(
        strategy_id="EMA_PULLBACK",
        strategy_version="v1.0.0",
        from_state=PromotionState.BACKTESTED,
        to_state=PromotionState.VALIDATED,
        at_utc=UTC_NOW,
        authority="human:alice",
        producer="HSA",
        evidence_ids=[ev_id],
        reason="out-of-sample validated with sharpe > 1.0",
    )
    assert t.transition_id.startswith("trn_")
    assert t.evidence_ids == [ev_id]


# --- StrategyHealthRecord -----------------------------------------------------


def test_health_record_metrics_are_individually_optional_not_defaulted():
    ev_id = new_evidence_id()
    h = StrategyHealthRecord(
        strategy_id="EMA_PULLBACK",
        strategy_version="v1.0.0",
        health_state=HealthState.WATCH,
        observed_at_utc=UTC_NOW,
        producer="NEO",
        win_rate=0.55,
        reason="trigger rate dropped vs regime baseline",
        evidence_ids=[ev_id],
    )
    assert h.win_rate == 0.55
    # Absent metrics must be None, never defaulted to 0.
    assert h.expectancy_r is None
    assert h.drawdown is None
    assert h.mae is None
    assert h.mfe is None
    assert h.observed_trigger_rate is None


def test_health_record_requires_evidence_backed_reason():
    with pytest.raises(pydantic.ValidationError):
        StrategyHealthRecord(
            strategy_id="EMA_PULLBACK",
            strategy_version="v1.0.0",
            health_state=HealthState.HEALTHY,
            observed_at_utc=UTC_NOW,
            producer="NEO",
            reason="fine",
            evidence_ids=[],  # empty -- not allowed
        )
