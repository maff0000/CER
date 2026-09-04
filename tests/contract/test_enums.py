import pytest

from cer.contract.enums import (
    EVIDENCE_TYPE_GROUP,
    EvidenceGroup,
    EvidenceType,
    HealthState,
    ProvenanceCompleteness,
    PromotionState,
    RunStatus,
    evidence_group_of,
    parse_evidence_type,
)
from cer.contract.errors import UnknownEvidenceTypeError


def test_exactly_20_evidence_types():
    assert len(list(EvidenceType)) == 20


def test_evidence_type_membership_matches_pid():
    expected = {
        "SOURCE_ANALYSIS",
        "STRATEGY_HYPOTHESIS",
        "BACKTEST",
        "OUT_OF_SAMPLE",
        "WALK_FORWARD",
        "MONTE_CARLO",
        "PARAMETER_SWEEP",
        "ABLATION",
        "SHADOW",
        "PAPER",
        "MT5_DEMO",
        "PLUTUS",
        "TINY_LIVE",
        "PERSONAL_LIVE",
        "FUNDED_LIVE",
        "NEO_OBSERVATION",
        "NEO_HYPOTHESIS",
        "REGIME_ANALYSIS",
        "DRIFT_ALERT",
        "STRATEGY_HEALTH",
    }
    assert {t.value for t in EvidenceType} == expected


def test_every_evidence_type_has_a_group():
    for t in EvidenceType:
        assert t in EVIDENCE_TYPE_GROUP
        assert isinstance(EVIDENCE_TYPE_GROUP[t], EvidenceGroup)


def test_group_assignment_matches_pid_headings():
    assert evidence_group_of(EvidenceType.SOURCE_ANALYSIS) == EvidenceGroup.RESEARCH
    assert evidence_group_of(EvidenceType.STRATEGY_HYPOTHESIS) == EvidenceGroup.RESEARCH
    assert evidence_group_of(EvidenceType.BACKTEST) == EvidenceGroup.HISTORICAL
    assert evidence_group_of(EvidenceType.ABLATION) == EvidenceGroup.HISTORICAL
    assert evidence_group_of(EvidenceType.SHADOW) == EvidenceGroup.FORWARD
    assert evidence_group_of(EvidenceType.PLUTUS) == EvidenceGroup.FORWARD
    assert evidence_group_of(EvidenceType.TINY_LIVE) == EvidenceGroup.LIVE
    assert evidence_group_of(EvidenceType.FUNDED_LIVE) == EvidenceGroup.LIVE
    assert evidence_group_of(EvidenceType.STRATEGY_HEALTH) == EvidenceGroup.LEARNING
    assert evidence_group_of(EvidenceType.DRIFT_ALERT) == EvidenceGroup.LEARNING


def test_parse_evidence_type_accepts_known_strings():
    assert parse_evidence_type("BACKTEST") is EvidenceType.BACKTEST
    assert parse_evidence_type(EvidenceType.PAPER) is EvidenceType.PAPER


@pytest.mark.parametrize("bad", ["NOT_A_TYPE", "backtest", "", "BACKTEST ", 123, None])
def test_parse_evidence_type_rejects_unknown_loudly(bad):
    with pytest.raises(UnknownEvidenceTypeError):
        parse_evidence_type(bad)


def test_exactly_10_promotion_states():
    assert len(list(PromotionState)) == 10
    assert {s.value for s in PromotionState} == {
        "DRAFT",
        "IMPLEMENTED",
        "BACKTESTED",
        "VALIDATED",
        "FORWARD_TEST",
        "LIVE_CANDIDATE",
        "TINY_LIVE",
        "APPROVED",
        "SUSPENDED",
        "RETIRED",
    }


def test_exactly_5_health_states():
    assert len(list(HealthState)) == 5
    assert {s.value for s in HealthState} == {
        "HEALTHY",
        "DORMANT",
        "WATCH",
        "DEGRADED",
        "SUSPENDED",
    }


def test_run_status_values():
    assert {s.value for s in RunStatus} == {"OPEN", "CLOSED", "FAILED"}


def test_provenance_completeness_values():
    assert {s.value for s in ProvenanceCompleteness} == {"COMPLETE", "INCOMPLETE"}


def test_all_enums_are_str_subclass():
    for enum_cls in (EvidenceType, EvidenceGroup, PromotionState, HealthState, RunStatus, ProvenanceCompleteness):
        for member in enum_cls:
            assert isinstance(member, str)
