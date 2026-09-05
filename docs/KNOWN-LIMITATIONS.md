# CER v1 — Known limitations

Contract version `1.0.0`.

This is the honest record of what CER v1 deliberately does **not** do. Each
entry below is a real, reproducible behaviour of the shipped product, not a
suspicion or a to-do. None of them is a defect against the product contract
in `PID.md` — the reasoning for that is given in each case, so a future
reader can re-examine the judgment rather than having to take it on trust.

Read this alongside [`PRODUCER-GUIDE.md`](PRODUCER-GUIDE.md), which tells a
producer how to work within these limits today.

Entry 2 was an open product question when v1 was delivered. It has since
been settled by central architecture in
[`adr/ADR-001-cross-producer-evidence-reference-resolution.md`](adr/ADR-001-cross-producer-evidence-reference-resolution.md),
which ratifies v1's behaviour and defers the additions it calls for to a
future versioned contract change. v1 conforms as delivered.

**What is not on this list:** CER never acknowledges a write it then loses.
That property is the spine of the registry and it holds — a `201` means the
record is durable. Where the backing storage is genuinely unavailable, CER
refuses the write with a `503` and records nothing, rather than accepting it
and losing it. The limitations below are about what CER cannot *tell you*
afterwards, and about data lost by something other than CER.

---

## 1. Runs and experiments are write-only through the contract

### The behaviour

You can create an experiment and a run, but there is no way to read either
one back through the API afterwards. There is no `GET /v1/runs/{run_id}`,
no `GET /v1/experiments/{experiment_id}`, and no list endpoint for either.

This matters most for provenance completeness. A `Run` computes and
persists `provenance_completeness` and `missing_provenance` — the same
"say so rather than guess" mechanism evidence records use — and stores them
durably. But the only time the contract ever shows you those values is in
the `201` response body at creation. After that, the stored values are
unreachable through the API.

### Evidence

Against a running instance:

```
POST /v1/experiments            -> 201
POST /v1/experiments/{id}/runs  -> 201
    provenance_completeness = "INCOMPLETE"
    missing_provenance      = ["producer_version", "git_repo", "git_commit",
                               "dataset_id", "dataset_version", "dataset_ref",
                               "config_hash", "config_ref"]

GET  /v1/runs/{run_id}          -> 404   (no such route)
GET  /v1/experiments/{exp_id}   -> 404   (no such route)
GET  /v1/runs                   -> 404   (no such route)
GET  /v1/experiments            -> 405   (the path exists, but only for POST)
```

The same applies to strategies and strategy versions: both are registered
by `POST` and neither has any `GET`.

The one way to see a stored run or experiment again is to replay its
creation call with its original idempotency key, which returns the stored
record rather than creating a new one — verified byte-identical to the
original `201` body, `provenance_completeness` included. That is a retry
path being reused as a read. It requires that an idempotency key was
supplied originally, and that the caller still knows the key and the exact
original body; a materially different body under the same key is a `409`,
not a read.

### Why this is not a contract violation

The PID's required-v1-capability list names querying for evidence,
artifacts, promotion history and health history. It does not include
querying runs or experiments. Runs and experiments appear in that list only
as things a producer must be able to *create* ("create experiment",
"create run", "finalise/close run"), and all three work.

The PID's provenance-completeness requirement — "missing required
provenance must be explicit and queryable" — is scoped to evidence records,
and there it is fully satisfied: evidence is queryable by strategy,
version, run, experiment, type and time window, individually fetchable by
id, and carries its own `provenance_completeness` / `missing_provenance` on
every returned record. Runs compute the same fields as a deliberate
extension beyond what the contract asked for; the extension simply stops
short of a read path.

Nothing is lost, either. The values are computed and persisted; they are
just not exposed.

### What fixing it would require

Read endpoints that do not exist today: `GET /v1/runs/{run_id}`,
`GET /v1/experiments/{experiment_id}`, and list/filter endpoints for
strategies, strategy versions, experiments and runs. That is new capability
— new routes, new query methods on the metadata store interface, new
pagination and filter semantics, and the client and guide updated to match.
It is a feature, not a repair, and it should be decided on product need
rather than slipped in as a fix.

