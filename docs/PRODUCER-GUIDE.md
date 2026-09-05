# CER Producer Integration Guide

Audience: engineers on a producer team (HSA, APOLLO, NEO, or a future
producer) integrating against the Canonical Evidence Registry (CER) v1
HTTP API. It assumes you have read `PID.md` for the *why*; this document
is the *how*.

Every example below was run against the real API (`cer.api.app.create_app`
wired to the real `SQLiteMetadataStore` and `FilesystemArtifactStore`) as
part of writing this guide — not written from memory. If you copy an
example verbatim it will work.

## 1. What CER is, from a producer's point of view

CER is the canonical store for empirical evidence, experiment/run
lineage, artifact references, promotion history and strategy-health
history. It does not generate strategies, run backtests, or make trading
decisions — your producer does that; CER just makes sure the result is
recorded once, correctly, and is queryable later without anyone searching
files or chat history.

You talk to CER over HTTP (`/v1/...`), either directly or through
`cer.client.CERClient`, a thin synchronous wrapper that gives you one
typed method per v1 capability and translates CER's structured error
responses back into Python exceptions. This guide uses `CERClient`
throughout; the raw HTTP shape is shown wherever it differs (mainly
artifact upload).

## 2. Checking compatibility: `/v1/version`

Before sending anything else, a producer should check the server's
contract/schema version. This endpoint is deliberately **not**
gated behind the contract-version header — you need to be able to call it
before you know what to send:

```python
import httpx

info = httpx.get("http://cer.internal:8000/v1/version").json()
# {'contract_version': '1.0.0', 'schema_version': 1, 'supported_schema_versions': [1]}
```

Every other `/v1/...` call must carry `X-CER-Contract-Version` set to a
version whose **major** component matches the server's `contract_version`
(a mismatch fails loudly — see §10). `CERClient` does this automatically,
pinned to the contract version the `cer` package you imported was built
against:

```python
from cer.client import CERClient

client = CERClient(base_url="http://cer.internal:8000")
```

## 3. Register your strategy identity once

`strategy_id` is a caller-supplied logical identity (letters/digits/`.`/`_`/`-`,
never reused for a different thesis); `strategy_version` is immutable once
registered — a logic change means a new version, not an edit.

```python
strategy = client.register_strategy(
    "EMA_PULLBACK", "EMA Pullback", "mean reversion to the 21 EMA"
)
sv = client.register_strategy_version(
    "EMA_PULLBACK", "v1.0.0",
    git_repo="git@example.com/strats.git",
    git_commit="a" * 40,
)
```

Both calls are safe to repeat with the *same* content (they return the
existing record); repeating with *different* content for the same id
raises `ImmutabilityError` (409) — see §9. "Same content" is compared
field-for-field, excluding the server-assigned `created_at` — you never
need to pass `created_at` yourself for this comparison to succeed, and an
otherwise-identical retry that omits it (as the examples above do) returns
the existing record rather than raising an error. A retry is only ever an
`ImmutabilityError` when a field you actually author — `name`, `thesis`,
`git_commit`, `notes`, and so on — genuinely differs from what's stored.

## 4. Experiment and run

An experiment groups one research/validation objective; a run is one
execution of it.

```python
experiment = client.create_experiment(
    "validate EMA pullback on EURUSD H1", "HSA",
    strategy_id="EMA_PULLBACK", strategy_version="v1.0.0",
)

run = client.create_run(
    experiment.experiment_id, "HSA",
    idempotency_key="hsa-2026-09-04-run1",
    dataset_id="eurusd_h1", environment="dev",
)
```

`run.provenance_completeness` and `run.missing_provenance` tell you
immediately whether this run has everything CER considers reproducibility
context (`producer_version`, `git_repo`, `git_commit`, `dataset_id`,
`dataset_version`, `dataset_ref`, `config_hash`, `config_ref`,
`environment`) — see §7.

Close the run when it's done:

```python
client.close_run(run.run_id)  # defaults to status=CLOSED, ended_at=now
```

## 5. Idempotency keys — and why they're scoped to *your* producer name

An idempotency key makes a retry safe: replaying the same call with the
same key and the same content returns the original record instead of
creating a duplicate. Coverage differs by endpoint, and getting this wrong
is exactly the kind of mistake that corrupts a history other teams read —
so know where each call you make falls in the table below:

