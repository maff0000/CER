"""CER identity kinds: validation and generation.

CER v1 defines six identity kinds:

* ``strategy_id`` — caller-supplied logical identity (e.g. ``EMA_PULLBACK``).
* ``strategy_version`` — caller-supplied immutable version (e.g. ``v1.2.0``).
* ``experiment_id`` — CER-generated, prefixed ``exp_``.
* ``run_id`` — CER-generated, prefixed ``run_``.
* ``evidence_id`` — CER-generated, prefixed ``ev_``.
* ``artifact_id`` — CER-generated, prefixed ``art_``.

Generated identifiers are opaque strings of the form ``<prefix><uuid4 hex>``
(e.g. ``run_3f9a1c2e4b7d4a1c9e2f6a0b1c2d3e4f``). They carry no meaning beyond
uniqueness and their kind prefix; callers must not parse them for anything
but the prefix.

``strategy_id`` and ``strategy_version`` are logical identities supplied by
the caller (e.g. a strategy repository or human), not generated here — only
their shape is validated.

Extendability: the PID requires the design remain extendable for
``trade_id``, ``decision_id``, ``execution_id`` and ``strategy_instance_id``
without a rewrite. This module keeps a single registry
(:data:`ID_KINDS`) of ``IdKind`` definitions keyed by kind name and prefix;
adding a new v1.x identity kind is a matter of registering a new
``IdKind`` entry (data), not restructuring validation/generation logic.
None of the four extension-point kinds are implemented as v1 entities —
they exist only to prove the extension point is real (see tests).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from .errors import IdentityError

#: Maximum length for any identifier string (generated or caller-supplied).
MAX_IDENTIFIER_LENGTH = 128

#: Shape for a caller-supplied logical identity segment (strategy_id,
#: strategy_version): letters, digits, dot, underscore, hyphen. No
#: whitespace, no path separators.
_LOGICAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class IdKind:
    """Definition of one identity kind.

    ``generated`` kinds are minted by :func:`generate_id` as
    ``<prefix><uuid4 hex>``; ``logical`` kinds are supplied by the caller and
    only shape-validated (never generated) by :func:`validate_logical_id`.
    """

    name: str
    prefix: str | None  # None for logical (caller-supplied) kinds
    generated: bool


#: Registry of all known identity kinds, keyed by kind name. This is the
#: extension point: a future identity kind is added here as data.
ID_KINDS: dict[str, IdKind] = {
    "strategy_id": IdKind(name="strategy_id", prefix=None, generated=False),
    "strategy_version": IdKind(name="strategy_version", prefix=None, generated=False),
    "experiment_id": IdKind(name="experiment_id", prefix="exp_", generated=True),
    "run_id": IdKind(name="run_id", prefix="run_", generated=True),
    "evidence_id": IdKind(name="evidence_id", prefix="ev_", generated=True),
    "artifact_id": IdKind(name="artifact_id", prefix="art_", generated=True),
    # --- Extension point (not implemented as v1 entities) ---
    # Registering these here is what makes the extension "data, not a
    # rewrite": generate_id()/validate_id() work for them immediately.
    "trade_id": IdKind(name="trade_id", prefix="trd_", generated=True),
    "decision_id": IdKind(name="decision_id", prefix="dec_", generated=True),
    "execution_id": IdKind(name="execution_id", prefix="exe_", generated=True),
    "strategy_instance_id": IdKind(
        name="strategy_instance_id", prefix="si_", generated=True
    ),
}

#: v1 identity kinds actually in use as CER entities (the other four in
#: ID_KINDS are the extension point only, not implemented in v1).
V1_ID_KINDS: frozenset[str] = frozenset(
    {
        "strategy_id",
        "strategy_version",
        "experiment_id",
        "run_id",
        "evidence_id",
        "artifact_id",
    }
)


def _kind(kind_name: str) -> IdKind:
    try:
        return ID_KINDS[kind_name]
    except KeyError as exc:
        raise IdentityError(f"unknown identity kind {kind_name!r}") from exc


def generate_id(kind_name: str) -> str:
    """Generate a new opaque identifier for a generated identity kind.

    Raises ``IdentityError`` for an unknown or non-generated kind (e.g.
    ``strategy_id``, which is caller-supplied).
    """
    kind = _kind(kind_name)
    if not kind.generated:
        raise IdentityError(
            f"identity kind {kind_name!r} is caller-supplied and cannot be generated"
        )
    assert kind.prefix is not None
    return f"{kind.prefix}{uuid.uuid4().hex}"


def validate_generated_id(kind_name: str, value: str) -> str:
    """Validate a generated-kind identifier (experiment_id, run_id, ...).

    Rejects empty, whitespace-padded, over-long, or wrong-prefix values
    loudly via ``IdentityError``.
    """
    kind = _kind(kind_name)
    if not kind.generated:
        raise IdentityError(
            f"identity kind {kind_name!r} is not a generated kind; "
            "use validate_logical_id instead"
        )
    _reject_empty_or_padded(kind_name, value)
    _reject_over_long(kind_name, value)
    assert kind.prefix is not None
    if not value.startswith(kind.prefix):
        raise IdentityError(
            f"{kind_name} {value!r} must start with prefix {kind.prefix!r}"
        )
    suffix = value[len(kind.prefix) :]
    if not suffix or not re.fullmatch(r"[0-9a-fA-F-]+", suffix):
        raise IdentityError(
            f"{kind_name} {value!r} has an invalid suffix after prefix "
            f"{kind.prefix!r} (expected a hex/ULID-like token)"
        )
    return value


def validate_logical_id(kind_name: str, value: str) -> str:
    """Validate a caller-supplied logical identity (strategy_id, strategy_version).

    Rejects empty, whitespace-padded, over-long, or malformed values loudly.
    """
    kind = _kind(kind_name)
    if kind.generated:
        raise IdentityError(
            f"identity kind {kind_name!r} is a generated kind; "
            "use validate_generated_id instead"
        )
    _reject_empty_or_padded(kind_name, value)
    _reject_over_long(kind_name, value)
    if not _LOGICAL_ID_RE.fullmatch(value):
        raise IdentityError(
            f"{kind_name} {value!r} must start with a letter/digit and contain "
            "only letters, digits, '.', '_' or '-'"
        )
    return value


def validate_id(kind_name: str, value: str) -> str:
    """Validate any identifier, dispatching to the right rule for its kind."""
    kind = _kind(kind_name)
    if kind.generated:
        return validate_generated_id(kind_name, value)
    return validate_logical_id(kind_name, value)


def _reject_empty_or_padded(kind_name: str, value: object) -> None:
    if not isinstance(value, str):
        raise IdentityError(
            f"{kind_name} must be a string, got {type(value).__name__} ({value!r})"
        )
    if value == "":
        raise IdentityError(f"{kind_name} must not be empty")
    if value != value.strip():
        raise IdentityError(f"{kind_name} {value!r} must not have leading/trailing whitespace")
    if value.strip() == "":
        raise IdentityError(f"{kind_name} must not be whitespace-only")


def _reject_over_long(kind_name: str, value: str) -> None:
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise IdentityError(
            f"{kind_name} exceeds max length {MAX_IDENTIFIER_LENGTH} "
            f"(got {len(value)} chars)"
        )


# --- Convenience wrappers for the six v1 kinds -----------------------------


def new_experiment_id() -> str:
    return generate_id("experiment_id")


def new_run_id() -> str:
    return generate_id("run_id")


def new_evidence_id() -> str:
    return generate_id("evidence_id")


def new_artifact_id() -> str:
    return generate_id("artifact_id")


def validate_strategy_id(value: str) -> str:
    return validate_logical_id("strategy_id", value)


def validate_strategy_version(value: str) -> str:
    return validate_logical_id("strategy_version", value)


def validate_experiment_id(value: str) -> str:
    return validate_generated_id("experiment_id", value)


def validate_run_id(value: str) -> str:
    return validate_generated_id("run_id", value)


def validate_evidence_id(value: str) -> str:
    return validate_generated_id("evidence_id", value)


def validate_artifact_id(value: str) -> str:
    return validate_generated_id("artifact_id", value)
