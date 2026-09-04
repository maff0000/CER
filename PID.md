# PID — CER v1 Canonical Evidence Registry

## Product outcome

Build CER v1 as the canonical empirical evidence and lineage system for THE GOAL.

CER must let authorised consumers determine, without searching arbitrary files or relying on chat history:

* which strategy/version evidence belongs to;
* which experiment/run produced it;
* which code commit, dataset, configuration and environment produced it;
* which producer wrote it;
* which metrics/verdicts were recorded;
* where associated artifacts live;
* what promotion and strategy-health history exists.

Presentation target: `FUNCTIONAL_ONLY`.

## Authority and boundaries

* Git is authoritative for code, strategy definitions, schemas, contracts and governance.
* CER is authoritative for empirical evidence, experiment/run identity, lineage, strategy-health history and artifact references.
* CER does not generate strategies, make trading decisions, execute trades, perform backtests, tune models, or replace Git.
* Downstream producers must not invent incompatible evidence identity or storage conventions.

## Canonical identity model

CER v1 must support immutable identifiers for:

* `strategy_id` — stable logical strategy identity; never reused for a different thesis.
* `strategy_version` — immutable version of one strategy; any logic change creates a new version.
* `experiment_id` — groups one research/validation objective.
* `run_id` — unique execution of an experiment.
* `evidence_id` — unique append-oriented evidence record.
* `artifact_id` — unique registered artifact.

The design must remain extendable for `trade_id`, `decision_id`, `execution_id`, `strategy_instance_id`.

## Controlled evidence classes

Research:

* `SOURCE_ANALYSIS`
* `STRATEGY_HYPOTHESIS`

Historical:

* `BACKTEST`
* `OUT_OF_SAMPLE`
* `WALK_FORWARD`
* `MONTE_CARLO`
* `PARAMETER_SWEEP`
* `ABLATION`

Forward:

* `SHADOW`
* `PAPER`
* `MT5_DEMO`
* `PLUTUS`

Live:

* `TINY_LIVE`
* `PERSONAL_LIVE`
* `FUNDED_LIVE`

Learning / surveillance:

* `NEO_OBSERVATION`
* `NEO_HYPOTHESIS`
* `REGIME_ANALYSIS`
* `DRIFT_ALERT`
* `STRATEGY_HEALTH`

Unknown ad-hoc evidence types must fail loudly unless introduced through a versioned contract change.

## Required provenance

Evidence records must support, where applicable:

* evidence type;
* strategy_id / strategy_version;
* experiment_id / run_id;
* producer and producer version;
* UTC creation and observation timestamps;
* Git repository and exact commit;
* dataset identity/version/reference;
* config hash/reference;
* environment;
* instrument(s);
* timeframe(s);
* metrics;
* verdict/status;
* regime/context tags;
* parent evidence/run references;
* artifact references;
* structured notes/observations;
* schema version.

Missing required provenance must be explicit and queryable; CER must never infer it silently.

## Artifact doctrine

A file is not valid evidence merely because it exists.

CER must:

1. register every material artifact;
2. assign artifact identity;
3. preserve hash/checksum;
4. preserve content/type metadata;
5. link artifact to evidence/run identity;
6. provide retrieval metadata;
7. prevent silent overwrite of immutable artifacts.

The logical model separates structured metadata from bulk artifacts. v1 must use the smallest robust implementation and avoid infrastructure bloat.

A lightweight filesystem/hash-addressed artifact adapter and simple relational metadata store are acceptable if the caller contract is backend-neutral and migration-friendly.

## Promotion lifecycle records

CER records transitions; it does not autonomously make promotion decisions.

Canonical states:

* `DRAFT`
* `IMPLEMENTED`
* `BACKTESTED`
* `VALIDATED`
* `FORWARD_TEST`
* `LIVE_CANDIDATE`
* `TINY_LIVE`
* `APPROVED`
* `SUSPENDED`
* `RETIRED`

Every transition must preserve strategy/version, from/to state, UTC timestamp, authority/producer, supporting evidence references and reason.

## Strategy health / drift records

CER must support NEO's always-on surveillance without implementing NEO's analysis.