| Endpoint | `CERClient` method | Key | What happens if you omit it |
|---|---|---|---|
| `POST /v1/evidence` | `append_evidence` | **Required** | The call is rejected (`contract_violation`, 400) — there is no key-less path |
| `POST /v1/experiments/{id}/runs` | `create_run` | Optional, honoured when supplied | Every call creates a new run |
| `POST /v1/experiments` | `create_experiment` | Optional, honoured when supplied | Every call creates a new experiment |
| `POST /v1/promotions` | `record_promotion` | Optional, honoured when supplied | Every call records a new transition |
| `POST /v1/health-records` | `record_health` | Optional, honoured when supplied | Every call records a new health observation |
| `POST /v1/artifacts` | `register_artifact` | Optional, producer-scoped, honoured when supplied | Every call creates a new artifact *record* (see below — the underlying blob is not duplicated) |

**On the optional endpoints, omitting the key does not make the call
"unsafe" in some vague sense — it means a retry (a timeout you retried, a
message your queue redelivered, a script you re-ran by hand) creates a
second record.** For `create_experiment` and `record_health` that's
usually just noise you can filter around. For `record_promotion` it's
worse: promotion history is a lifecycle a human or NEO reads to answer
"what state is this strategy in and when did it get there" — a
duplicated transition (the same `DRAFT` -> `IMPLEMENTED` recorded twice)
corrupts that history for every consumer of it, not just the caller who
retried. If your promotion-recording code path can ever be retried
(anything calling over HTTP can), pass an idempotency key.

**`register_artifact` needs calling out separately, because "duplicate"
means something narrower there than it does everywhere else in this
table.** The artifact *blob* is always content-addressed and deduplicated
by CER regardless of whether you pass a key — uploading the same bytes
twice never stores the bytes twice. What an omitted key duplicates is the
artifact *record* — the row that gives those bytes their own `artifact_id`,
`registered_at`, and `run_id`/`evidence_id` attachment. Retry a
`register_artifact` call with no key and you get two `artifact_id`s
pointing at the same underlying blob, which is exactly the kind of
surprise a producer would otherwise assume can't happen ("it's
content-addressed, so retries must be safe" — the blob, yes; the record,
no). Pass an idempotency key when the registration call might be retried
and you want a retry to hand back the original record instead of minting
a second one.

