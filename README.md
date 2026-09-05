# CER — Canonical Evidence Registry

CER is the canonical empirical-evidence and lineage system for THE GOAL.
It is authoritative for:

* which strategy/version a piece of evidence belongs to;
* which experiment/run produced it;
* which code commit, dataset, configuration and environment produced it;
* which producer wrote it, and when (UTC);
* which metrics/verdicts were recorded;
* where associated artifacts live, and their integrity (SHA-256);
* what promotion (`DRAFT` → ... → `RETIRED`) and strategy-health history
  exists for a strategy/version.

Git remains authoritative for code, strategy definitions, schemas,
contracts and governance — CER does not replace it.

## Non-goals

Per `PID.md`, CER deliberately does **not** implement: strategy
generation, a backtesting engine, trade execution, NEO's analysis logic,
strategy tuning, a generic data lake, a speculative dashboard/UI, or
distributed infrastructure beyond what's demonstrated as needed. Its
presentation target is `FUNCTIONAL_ONLY` — there is no UI, and building
one is explicitly out of scope.

Unknown/ad-hoc evidence types, incompatible contract or schema versions,
and missing required provenance are all rejected or flagged loudly — CER
never infers or silently backfills a producer's missing context.

## Architecture

```
src/cer/
  contract/    pure-domain layer: identity, controlled evidence types,
               pydantic v2 models, error hierarchy, versioning, and the
               ArtifactStore/MetadataStore Protocol interfaces. No I/O.
  runtime/     environment-driven configuration (no secrets/config in
               source), a UTC-only clock, structured JSON logging.
  metadata/    SQLiteMetadataStore: strategies, experiments, runs,
               evidence, artifact references, promotions, health —
               migration-tracked, idempotent, immutable-once-written.
  artifacts/   FilesystemArtifactStore: hash-addressed (SHA-256), content-
               deduplicated bulk artifact storage on local disk.
  api/         FastAPI HTTP service (versioned under /v1) wiring the above
               behind the contract's Protocol interfaces — no concrete
               backend is hardcoded into the service layer.
  client.py    CERClient: a synchronous producer/consumer client over the
               HTTP API. See docs/PRODUCER-GUIDE.md.
```

The metadata and artifact backends are both replaceable behind their
`Protocol` interfaces (`cer.contract.stores`) — SQLite and local-filesystem
are the smallest robust v1 implementations, not a permanent commitment to
either.

## Running the tests

```
python3 -m pytest
```

Test layout:

* `tests/contract/`, `tests/runtime/` — pure unit tests, no I/O.
* `tests/metadata/`, `tests/artifacts/` — each backend against its own
  real implementation (real SQLite file, real filesystem).
* `tests/api/` — the HTTP layer against in-memory fakes of both stores,
  proving the API's own behaviour (routing, error mapping, idempotency
  plumbing) independent of any one backend's implementation details.
* `tests/integration/` — end-to-end proof against the **real** stores
  driven through the real HTTP API: the PID's acceptance criteria (vertical
  slice, multi-producer, lifecycle/health, fault/restart, structured
  logging) proven against the running product, not fakes. See each
  module's docstring for which PID criterion it proves.

## Configuration

CER takes no configuration from source code — everything is read from the
process environment by `cer.runtime.config.load_settings()`, which fails
loudly (naming the missing variable) if a required one is absent.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `CER_METADATA_DB_PATH` | yes | — | Path to the SQLite database file (created if absent) |
| `CER_ARTIFACT_ROOT` | yes | — | Root directory for hash-addressed artifact storage |
| `CER_HOST` | no | `0.0.0.0` | Bind host for the HTTP service |
| `CER_PORT` | no | `8000` | Bind port |
| `CER_LOG_LEVEL` | no | `INFO` | Root logger level |
| `CER_ENVIRONMENT` | no | `dev` | Free-form environment label, surfaced in logs/health |
| `CER_MAX_ARTIFACT_BYTES` | no | `104857600` (100 MiB) | Maximum single artifact upload size |

No secrets are ever read, persisted, or logged by CER; `Settings.redacted()`
masks any field whose *name* looks secret-shaped before it is logged.

## Further reading

* [`PID.md`](PID.md) — the authoritative product contract for CER v1.
* [`docs/PRODUCER-GUIDE.md`](docs/PRODUCER-GUIDE.md) — integration guide
  for a producer team (HSA, APOLLO, NEO, ...) writing evidence into CER.
* [`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md) — what CER v1
  deliberately does not do, with the evidence for each and what changing it
* [`docs/adr/`](docs/adr/) — architecture decisions governing CER's
  semantics. ADR-001 settles cross-producer evidence-reference resolution;
  v1 conforms as delivered.
  would require. Read this before designing a system around CER.