Canonical health states:

* `HEALTHY`
* `DORMANT`
* `WATCH`
* `DEGRADED`
* `SUSPENDED`

Health evidence must support:

* observed trigger rate;
* regime-conditioned expected trigger rate;
* expectancy / R;
* win rate;
* drawdown;
* MAE/MFE;
* holding time;
* execution/slippage quality;
* regime distribution;
* strategy/chain strength distribution where available;
* historical/forward/live baseline comparison;
* confidence and evidence-backed reason.

CER records observations. NEO performs analysis and hypotheses.

## Producer / consumer contract

All producers/consumers use versioned CER contracts and must not depend on direct database internals.

Required v1 capabilities:

* register/reference strategy and version;
* create experiment;
* create run;
* append evidence;
* register artifact;
* attach artifact;
* record metrics;
* finalise/close run;
* query evidence by strategy/version/run/type/time where practical;
* query artifacts;
* query promotion history;
* query health history.

## Idempotency and immutability

* ingestion must tolerate safe retries;
* duplicate submissions must be detectable via idempotency key or equivalent;
* duplicate run creation must not create ambiguous identities;
* historical evidence must never be silently rewritten to match a newer strategy version;
* immutable artifacts/evidence cannot be silently replaced.

## Reproducibility

For material experiments CER must preserve enough provenance to reconstruct the evidence context, including exact code commit, strategy version, dataset/reference and configuration identity.

If reproducibility is incomplete, the record must be marked incomplete rather than guessed.

## Runtime / deployment

* development host: `dell-debian`;
* Docker-first where a service runtime is used;
* same proven application/image must be promotable later to production;
* no config or secrets in source code;
* UTC everywhere;
* structured logs;
* health/readiness where applicable;
* persistent state must survive restart;
* metadata/artifact backends remain replaceable behind interfaces.

Anti-bloat: do not introduce distributed infrastructure unless required by demonstrated product need. SQLite or another simple relational store is acceptable for the first dev implementation if schema/migration discipline is clean.

## Security

CER must never require or persist broker credentials, API keys, passwords, private keys or secrets as evidence.

Public Git must contain no secrets. Runtime configuration remains external.

## Acceptance criteria

### Contract gate

* canonical identity model implemented and tested;
* controlled evidence types implemented;
* incompatible schema/version inputs fail loudly.

### Vertical slice

Prove end to end on the running product:

strategy/version reference -> experiment -> run -> `BACKTEST` evidence -> artifact registration -> query/retrieval.

### Multi-producer proof

At least three representative producers (fixtures acceptable initially, e.g. HSA/APOLLO/NEO) can write distinct valid records using the same contract without ambiguity.

### Lifecycle / health proof

Promotion history and `STRATEGY_HEALTH` records persist and are queryable.

### Fault / restart proof

Prove behaviour for duplicate/retried submission, metadata-store failure, artifact-store failure, malformed provenance, checksum mismatch, and restart with evidence preserved.

### Operational proof

* clean startup on `dell-debian`;
* required dependencies reflected in readiness;
* structured UTC logging;
* persisted records survive restart;
* registered artifacts remain retrievable after restart.

## Required tests

At minimum:

* identity validation;
* schema validation;
* lifecycle transitions;
* artifact checksum/integrity;
* idempotency;
* representative producer contract tests;
* full experiment/run/evidence/artifact integration test;
* failure/restart tests.

## Explicit non-goals

Do not build:

* strategy generation;
* backtesting engine;
* trading execution;
* NEO logic;
* strategy tuning;
* generic data lake;
* speculative dashboard/UI;
* distributed infrastructure without evidence it is needed.

## Definition of complete

CER v1 is complete only when:

* authoritative contracts/schemas exist;
* running product behaviour is proven;
* metadata persistence works;
* artifact abstraction/integrity works;
* idempotency works;
* lifecycle and health records work;
* restart/failure behaviour is proven;
* producer integration guidance exists;
* Git is clean and auditable;
* no secrets/config are embedded in code;
* FORGE's fresh-context independent Auditor returns `PRODUCT_GREEN` against this PID.

Passing unit tests alone is insufficient.