In every case, a retry with the **same** producer, the **same** key, and
an **identical** body returns the stored record — never a duplicate,
never an error. Server-assigned fields (creation timestamps CER stamps
itself when you don't supply one) are excluded from that comparison, so a
genuine retry is never a false conflict on account of time having passed;
anything you author yourself — including `observed_at_utc` — is part of
the compared content, so changing it between calls with the same key is a
real conflict, not a retry.

For example, `create_run`:

```python
run1 = client.create_run(experiment.experiment_id, "HSA", idempotency_key="k1", environment="dev")
run2 = client.create_run(experiment.experiment_id, "HSA", idempotency_key="k1", environment="dev")
assert run2.run_id == run1.run_id  # same record, not a new one
```

A retry with the same key but **different** content raises
`IdempotencyConflictError` (409) — CER refuses to silently overwrite or
guess which body was "right":

```python
from cer.contract.errors import IdempotencyConflictError

try:
    client.create_run(experiment.experiment_id, "HSA", idempotency_key="k1", environment="prod")
except IdempotencyConflictError:
    ...  # same key, different environment -> conflict, not a silent overwrite
```

**The key is scoped to `(producer, idempotency_key)`, not the key alone.**
Two different producers may use the exact same key string — e.g. both
happening to key on today's date — without colliding:

```python
hsa_ev = client.append_evidence("BACKTEST", 1, "HSA", "2026-09-04-daily-sweep", verdict="PROMISING")
neo_ev = client.append_evidence("NEO_OBSERVATION", 1, "NEO", "2026-09-04-daily-sweep",
                                 notes={"observation": "unrelated to HSA's sweep"})
assert hsa_ev.evidence_id != neo_ev.evidence_id  # no collision across producers
```

This means: **you never need to coordinate key choice with other
producers**, but you must always pass a real, non-empty `producer` name
whenever you pass an idempotency key — supplying a key with an empty
producer is itself rejected as a `contract_violation` (400), not silently
treated as an unscoped key.

A natural, collision-resistant key for most producers is something derived
from your own run/date/parameter identity (e.g.
`f"{producer}-{run_date}-{symbol}-{param_hash}"`) — you don't need it to be
globally unique, only unique *to you*, and only for submissions you
actually want treated as the same logical event on retry.

## 6. Appending evidence

`evidence_type` must be one of the 20 controlled classes in `PID.md`
(`BACKTEST`, `WALK_FORWARD`, `NEO_OBSERVATION`, `DRIFT_ALERT`, ... — see
`cer.contract.enums.EvidenceType`). An unrecognised type fails loudly
(`unknown_evidence_type`, 400) rather than being coerced or accepted as an
ad-hoc string:

```python
evidence = client.append_evidence(
    "BACKTEST", schema_version=1, producer="HSA", idempotency_key="hsa-2026-09-04-ev1",
    strategy_id="EMA_PULLBACK", strategy_version="v1.0.0",
    experiment_id=experiment.experiment_id, run_id=run.run_id,
    producer_version="1.4.2",
    observed_at_utc="2026-09-04T00:00:00Z",
    git_repo="git@example.com/strats.git", git_commit="a" * 40,
    dataset_id="eurusd_h1_2020_2024", dataset_version="3",
    dataset_ref="s3://datasets/eurusd_h1_2020_2024.parquet",
    config_hash="cfg_" + "b" * 16, config_ref="configs/ema_pullback.yaml",
    environment="dev",
    instruments=["EURUSD"], timeframes=["H1"],
    metrics={"sharpe": 1.42, "trades": 318, "win_rate": 0.54},
    verdict="PROMISING", status="COMPLETE",
    regime_tags=["trend"],
    notes={"observation": "consistent across regimes"},
)
```

`schema_version` (an int, currently `1`) is the shape version of the
evidence record itself. An unsupported value fails loudly as
`incompatible_schema_version` (400) — this is independent of the
`X-CER-Contract-Version` header, which governs the *API contract*, not the
record shape.

`idempotency_key` is **required** on every `append_evidence` call — CER
has no other way to make ingestion retry-safe for a record that generates
its own identity. Pass it as the `idempotency_key` argument (`CERClient`
sends it as the `Idempotency-Key` header); if you're calling the raw HTTP
API you may send it as either the header or the `idempotency_key` body
field, but if you send both they must agree.

## 7. Provenance completeness — never fabricate a placeholder

Every `EvidenceRecord` (and `Run`) gets `provenance_completeness`
(`COMPLETE`/`INCOMPLETE`) and `missing_provenance` (the exact list of
field names that are absent) computed automatically by CER — you never set
these yourself, and CER never guesses a value on your behalf.

If your producer genuinely doesn't have a piece of context — NEO's
always-on surveillance typically has no `run_id`, no `dataset_id`, no
`config_hash`, because it isn't executing a backtest — **do not invent a
placeholder value** ("N/A", `"unknown"`, copying a stale value from a
previous record) to make the record look complete. Leave the field unset;
CER will record it as `INCOMPLETE` and name exactly what's missing. A
fabricated placeholder is worse than an honest gap: it looks like real
provenance to every future consumer of that record.

```python
neo_evidence = client.append_evidence(
    "NEO_OBSERVATION", 1, "NEO", "neo-2026-09-04-obs1",
    strategy_id="EMA_PULLBACK", strategy_version="v1.0.0",
    notes={"observation": "trigger rate elevated vs. baseline"},
    # no run_id, no dataset_id, no config_hash, no git_commit: genuinely absent
)
assert neo_evidence.provenance_completeness == "INCOMPLETE"
assert "dataset_id" in neo_evidence.missing_provenance
assert "git_commit" in neo_evidence.missing_provenance
# strategy_id/strategy_version/notes WERE supplied, so they are not listed
assert "strategy_id" not in neo_evidence.missing_provenance
```

The record is still accepted (201), still fully queryable, and still a
first-class evidence record — `INCOMPLETE` is a queryable fact about it,
not a rejection.

## 8. Artifacts

An artifact is registered by sending its **raw bytes as the request
body** (not multipart) with metadata in headers. `CERClient.register_artifact`
wraps this:

```python
artifact = client.register_artifact(
    b"equity_date,equity\n2026-01-01,10000\n",
    "equity_curve.csv",
    content_type="text/csv",
    run_id=run.run_id,
    evidence_id=evidence.evidence_id,
    idempotency_key="hsa-2026-09-04-equity-curve",  # optional -- see §5
)
```

Over raw HTTP, the same call is:

```
POST /v1/artifacts
X-CER-Contract-Version: 1.0.0
Content-Type: text/csv
X-CER-Filename: equity_curve.csv
X-CER-Run-Id: run_...            (optional, immediate attachment)
X-CER-Evidence-Id: ev_...        (optional, immediate attachment)
X-CER-Declared-Sha256: <hex>     (optional integrity check -- see below)
Idempotency-Key: <key>           (optional, producer-scoped -- see §5)

<raw bytes as the body>
```

CER computes the SHA-256 of the bytes itself and stores the artifact
content-addressed. If you pass `X-CER-Declared-Sha256`, CER verifies it
against the bytes it actually received and rejects a mismatch loudly
(`checksum_mismatch`, 422) **without storing anything** — neither a blob
nor an index entry is written on a checksum failure. Passing a declared
checksum is optional but recommended: it catches a truncated upload or a
transport bug at the producer's own boundary, rather than silently
registering corrupted evidence.

```python
data = client.download_artifact(artifact.artifact_id)  # byte-identical to what you sent
metadata = client.get_artifact_metadata(artifact.artifact_id)  # sha256, size, content_type, filename
```

An artifact can be registered without attaching it to a run/evidence at
creation time, and attached later:

```python
artifact = client.register_artifact(b"...", "report.pdf", content_type="application/pdf")
client.attach_artifact(artifact.artifact_id, run_id=run.run_id, evidence_id=evidence.evidence_id)
```

Attachment is one-way: once an artifact is attached to a `run_id` or
`evidence_id`, attaching it to a *different* one raises `ImmutabilityError`
(409) rather than silently relinking it.

`GET /v1/artifacts/{artifact_id}` (`client.get_artifact_metadata`) is the
authoritative view of an artifact's *current* lineage: it always reflects
the latest `run_id`/`evidence_id` attachment, whether that attachment was
made at registration (via `X-CER-Run-Id`/`X-CER-Evidence-Id`) or later via
`/attach`.

```python
art2 = client.register_artifact(b"...", "report.pdf", content_type="application/pdf")
assert client.get_artifact_metadata(art2.artifact_id).run_id is None  # not attached yet

client.attach_artifact(art2.artifact_id, run_id=run.run_id, evidence_id=evidence.evidence_id)
current = client.get_artifact_metadata(art2.artifact_id)
assert current.run_id == run.run_id and current.evidence_id == evidence.evidence_id
```

`client.query_artifacts(run_id=...)` / `client.query_artifacts(evidence_id=...)`
remain the right tool for a different question — not "what is this one
artifact currently attached to" (use the GET above for that) but "find
every artifact attached to this run/evidence record", e.g. to list all the
files a given run produced.

## 9. Immutability rules — what can never be rewritten

* **Strategies and strategy versions**: re-registering the same
  `strategy_id` / `(strategy_id, strategy_version)` with identical content
  is a no-op that returns the existing record; with *different* content it
  raises `ImmutabilityError` (409) — see §3 for what counts as "identical"
  (the server-assigned `created_at` is excluded from that comparison). A
  strategy version is never edited in place — a logic change is a new
  version.
* **Evidence**: once written, an evidence record's content never changes.
  A same-key-same-body retry returns the original; a same-key-different-body
  retry is `IdempotencyConflictError` (409); a direct clash on
  `evidence_id` itself with different content is `ImmutabilityError` (409).
  Historical evidence is never rewritten to match a newer strategy version.
* **Artifacts**: a blob is written once (content-addressed by SHA-256);
  writing different bytes that hash to the same digest path is refused,
  not overwritten. An artifact's `run_id`/`evidence_id` attachment, once
  set, cannot be changed to a different value (`ImmutabilityError`, 409) —
  only set once, from unset.
* **Promotion transitions and health records**: append-only. There is no
  update or delete endpoint for either; correcting history means recording
  a new transition/observation, never editing an old one.

## 10. Error codes a producer must handle

CER never returns a bare stack trace for a request it understands as
invalid or a dependency it knows is down. Every error response has the
same shape: `{"code": "...", "message": "...", "request_id": "..."}`.
**Handle errors by `code`, never by parsing `message`** — `message` is
for a human, `code` is the stable contract. `CERClient` raises the
matching exception from `cer.contract.errors` for each code below.

| `code` | HTTP status | Meaning | What a producer should do |
|---|---|---|---|
| `contract_violation` | 400 | A structural rule was broken (missing contract-version header, idempotency key with no producer, header/body idempotency-key mismatch, missing required idempotency key) | Fix the request; not retriable as-is |
| `incompatible_schema_version` | 400 | `X-CER-Contract-Version` or `schema_version` is missing, malformed, or a major/version mismatch | Check `/v1/version`; upgrade your `cer` client or pin a supported version |
| `unknown_evidence_type` | 400 | `evidence_type` is not one of the controlled classes | Use a value from `cer.contract.enums.EvidenceType`; do not invent new ad-hoc types |
| `identity_error` | 400 | A `strategy_id`/`strategy_version` (or similar) has an invalid shape | Fix the identifier — letters/digits/`.`/`_`/`-` only, no whitespace |
| `validation_error` | 400 or 422 | A field failed model validation (e.g. a naive/non-UTC datetime) — 400 when CER's own contract model rejects it, 422 when the request body itself doesn't match the expected shape (missing/extra/wrong-typed field) | Fix the field; always send timezone-aware UTC datetimes, never send unrecognised fields |
| `checksum_mismatch` | 422 | `X-CER-Declared-Sha256` didn't match the received bytes | Nothing was stored; re-upload, or drop the declared checksum if you don't actually know it |
| `idempotency_conflict` | 409 | Same `(producer, idempotency_key)` was already used with different content | This is a real conflict, not a transient error — do not blindly retry; use a new key for genuinely new content |
| `immutability_violation` | 409 | An attempt to change an already-written immutable record (different content on re-registration, relinking an already-attached artifact, re-closing a closed run) | Do not retry as-is; if this is truly new information, create a new record instead of overwriting |
| `not_found` | 404 | The referenced id (evidence/run/artifact/experiment/strategy) doesn't exist | Check the id; don't assume eventual consistency will fix it |
| `metadata_store_error` | 503 | The metadata backend is unavailable | Transient — safe to retry with backoff; no record is created and nothing is retrievable (on `POST /v1/artifacts` specifically, the blob may already be written to the artifact store by the time metadata registration fails — no record exists to find it by, but it is not accurate to say nothing at all was written) |
| `artifact_store_error` | 503 | The artifact backend is unavailable | Transient — safe to retry with backoff; nothing was written |
| `http_error` | varies | A framework-level HTTP error (route not found, wrong method) | Check the URL/method |
| `internal_error` | 500 | An unexpected server-side failure | Retriable, but report it — this is not an expected error path |
| `cer_client_error` | n/a (client-side) | `CERClient` received a `code` it doesn't recognise (e.g. a newer server) or a malformed error body | Upgrade your `cer` client |

## 11. Queries

Every append-only collection is queryable, filtered and paginated
(`limit`/`offset` where applicable):

```python
client.query_evidence(strategy_id="EMA_PULLBACK", strategy_version="v1.0.0")
client.query_evidence(run_id=run.run_id)
client.query_evidence(evidence_type="BACKTEST", since="2026-01-01T00:00:00Z")
client.query_artifacts(evidence_id=evidence.evidence_id)
client.query_artifacts(run_id=run.run_id)
client.query_promotions(strategy_id="EMA_PULLBACK")
client.query_health(strategy_id="EMA_PULLBACK")
```

## 12. Promotion and health records (usually not the historical-evidence producer)

These are typically written by whichever authority (a human via a PL tool,
or NEO for health) is recording the outcome — but the shape is the same
regardless of who calls it. Every promotion transition requires at least
one supporting `evidence_id`; every health record requires a reason and at
least one supporting `evidence_id` — CER records observations, it does not
accept an assertion with no evidence behind it.

Both accept an optional `idempotency_key` (see §5) — pass one whenever
this call could ever be retried. This matters most for
`record_promotion`: promotion history is append-only and has no
dedup other than the key, so a retried call with no key records the same
transition twice.

```python
client.record_promotion(
    "EMA_PULLBACK", "v1.0.0", "DRAFT", "IMPLEMENTED",
    authority="PL", producer="HSA",
    evidence_ids=[evidence.evidence_id], reason="code complete",
    idempotency_key="hsa-2026-09-04-promote-implemented",
)

client.record_health(
    "EMA_PULLBACK", "v1.0.0", "HEALTHY", producer="NEO",
    reason="trigger rate within regime-conditioned expectation",
    evidence_ids=[evidence.evidence_id],
    win_rate=0.55, observed_trigger_rate=0.12, confidence=0.9,
    idempotency_key="neo-2026-09-04-health-check",
)
```

Metrics you don't have are simply omitted (`None`), never defaulted to
zero — a missing win rate and a 0% win rate are very different facts.

## 13. No database internals, ever

Nothing above touches SQLite, a file path, or a table name. That's
deliberate — the PID requires producers not depend on CER's storage
internals so the backend can be swapped later without breaking every
producer. If you find yourself reaching for `sqlite3` or the artifact
store's on-disk layout directly, stop and use the HTTP API/`CERClient`
instead.
