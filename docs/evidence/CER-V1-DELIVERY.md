# CER v1 — Delivery Evidence

Branch `feat/cer-v1`, built under Forge governance against `PID.md`.
Presentation target: `FUNCTIONAL_ONLY`.

## Verdict

An independent, fresh-context Auditor returned **`PRODUCT_GREEN`** against
commit `217c0468dfc548fdd85fcf55219e405ab7b2a13f`, after driving ~120 HTTP
interactions against the running product both natively and in Docker,
planting 14 mutations in load-bearing guards (all caught by the suite), and
running every example in `docs/PRODUCER-GUIDE.md` verbatim against a live
instance.

Its central durability finding, in its own words: *"I could not obtain a
success response for anything that was then lost."*

Two earlier audits of earlier commits returned `PRODUCT_RED`. Each audit was
dispatched with no knowledge of its predecessors, so a clean verdict could
not be inherited.

## What was built

A containerised HTTP service plus a producer client, over a SQLite metadata
store and a hash-addressed filesystem artifact store.

The PID does not name a service in so many words. It was read as one because
it requires an *image* promotable to production, health/readiness endpoints,
clean startup, proof "on the running product", and that producers not depend
on database internals. That reading is recorded here because it was the one
significant interpretive decision in the build.

| Layer | Package |
|---|---|
| Versioned contract — identity, 20 evidence classes, provenance models | `src/cer/contract/` |
| Config, structured UTC logging, clock | `src/cer/runtime/` |
| SQLite metadata store, forward-only migrations | `src/cer/metadata/` |
| Hash-addressed filesystem artifact store | `src/cer/artifacts/` |
| HTTP API and producer client | `src/cer/api/`, `src/cer/client.py` |

## Acceptance criteria — how each was proven

Proven on the running product, not only in the suite. The PID's own closing
line is that passing unit tests alone is insufficient.

| PID criterion | Evidence |
|---|---|
| Contract gate | All 20 evidence classes accepted; unknown type, incompatible schema version, unknown field and naive datetime each rejected loudly with a stable error `code` |
| Vertical slice | strategy → version → experiment → run → `BACKTEST` evidence → artifact registration → query → byte-identical retrieval, over HTTP against the container |
| Multi-producer | HSA / APOLLO / NEO writing distinct record shapes through one contract, safely sharing an idempotency key string; NEO's provenance-light records recorded `INCOMPLETE` with missing fields named, never backfilled |
| Lifecycle / health | Promotion transitions and `STRATEGY_HEALTH` records persist, stay ordered, and preserve authority, reason and evidence references |
| Fault / restart | Retried submission, metadata-store failure, artifact-store failure, malformed provenance, checksum mismatch, and restart with evidence and artifacts preserved |
| Operational | Clean startup on `dell-debian` native and in Docker, non-root `uid=999`, readiness reflecting both dependencies, structured UTC JSON logs, state surviving `docker compose down && up` |

## Defects found and fixed

Eleven defects were found and repaired before the GREEN verdict. Every one
surfaced at a boundary — between two components, between a test double and a
real store, or between the code and an actual running container. None was
caught by the suite that shipped it.

The four that matter most for anyone maintaining this:

1. **Acknowledged writes that were then lost.** With the metadata store gone,
   nine submissions returned `201 Created`, were served back on `GET`, and
   left zero survivors after restart — while `/ready` correctly reported 503
   throughout. The write path never asked what readiness already knew. Both
   stores now verify their durable backing before acknowledging a write.

2. **A store that re-manufactured itself.** `health()` refused to create a
   vanished artifact store, but `put()` lazily initialised on every write and
   did exactly that: one write after a volume loss returned `201`, recreated
   the root, and flipped `/ready` back to 200 while every registered artifact
   was permanently unretrievable. This one was caused by an explicit PL
   instruction to keep lazy initialisation "for convenience".

3. **A test double that disagreed with the real store.** Safe retries were
   rejected as 409 conflicts because the API filled server-side timestamps
   per attempt while the store's fingerprint included them. Both suites were
   green: the store's tests pinned timestamps, and the API's fakes already
   excluded the field. The fake and the real store disagreed on exactly the
   field that mattered.

4. **Tests whose names claimed more than they tested.** The artifact-store
   failure proof replaced the store root with a regular file — the one
   variant where `mkdir` fails — so it passed while the ordinary "directory
   is gone" case returned `201`. A malformed-provenance proof queried an
   empty database and asserted four keys were absent from an empty list; it
   would have passed against an implementation that stored every malformed
   record. Both now fail under an injected defect.

Two further defects were found by Engineers verifying their own work rather
than trusting it — one while checking that the producer guide's examples
actually ran, one while proving a container fix.

## Reproducing the durability proofs

Both were verified against a real container and against control builds with
the guard removed, which reproduced the original defects exactly.

**Write refused when the metadata store is gone**

```
docker run -d --name cer -e CER_METADATA_DB_PATH=/var/lib/cer/metadata/sub/cer.db \
  -e CER_ARTIFACT_ROOT=/var/lib/cer/artifacts -p 8000:8000 \
  -v m:/var/lib/cer/metadata -v a:/var/lib/cer/artifacts cer:latest
curl -s localhost:8000/ready                      # 200
docker exec -u root cer rm -rf /var/lib/cer/metadata/sub
curl -s localhost:8000/ready                      # 503, failed_dependency: metadata_store
curl -s -XPOST localhost:8000/v1/evidence ...     # 503 metadata_store_error
                                                  # before the fix: 201, then lost on restart
```

**Restart onto a lost artifact volume**

```
# register an artifact, then:
docker rm -f cer && docker volume rm <artifact-volume> && docker volume create <artifact-volume>
# restart on the same metadata volume:
curl -s localhost:8000/ready                      # 503, failed_dependency: artifact_store
curl -s localhost:8000/v1/artifacts/<id>/download # 503, not a 404 blaming the caller
curl -s localhost:8000/v1/artifacts/<id>          # 200 — metadata readable, so the loss is visible
                                                  # before the fix: /ready 200, root silently recreated
```

## Known limitations

Three deliberate gaps are recorded in `docs/KNOWN-LIMITATIONS.md`, each
verified against the running product. None is a PID violation, and each was
left rather than fixed because closing it would mean adding capability the
PID does not require, or making a product decision that is not the delivery
loop's to make.

## Provenance

- Built through bounded, isolated Engineer dispatches; no Engineer held Git
  authority, and every diff was reviewed and committed by the PL.
- `/srv/CER` was never modified during the build; it remained at `b05104b`
  throughout, with all work done in separate worktrees.
- Three independent audits, each dispatched with fresh context.