---

## 2. Evidence references on promotions and health records are not resolved

### The behaviour

`PromotionTransition.evidence_ids` and `StrategyHealthRecord.evidence_ids`
are accepted as given. CER validates that each id is *well-formed*, but
never checks that the evidence it names actually exists. A promotion or
health record citing evidence that was never registered is stored happily
and returned by the query endpoints exactly like any other.

CER is not uniformly permissive here, which is what makes the gap worth
knowing about. Several other cross-references **are** resolved at write
time, and are rejected with `404` when they do not exist.

The inconsistency is sharpest on health records, because
`StrategyHealthRecord.evidence_ids` carries `min_length=1` and the model is
explicit that a health observation must have an "evidence-backed reason".
CER enforces that you cite *something*; it does not check that the
something exists.

### Evidence

Against a running instance, using an evidence id that was never registered
(`ev_00000000000000000000000000000000`):

| Write | Result |
| --- | --- |
| `POST /v1/promotions` with that id in `evidence_ids` | **201 Created** |
| `POST /v1/health-records` with that id in `evidence_ids` | **201 Created** |
| `POST /v1/evidence` with a nonexistent `run_id` | 404 `not_found` |
| `POST /v1/evidence` with a nonexistent `experiment_id` | 404 `not_found` |
| `POST /v1/artifacts/{id}/attach` with that `evidence_id` | 404 `not_found` |
| `POST /v1/artifacts` with `X-CER-Evidence-Id` set to that id | 404 `not_found` |

Two further references are also unresolved, and belong in the same picture:

| Write | Result |
| --- | --- |
| `POST /v1/evidence` with a nonexistent id in `parent_evidence_ids` | **201 Created** |
| `POST /v1/evidence` with a `strategy_id` that was never registered | **201 Created** |

So the rule in practice is: *the identity a record is filed under is
checked; the identities a record cites are not.*

### Why this is not a contract violation

The PID requires that every promotion transition "preserve" its supporting
evidence references, and that health records support an "evidence-backed
reason". CER preserves both, exactly and immutably. The PID does not
require CER to resolve those references, and it explicitly does not make
CER the arbiter of whether a transition was justified — "CER records
transitions; it does not autonomously make promotion decisions."

### Ruled on by ADR-001

This was an open product question at the time CER v1 was delivered. It has
since been settled by central architecture in **ADR-001 — Cross-Producer
Evidence Reference Resolution** (`docs/adr/ADR-001-cross-producer-evidence-reference-resolution.md`),
which ratifies the split described above rather than closing it.

The governing rule:

> Asynchronous semantic references may dangle temporarily.
> Transactional/causal references may not.

That is precisely the behaviour recorded in the evidence table above, so
CER v1 conforms to the ADR as delivered and requires no change under it.
The ADR states this explicitly: *"CER v1 remains PRODUCT_GREEN and requires
no change under this ADR."*

What the ADR adds, deferred to a future versioned contract change after
v1.0.0, is the part v1 genuinely lacks: reference-resolution state tracked
separately from the immutable record (`UNRESOLVED` / `RESOLVED` /
`INVALID_TARGET`), a way for consumers to distinguish a citation being
present from that citation being resolved, and a query for unresolved
references older than a given age so operators can tell an ordinary arrival
delay from broken producer lineage.

The ADR's central distinction is worth stating in full, because it is what
makes the permissive behaviour correct rather than merely tolerated:

> **Reference accepted does not mean reference trusted.**

### What this means for a v1 consumer today

Because resolution state is deferred, a v1.0.0 consumer cannot tell a
resolved citation from an unresolved one through the API. Under the ADR an
unresolved reference must not count as verified supporting evidence for a
promotion, a health conclusion or any assurance decision.

So at v1.0.0: treat `evidence_ids` on a promotion or health record as a
citation, not as verification. If you need trust, resolve the ids yourself
with `GET /v1/evidence/{id}`. CER has never claimed these citations were
verified, and it does not make promotion decisions — the PID is explicit
that it records transitions rather than adjudicating them.

