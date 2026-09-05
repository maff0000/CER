# ADR-001 — Cross-Producer Evidence Reference Resolution

**Status:** APPROVED
**Scope:** Programme-level CER evidence-reference semantics
**Decision authority:** Central architecture
**Applies from:** Future versioned CER producer-contract change after v1.0.0
**CER v1 status:** Unchanged; PRODUCT_GREEN remains accepted

> Reproduced here because it governs CER's evidence-reference semantics and
> supersedes the open question previously recorded in
> `docs/KNOWN-LIMITATIONS.md`. This ADR is programme-level and applies
> beyond CER; the authoritative copy is held by central architecture, and
> this copy must not be edited to reflect CER-local decisions.

## Context

CER is the canonical empirical evidence and lineage registry for THE GOAL.
Independent producers including HSA, APOLLO/ATHENA, NEO, PLUTUS and TRON may
legitimately publish related records asynchronously.

Strict write-time existence checks for every semantic evidence reference
would impose artificial producer ordering, create brittle retry coupling,
and reject otherwise valid records merely because supporting evidence has
not arrived yet.

At the same time, accepting a structurally valid reference must not cause
that reference to be treated as verified supporting evidence before its
target exists and is valid.

The architectural distinction is therefore:

> **Reference accepted does not mean reference trusted.**

## Decision

### 1. Asynchronous semantic references may temporarily dangle

A structurally valid `evidence_id` reference MAY be accepted before its
target evidence exists.

The immutable originating record MUST be preserved exactly as submitted.

CER MUST NOT rewrite that originating record merely because
target-resolution state later changes.

### 2. Resolution state is derived state

CER MUST track reference-resolution state separately from the originating
immutable record.

Initial canonical states:

* `UNRESOLVED`
* `RESOLVED`
* `INVALID_TARGET`

`RETIRED_TARGET` is explicitly deferred and MUST NOT be introduced until a
future evidence-lifecycle requirement justifies it.

When target evidence later arrives, CER MAY update derived/reference-index
state from `UNRESOLVED` to `RESOLVED`.

That update is not mutation of the originating immutable record.

### 3. Accepted references are not automatically trusted

An unresolved reference MUST NOT count as verified supporting evidence for:

* strategy promotion;
* strategy-health conclusions;
* assurance decisions;
* or any other decision whose semantics require trusted supporting evidence.

Consumers MUST be able to distinguish citation presence from citation
resolution/trust state.

### 4. Unresolved references remain visible

Unresolved references MUST remain visible, queryable and reportable.

References that can never resolve MUST remain explicitly identifiable.

CER MUST NOT silently discard, repair, reinterpret or treat such references
as trusted.

### 5. Referential integrity depends on relationship semantics

Strict referential integrity remains mandatory where causality requires an
existing target.

For example:

> "Attach artifact X to evidence Y now"

requires evidence `Y` to exist at transaction time.

The governing rule is:

> **Asynchronous semantic references may dangle temporarily.
> Transactional/causal references may not.**

### 6. Aging unresolved references becomes operational evidence

A future CER v1.x or later contract evolution SHOULD expose a query
equivalent to:

> all unresolved evidence references older than N hours/days

This enables operators to distinguish ordinary asynchronous arrival delays
from broken producer lineage.

This capability is not required to reopen CER v1.

## Versioning and compatibility

These semantics MUST be introduced only through an explicit versioned CER
producer-contract change.

Existing v1.0.0 immutable records MUST NOT be retroactively rewritten or
reinterpreted.

Any new:

* persisted resolution/index state;
* API fields;
* query endpoints;
* migrations;
* producer expectations;

must follow normal CER contract-versioning discipline.

## Consequences

This decision preserves producer independence while giving downstream
assurance consumers a reliable distinction between citation and trust.

It also preserves CER's core doctrine:

> **Incomplete information is represented explicitly rather than guessed,
> silently repaired or unnecessarily rejected.**

CER v1 remains PRODUCT_GREEN and requires no change under this ADR.

---

## CER v1.0.0 conformance

Verified against the delivered branch. Every clause that binds at v1 holds;
the clauses that do not are deferred by the ADR itself to a future
contract change.

| Clause | Requirement at v1 | v1.0.0 behaviour |
| --- | --- | --- |
| §1 asynchronous semantic references | MAY dangle | Promotion, health and `parent_evidence_ids` citations of unregistered evidence are accepted (`201`) |
| §4 visibility | Not discarded, repaired or reinterpreted | Citations are stored and returned verbatim, immutably |
| §5 transactional/causal references | MUST NOT dangle | Artifact attachment (at registration and via `/attach`), and evidence filed under a `run_id`/`experiment_id`, all reject a nonexistent target with `404` |
| §2 resolution state | Deferred to a future contract change | Not present |
| §3 trust distinction | Deferred to a future contract change | Not present |
| §6 aging query | Deferred; explicitly not required to reopen v1 | Not present |

### Operational implication for v1 consumers

Because §2 and §3 are deferred, a v1.0.0 consumer **cannot** distinguish a
resolved citation from an unresolved one through the API. Under §3, an
unresolved reference must not count as verified supporting evidence — so at
v1.0.0 a consumer must not treat any `evidence_ids` citation on a promotion
or health record as verified merely because the record exists.

Citations at v1 carry provenance, not assurance. A consumer needing trust
must resolve the cited ids itself via `GET /v1/evidence/{id}`, or wait for
the contract change that introduces resolution state.

This does not reopen v1: the ADR states that v1 requires no change, and CER
has never claimed these citations were verified.
