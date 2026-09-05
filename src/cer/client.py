"""``CERClient`` — the producer/consumer client over the CER HTTP API.

A small synchronous wrapper around ``httpx`` giving producers (HSA, APOLLO,
NEO, ...) one typed method per v1 capability, so they never hand-roll HTTP
or touch database internals (a PID requirement: "producers/consumers ...
must not depend on direct database internals").

It sends the contract-version header on every request, supports
idempotency keys (``Idempotency-Key`` header) on the operations that use
them, and translates a structured CER error response (``{code, message,
request_id}``) back into the matching ``cer.contract.errors`` exception
type by its stable ``code`` — never by string-matching the message. No
retry/backoff framework: anti-bloat, callers own their own retry policy.

Idempotency keys are scoped to your producer name, not global
----------------------------------------------------------------
The server derives the record's id from ``(producer, idempotency_key)``
together, never from the key alone. This means your own retry of the same
key (as the *same* producer, with the same body) is idempotent as
expected — but it also means two different producers may safely reuse the
exact same key string without colliding (e.g. both happening to key on
"today's date"). You never need to coordinate key choice with other
producers to avoid a collision, and a key you invent cannot be shadowed by
someone else's. This does mean ``producer`` must be a non-empty string on
every call that supplies an idempotency key — the server rejects a key
with no producer as a 400 ``contract_violation`` rather than silently
treating it as unscoped.

Which calls take an idempotency key
-------------------------------------
``append_evidence`` requires one. ``create_run``, ``create_experiment``,
``record_promotion``, ``record_health`` and ``register_artifact`` accept
one optionally: pass it and your retry of the same call is safe (the
stored record comes back, no duplicate row); omit it and you get an
ordinary create. A materially different body under a key you already used
is a 409 ``idempotency_conflict``, never a silent overwrite.

``register_artifact`` needs its ``producer`` passed explicitly alongside
the key (it travels as the ``X-CER-Producer`` header). Unlike the other
calls there is no producer anywhere else in an artifact registration to
take it from — the request body is the artifact's raw bytes, and an
``ArtifactRecord`` has no producer field. Note that CER deduplicates an
artifact's *bytes* by content hash regardless: without a key, retrying a
registration stores the blob once but leaves you two ``artifact_id`` values
for one logical registration, with nothing to tell you they are the same
submission.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Union

import httpx

from cer.contract.enums import RunStatus
from cer.contract.errors import (
    ArtifactStoreError,
    CERError,
    ChecksumMismatchError,
    ContractViolationError,
    IdempotencyConflictError,
    IdentityError,
    ImmutabilityError,
    IncompatibleSchemaVersionError,
    MetadataStoreError,
    NotFoundError,
    UnknownEvidenceTypeError,
    ValidationError,
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
from cer.contract.version import CONTRACT_VERSION

CONTRACT_VERSION_HEADER = "X-CER-Contract-Version"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

#: code -> exception type, most-specific entries only need to be here once
#: since lookup is a direct dict hit on the server's stable `code` string,
#: not an isinstance walk.
_CODE_TO_EXCEPTION: dict[str, type[CERError]] = {
    exc.code: exc  # type: ignore[misc]
    for exc in (
        ContractViolationError,
        IncompatibleSchemaVersionError,
        UnknownEvidenceTypeError,
        IdentityError,
        ValidationError,
        ImmutabilityError,
        IdempotencyConflictError,
        NotFoundError,
        MetadataStoreError,
        ArtifactStoreError,
        ChecksumMismatchError,
        CERError,
    )
}


class CERClientError(CERError):
    """Fallback for a CER error response whose `code` this client build
    does not recognise (e.g. a newer server), or a malformed error body."""

    code = "cer_client_error"


def _raise_for_error_response(response: httpx.Response) -> None:
    try:
        body = response.json()
        code = body.get("code")
        message = body.get("message", "")
        request_id = body.get("request_id")
    except Exception:
        raise CERClientError(
            f"CER request failed with status {response.status_code} and a "
            f"non-JSON or malformed error body: {response.text[:500]!r}"
        ) from None

    exc_type = _CODE_TO_EXCEPTION.get(code, CERClientError)
    full_message = message if not request_id else f"{message} (request_id={request_id})"
    raise exc_type(full_message, code=code or exc_type.code)


class CERClient:
    """Synchronous typed client for the CER v1 HTTP API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        contract_version: str = CONTRACT_VERSION,
        timeout: float = 10.0,
        transport: Optional[httpx.BaseTransport] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        """Build a client against ``base_url`` (a real/synchronous
        ``httpx.BaseTransport`` may be supplied via ``transport`` — e.g. to
        point at a Unix socket).

        ``http_client`` lets a caller (tests, mainly) hand in an
        already-constructed synchronous ``httpx.Client`` — e.g.
        ``starlette.testclient.TestClient(app)`` — instead. Pydantic's ASGI
        transport is async-only and cannot back a synchronous
        ``httpx.Client`` directly; ``TestClient`` is the standard sync
        bridge for testing an ASGI app in-process, so tests should build
        one and pass it here rather than trying to wire an ASGI transport
        by hand.
        """
        self._contract_version = contract_version
        if http_client is not None:
            self._client = http_client
        else:
            self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CERClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- internal request helper -----------------------------------------

    def _headers(self, *, idempotency_key: Optional[str] = None) -> dict[str, str]:
        headers = {CONTRACT_VERSION_HEADER: self._contract_version}
        if idempotency_key:
            headers[IDEMPOTENCY_KEY_HEADER] = idempotency_key
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        content: Optional[bytes] = None,
    ) -> httpx.Response:
        response = self._client.request(
            method, path, json=json_body, params=params, headers=headers, content=content
        )
        if response.status_code >= 400:
            _raise_for_error_response(response)
        return response

    # --- discovery / health --------------------------------------------

    def get_version(self) -> dict:
        return self._request("GET", "/v1/version").json()

    def health(self) -> dict:
        return self._request("GET", "/health").json()

    def ready(self) -> dict:
        return self._request("GET", "/ready").json()

    # --- strategy / strategy version -------------------------------------

    def register_strategy(
        self, strategy_id: str, name: str, thesis: str, *, created_at: Optional[datetime] = None
    ) -> Strategy:
        body = {"strategy_id": strategy_id, "name": name, "thesis": thesis}
        if created_at is not None:
            body["created_at"] = created_at.isoformat()
        resp = self._request("POST", "/v1/strategies", json_body=body, headers=self._headers())
        return Strategy.model_validate(resp.json())

    def register_strategy_version(
        self,
        strategy_id: str,
        strategy_version: str,
        git_repo: str,
        git_commit: str,
        *,
        created_at: Optional[datetime] = None,
        notes: Optional[str] = None,
    ) -> StrategyVersion:
        body: dict[str, Any] = {
            "strategy_version": strategy_version,
            "git_repo": git_repo,
            "git_commit": git_commit,
            "notes": notes,
        }
        if created_at is not None:
            body["created_at"] = created_at.isoformat()
        resp = self._request(
            "POST",
            f"/v1/strategies/{strategy_id}/versions",
            json_body=body,
            headers=self._headers(),
        )
        return StrategyVersion.model_validate(resp.json())

    # --- experiment / run -------------------------------------------------

    def create_experiment(
        self,
        objective: str,
        producer: str,
        *,
        strategy_id: Optional[str] = None,
        strategy_version: Optional[str] = None,
        created_at: Optional[datetime] = None,
        idempotency_key: Optional[str] = None,
    ) -> Experiment:
        """Create an experiment. ``idempotency_key`` is optional; when given
        it is scoped to ``producer`` server-side, so your retry of the same
        call returns the same experiment instead of minting a second one."""
        body: dict[str, Any] = {
            "objective": objective,
            "producer": producer,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
        }
        if created_at is not None:
            body["created_at"] = created_at.isoformat()
        resp = self._request(
            "POST",
            "/v1/experiments",
            json_body=body,
            headers=self._headers(idempotency_key=idempotency_key),
        )
        return Experiment.model_validate(resp.json())

    def create_run(
        self,
        experiment_id: str,
        producer: str,
        *,
        idempotency_key: Optional[str] = None,
        status: RunStatus = RunStatus.OPEN,
        started_at: Optional[datetime] = None,
        producer_version: Optional[str] = None,
        git_repo: Optional[str] = None,
        git_commit: Optional[str] = None,
        dataset_id: Optional[str] = None,
        dataset_version: Optional[str] = None,
        dataset_ref: Optional[str] = None,
        config_hash: Optional[str] = None,
        config_ref: Optional[str] = None,
        environment: Optional[str] = None,
    ) -> Run:
        """Create a run. If ``idempotency_key`` is given, it is scoped to
        ``producer`` server-side (see the module docstring) — your retry
        of the same key is idempotent, and other producers' use of the
        same key string never collides with yours."""
        body: dict[str, Any] = {
            "producer": producer,
            "status": status.value if isinstance(status, RunStatus) else status,
            "producer_version": producer_version,
            "git_repo": git_repo,
            "git_commit": git_commit,
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "dataset_ref": dataset_ref,
            "config_hash": config_hash,
            "config_ref": config_ref,
            "environment": environment,
        }
        if started_at is not None:
            body["started_at"] = started_at.isoformat()
        resp = self._request(
            "POST",
            f"/v1/experiments/{experiment_id}/runs",
            json_body=body,
            headers=self._headers(idempotency_key=idempotency_key),
        )
        return Run.model_validate(resp.json())

    def close_run(
        self,
        run_id: str,
        *,
        status: RunStatus = RunStatus.CLOSED,
        ended_at: Optional[datetime] = None,
    ) -> Run:
        body: dict[str, Any] = {"status": status.value if isinstance(status, RunStatus) else status}
        if ended_at is not None:
            body["ended_at"] = ended_at.isoformat()
        resp = self._request(
            "POST", f"/v1/runs/{run_id}/close", json_body=body, headers=self._headers()
        )
        return Run.model_validate(resp.json())

    # --- evidence -----------------------------------------------------------

    def append_evidence(
        self,
        evidence_type: str,
        schema_version: int,
        producer: str,
        idempotency_key: str,
        **fields: Any,
    ) -> EvidenceRecord:
        """Append an evidence record. ``fields`` may include any other
        ``EvidenceCreateRequest`` field (strategy_id, run_id, metrics,
        verdict, notes, ...).

        ``idempotency_key`` is scoped to ``producer`` server-side (see the
        module docstring): your own retry of the same key is idempotent,
        and other producers may safely reuse the same key string."""
        body: dict[str, Any] = {
            "evidence_type": evidence_type,
            "schema_version": schema_version,
            "producer": producer,
            "idempotency_key": idempotency_key,
            **fields,
        }
        resp = self._request(
            "POST",
            "/v1/evidence",
            json_body=body,
            headers=self._headers(idempotency_key=idempotency_key),
        )
        return EvidenceRecord.model_validate(resp.json())

    def get_evidence(self, evidence_id: str) -> EvidenceRecord:
        resp = self._request(
            "GET", f"/v1/evidence/{evidence_id}", headers=self._headers()
        )
        return EvidenceRecord.model_validate(resp.json())

    def query_evidence(self, **filters: Any) -> list[EvidenceRecord]:
        resp = self._request("GET", "/v1/evidence", params=filters, headers=self._headers())
        return [EvidenceRecord.model_validate(item) for item in resp.json()]

    # --- artifacts ------------------------------------------------------

    def register_artifact(
        self,
        data: bytes,
        filename: str,
        *,
        content_type: str = "application/octet-stream",
        declared_sha256: Optional[str] = None,
        run_id: Optional[str] = None,
        evidence_id: Optional[str] = None,
        producer: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> ArtifactRecord:
        """Register an artifact's bytes and return its ``ArtifactRecord``.

        ``idempotency_key`` is optional; when given, ``producer`` must be
        given too (the key is scoped to it — see the module docstring) and
        a retry returns the stored record with the same ``artifact_id``
        instead of registering the same bytes a second time.
        """
        headers = self._headers(idempotency_key=idempotency_key)
        headers["Content-Type"] = content_type
        headers["X-CER-Filename"] = filename
        if declared_sha256:
            headers["X-CER-Declared-Sha256"] = declared_sha256
        if run_id:
            headers["X-CER-Run-Id"] = run_id
        if evidence_id:
            headers["X-CER-Evidence-Id"] = evidence_id
        if producer:
            headers["X-CER-Producer"] = producer
        resp = self._request("POST", "/v1/artifacts", content=data, headers=headers)
        return ArtifactRecord.model_validate(resp.json())

    def get_artifact_metadata(self, artifact_id: str) -> ArtifactRecord:
        resp = self._request(
            "GET", f"/v1/artifacts/{artifact_id}", headers=self._headers()
        )
        return ArtifactRecord.model_validate(resp.json())

    def download_artifact(self, artifact_id: str) -> bytes:
        resp = self._request(
            "GET", f"/v1/artifacts/{artifact_id}/download", headers=self._headers()
        )
        return resp.content

    def query_artifacts(self, **filters: Any) -> list[ArtifactRecord]:
        resp = self._request("GET", "/v1/artifacts", params=filters, headers=self._headers())
        return [ArtifactRecord.model_validate(item) for item in resp.json()]

    def attach_artifact(
        self,
        artifact_id: str,
        *,
        evidence_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> ArtifactRecord:
        body = {"evidence_id": evidence_id, "run_id": run_id}
        resp = self._request(
            "POST",
            f"/v1/artifacts/{artifact_id}/attach",
            json_body=body,
            headers=self._headers(),
        )
        return ArtifactRecord.model_validate(resp.json())

    # --- promotion history ------------------------------------------------

    def record_promotion(
        self,
        strategy_id: str,
        strategy_version: str,
        from_state: str,
        to_state: str,
        authority: str,
        producer: str,
        evidence_ids: list[str],
        reason: str,
        *,
        at_utc: Optional[datetime] = None,
        idempotency_key: Optional[str] = None,
    ) -> PromotionTransition:
        """Record a promotion transition. ``idempotency_key`` is optional;
        when given it is scoped to ``producer``, so a retry re-reads the
        transition you already recorded rather than recording the same
        intended transition a second time."""
        body: dict[str, Any] = {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "from_state": from_state,
            "to_state": to_state,
            "authority": authority,
            "producer": producer,
            "evidence_ids": evidence_ids,
            "reason": reason,
        }
        if at_utc is not None:
            body["at_utc"] = at_utc.isoformat()
        resp = self._request(
            "POST",
            "/v1/promotions",
            json_body=body,
            headers=self._headers(idempotency_key=idempotency_key),
        )
        return PromotionTransition.model_validate(resp.json())

    def query_promotions(self, **filters: Any) -> list[PromotionTransition]:
        resp = self._request("GET", "/v1/promotions", params=filters, headers=self._headers())
        return [PromotionTransition.model_validate(item) for item in resp.json()]

    # --- strategy health history ------------------------------------------

    def record_health(
        self,
        strategy_id: str,
        strategy_version: str,
        health_state: str,
        producer: str,
        reason: str,
        evidence_ids: list[str],
        *,
        observed_at_utc: Optional[datetime] = None,
        idempotency_key: Optional[str] = None,
        **metrics: Union[float, dict, None],
    ) -> StrategyHealthRecord:
        """Record a strategy-health observation. ``idempotency_key`` is
        optional; when given it is scoped to ``producer`` and makes a retry
        of the same observation safe."""
        body: dict[str, Any] = {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "health_state": health_state,
            "producer": producer,
            "reason": reason,
            "evidence_ids": evidence_ids,
            **metrics,
        }
        if observed_at_utc is not None:
            body["observed_at_utc"] = observed_at_utc.isoformat()
        resp = self._request(
            "POST",
            "/v1/health-records",
            json_body=body,
            headers=self._headers(idempotency_key=idempotency_key),
        )
        return StrategyHealthRecord.model_validate(resp.json())

    def query_health(self, **filters: Any) -> list[StrategyHealthRecord]:
        resp = self._request(
            "GET", "/v1/health-records", params=filters, headers=self._headers()
        )
        return [StrategyHealthRecord.model_validate(item) for item in resp.json()]