### What implementing the ADR would require

Not a repair, and not in v1: a persisted derived resolution index kept
separate from the immutable records, a schema migration, new API fields and
query endpoints, a contract version bump, and a decision on how the index is
populated for records already stored. All of it must follow normal CER
contract-versioning discipline, and existing v1.0.0 records must not be
retroactively rewritten or reinterpreted.

---

## 3. A lost metadata volume is not detected at startup

### The behaviour

CER keeps its two kinds of state on two independently-mountable volumes:
the SQLite metadata database, and the hash-addressed artifact root. They
can be lost independently.

The artifact side handles this. If the artifact volume is lost while the
service is stopped, startup does **not** quietly create an empty artifact
store over it. It distinguishes a genuine first deployment (no artifacts
have ever been registered) from a lost volume (the metadata store still
holds artifact records) by cross-checking the *other* volume, and in the
lost-volume case it refuses to initialise: `/ready` returns `503` naming
`artifact_store`, writes are refused, and the artifact metadata stays
readable so an operator can see exactly what was lost.

The metadata side has no equivalent check. Lose the metadata volume while
the service is stopped and, on restart, CER creates a fresh empty database,
applies its migrations, and reports itself completely healthy over a
registry that has lost every record it ever held.

The two stores then contradict each other about the same artifact.

### Evidence

Register an artifact and a piece of evidence, stop the service, delete
**only** the metadata volume, and start it again:

```
before:  GET /ready                        -> 200
         GET /v1/artifacts/{id}            -> 200
         GET /v1/artifacts/{id}/download   -> 200

(service stopped; metadata volume deleted; artifact volume untouched)

after:   GET /ready                        -> 200   {"status":"ok","failed_dependency":null}
         GET /v1/artifacts/{id}            -> 404   "artifact_id '...' does not exist"
         GET /v1/artifacts/{id}/download   -> 200   ...and returns the original bytes
         GET /v1/evidence/{id}             -> 404
         GET /v1/evidence                  -> 200   0 records
         GET /v1/artifacts                 -> 200   0 records
```

The `404`-then-`200` pair on the same artifact id is the clearest symptom:
the metadata path says the artifact was never registered, while the bytes
path serves it. Readiness reports `ok` throughout.

### Why this is not a durability breach

CER never acknowledged a write it then lost. Every record that returned
`201` was durably committed at the time, and the storage was subsequently
destroyed by something outside CER — an unmounted volume, a wiped data
directory, a bad restore. That is external data loss, and no application
can prevent it.

The PID requires that "persistent state must survive restart" and that
"required dependencies [are] reflected in readiness". CER's persistence is
correct: with both volumes intact, restart preserves everything, including
artifact bytes byte-for-byte. What is missing is *detection* — CER cannot
currently tell "this is a brand-new deployment" from "this deployment's
metadata was destroyed", because an empty database looks identical to a
first run.

The consequence is nonetheless real and worth stating plainly: after this
kind of loss, CER reports itself healthy while being authoritative for
nothing. An operator gets no signal from the product that anything is
wrong.

### What fixing it would require

The trick used on the artifact side does not generalise. That check works
because the metadata store is a second, independent witness to whether
artifacts ever existed. There is no third store to witness for the metadata
store, and the artifact volume cannot fill that role — a deployment may
legitimately hold evidence and no artifacts at all, so an empty artifact
store proves nothing about whether the registry was ever populated.

A real fix needs volume-identity markers cross-recorded between the two
stores: each volume stamped with an identity and a creation time at first
initialisation, each store recording the identity of its counterpart, and a
startup comparison that treats "my counterpart remembers a metadata volume
whose identity is not the one I just found" as data loss rather than a
first deployment. That means new on-disk state, a migration, new startup
logic on both sides, and careful handling of the legitimate cases that look
similar — an intentional wipe-and-restart, a restore from backup, a volume
deliberately re-provisioned.

That is a design change, not a repair. Until it is made, the operational
mitigation is external: back up the metadata volume, and monitor the
registry's record counts rather than relying on `/ready` alone.
