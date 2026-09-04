import pytest

from cer.contract.errors import IdentityError
from cer.contract.identity import (
    ID_KINDS,
    V1_ID_KINDS,
    generate_id,
    new_artifact_id,
    new_evidence_id,
    new_experiment_id,
    new_run_id,
    validate_artifact_id,
    validate_evidence_id,
    validate_experiment_id,
    validate_generated_id,
    validate_id,
    validate_logical_id,
    validate_run_id,
    validate_strategy_id,
    validate_strategy_version,
)

GENERATED_KINDS = [
    ("experiment_id", "exp_", new_experiment_id, validate_experiment_id),
    ("run_id", "run_", new_run_id, validate_run_id),
    ("evidence_id", "ev_", new_evidence_id, validate_evidence_id),
    ("artifact_id", "art_", new_artifact_id, validate_artifact_id),
]


@pytest.mark.parametrize("kind_name,prefix,new_fn,validate_fn", GENERATED_KINDS)
def test_generated_id_round_trips(kind_name, prefix, new_fn, validate_fn):
    value = new_fn()
    assert value.startswith(prefix)
    assert validate_fn(value) == value
    assert validate_id(kind_name, value) == value


@pytest.mark.parametrize("kind_name,prefix,new_fn,validate_fn", GENERATED_KINDS)
def test_generated_ids_are_unique(kind_name, prefix, new_fn, validate_fn):
    values = {new_fn() for _ in range(50)}
    assert len(values) == 50


@pytest.mark.parametrize("kind_name,prefix,new_fn,validate_fn", GENERATED_KINDS)
def test_generated_id_rejects_wrong_prefix(kind_name, prefix, new_fn, validate_fn):
    bad = "wrongprefix_" + new_fn()[len(prefix):]
    with pytest.raises(IdentityError):
        validate_fn(bad)


@pytest.mark.parametrize("kind_name,prefix,new_fn,validate_fn", GENERATED_KINDS)
@pytest.mark.parametrize("bad", ["", "   ", None, 123])
def test_generated_id_rejects_empty_and_wrong_type(kind_name, prefix, new_fn, validate_fn, bad):
    with pytest.raises(IdentityError):
        validate_fn(bad)


@pytest.mark.parametrize("kind_name,prefix,new_fn,validate_fn", GENERATED_KINDS)
def test_generated_id_rejects_whitespace_padding(kind_name, prefix, new_fn, validate_fn):
    padded = " " + new_fn() + " "
    with pytest.raises(IdentityError):
        validate_fn(padded)


@pytest.mark.parametrize("kind_name,prefix,new_fn,validate_fn", GENERATED_KINDS)
def test_generated_id_rejects_over_long(kind_name, prefix, new_fn, validate_fn):
    too_long = prefix + "a" * 200
    with pytest.raises(IdentityError):
        validate_fn(too_long)


# --- strategy_id / strategy_version (caller-supplied logical identities) ---


@pytest.mark.parametrize("good", ["EMA_PULLBACK", "ema-pullback-2", "A", "strategy.v1"])
def test_strategy_id_accepts_valid_shapes(good):
    assert validate_strategy_id(good) == good


@pytest.mark.parametrize("good", ["v1.2.0", "1.0.0", "v1", "release-42"])
def test_strategy_version_accepts_valid_shapes(good):
    assert validate_strategy_version(good) == good


@pytest.mark.parametrize("bad", ["", "   ", " EMA_PULLBACK", "EMA_PULLBACK ", None, 123])
def test_strategy_id_rejects_bad_values(bad):
    with pytest.raises(IdentityError):
        validate_strategy_id(bad)


@pytest.mark.parametrize("bad", ["", "   ", " v1.2.0", "v1.2.0 ", None])
def test_strategy_version_rejects_bad_values(bad):
    with pytest.raises(IdentityError):
        validate_strategy_version(bad)


def test_strategy_id_rejects_over_long():
    with pytest.raises(IdentityError):
        validate_strategy_id("a" * 200)


def test_strategy_id_rejects_path_separators_and_control_chars():
    with pytest.raises(IdentityError):
        validate_strategy_id("../etc/passwd")
    with pytest.raises(IdentityError):
        validate_strategy_id("bad id")


def test_strategy_id_is_not_generated():
    # strategy_id is caller-supplied; there is no generator for it.
    with pytest.raises(IdentityError):
        generate_id("strategy_id")


def test_validate_generated_id_rejects_logical_kind():
    with pytest.raises(IdentityError):
        validate_generated_id("strategy_id", "EMA_PULLBACK")


def test_validate_logical_id_rejects_generated_kind():
    with pytest.raises(IdentityError):
        validate_logical_id("run_id", new_run_id())


def test_unknown_identity_kind_raises():
    with pytest.raises(IdentityError):
        validate_id("not_a_real_kind", "whatever")
    with pytest.raises(IdentityError):
        generate_id("not_a_real_kind")


# --- Extension point -----------------------------------------------------


@pytest.mark.parametrize(
    "kind_name,prefix",
    [
        ("trade_id", "trd_"),
        ("decision_id", "dec_"),
        ("execution_id", "exe_"),
        ("strategy_instance_id", "si_"),
    ],
)
def test_extension_point_kinds_are_registered_and_usable(kind_name, prefix):
    # The PID requires the design remain extendable for these four kinds
    # without a rewrite -- proving they work through the *same* generic
    # generate_id/validate_id functions is exactly what "data, not a
    # rewrite" means. None of these are implemented as v1 entities.
    assert kind_name in ID_KINDS
    assert kind_name not in V1_ID_KINDS
    value = generate_id(kind_name)
    assert value.startswith(prefix)
    assert validate_id(kind_name, value) == value
    with pytest.raises(IdentityError):
        validate_id(kind_name, "not-a-valid-value")


def test_v1_id_kinds_are_exactly_the_six_pid_kinds():
    assert V1_ID_KINDS == {
        "strategy_id",
        "strategy_version",
        "experiment_id",
        "run_id",
        "evidence_id",
        "artifact_id",
    }


def test_adding_a_new_kind_is_pure_data():
    # Demonstrate the extension point does not require touching
    # generate_id/validate_id: register a throwaway kind and use it.
    from cer.contract.identity import IdKind

    ID_KINDS["_test_kind"] = IdKind(name="_test_kind", prefix="tk_", generated=True)
    try:
        value = generate_id("_test_kind")
        assert value.startswith("tk_")
        assert validate_id("_test_kind", value) == value
    finally:
        del ID_KINDS["_test_kind"]
