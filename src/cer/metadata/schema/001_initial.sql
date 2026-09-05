-- CER metadata store -- initial schema (schema_migrations version 1).
--
-- Forward-only migration. Once this file has been applied to any
-- database, its bytes must never change -- cer.metadata.migrations
-- verifies a checksum of this exact file against what was recorded at
-- apply time and fails loudly on a mismatch. A future schema change is a
-- new numbered file (002_....sql), never an edit to this one.
--
-- Timestamps are stored as ISO-8601 strings with full microsecond
-- precision and an explicit UTC offset (see cer.metadata.sqlite_store's
-- _dt_to_str/_str_to_dt) so round-tripping through this store is exact.
-- List/dict-valued contract fields are stored as canonical JSON text in
-- a "*_json" column.

CREATE TABLE IF NOT EXISTS strategies (
    strategy_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    thesis        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_id      TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    git_repo         TEXT NOT NULL,
    git_commit       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    notes            TEXT,
    PRIMARY KEY (strategy_id, strategy_version),
    FOREIGN KEY (strategy_id) REFERENCES strategies (strategy_id)
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id    TEXT PRIMARY KEY,
    objective        TEXT NOT NULL,
    strategy_id      TEXT,
    strategy_version TEXT,
    producer         TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id                  TEXT PRIMARY KEY,
    experiment_id           TEXT NOT NULL,
    status                  TEXT NOT NULL,
    started_at              TEXT NOT NULL,
    ended_at                TEXT,
    producer                TEXT NOT NULL,
    producer_version        TEXT,
    git_repo                TEXT,
    git_commit              TEXT,
    dataset_id              TEXT,
    dataset_version         TEXT,
    dataset_ref             TEXT,
    config_hash             TEXT,
    config_ref              TEXT,
    environment             TEXT,
    provenance_completeness TEXT NOT NULL,
    missing_provenance_json TEXT NOT NULL,
    idempotency_key         TEXT,
    content_fingerprint     TEXT,
    FOREIGN KEY (experiment_id) REFERENCES experiments (experiment_id)
);

-- idempotency_key is optional (create_run's parameter is optional), so a
-- plain UNIQUE column would still be correct in SQLite (NULLs are never
-- considered equal to each other in a UNIQUE index) -- the partial index
-- is used anyway to document the intent explicitly and to keep the index
-- small.
CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_idempotency_key
    ON runs (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_runs_experiment_id ON runs (experiment_id);
CREATE INDEX IF NOT EXISTS ix_runs_started_at ON runs (started_at);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id              TEXT PRIMARY KEY,
    idempotency_key          TEXT NOT NULL,
    evidence_type            TEXT NOT NULL,
    schema_version           INTEGER NOT NULL,
    strategy_id              TEXT,
    strategy_version         TEXT,
    experiment_id            TEXT,
    run_id                   TEXT,
    producer                 TEXT NOT NULL,
    producer_version         TEXT,
    created_at_utc           TEXT NOT NULL,
    observed_at_utc          TEXT,
    git_repo                 TEXT,
    git_commit               TEXT,
    dataset_id               TEXT,
    dataset_version          TEXT,
    dataset_ref              TEXT,
    config_hash              TEXT,
    config_ref               TEXT,
    environment               TEXT,
    instruments_json          TEXT NOT NULL,
    timeframes_json            TEXT NOT NULL,
    metrics_json                TEXT NOT NULL,
    verdict                     TEXT,
    status                      TEXT,
    regime_tags_json             TEXT NOT NULL,
    parent_evidence_ids_json      TEXT NOT NULL,
    parent_run_ids_json            TEXT NOT NULL,
    artifact_ids_json               TEXT NOT NULL,
    notes_json                       TEXT NOT NULL,
    provenance_completeness           TEXT NOT NULL,
    missing_provenance_json            TEXT NOT NULL,
    content_fingerprint                 TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs (run_id),
    FOREIGN KEY (experiment_id) REFERENCES experiments (experiment_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_idempotency_key ON evidence (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_evidence_strategy_id ON evidence (strategy_id);
CREATE INDEX IF NOT EXISTS ix_evidence_strategy_id_version ON evidence (strategy_id, strategy_version);
CREATE INDEX IF NOT EXISTS ix_evidence_run_id ON evidence (run_id);
CREATE INDEX IF NOT EXISTS ix_evidence_experiment_id ON evidence (experiment_id);
CREATE INDEX IF NOT EXISTS ix_evidence_evidence_type ON evidence (evidence_type);
CREATE INDEX IF NOT EXISTS ix_evidence_created_at_utc ON evidence (created_at_utc);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id          TEXT PRIMARY KEY,
    sha256                TEXT NOT NULL,
    size_bytes             INTEGER NOT NULL,
    content_type            TEXT NOT NULL,
    filename                 TEXT NOT NULL,
    uri                       TEXT NOT NULL,
    registered_at              TEXT NOT NULL,
    run_id                      TEXT,
    evidence_id                  TEXT,
    content_fingerprint           TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs (run_id),
    FOREIGN KEY (evidence_id) REFERENCES evidence (evidence_id)
);

CREATE INDEX IF NOT EXISTS ix_artifacts_run_id ON artifacts (run_id);
CREATE INDEX IF NOT EXISTS ix_artifacts_evidence_id ON artifacts (evidence_id);
CREATE INDEX IF NOT EXISTS ix_artifacts_registered_at ON artifacts (registered_at);

CREATE TABLE IF NOT EXISTS promotions (
    transition_id       TEXT PRIMARY KEY,
    strategy_id           TEXT NOT NULL,
    strategy_version       TEXT NOT NULL,
    from_state              TEXT NOT NULL,
    to_state                 TEXT NOT NULL,
    at_utc                    TEXT NOT NULL,
    authority                  TEXT NOT NULL,
    producer                    TEXT NOT NULL,
    evidence_ids_json             TEXT NOT NULL,
    reason                         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_promotions_strategy_id ON promotions (strategy_id);
CREATE INDEX IF NOT EXISTS ix_promotions_strategy_id_version ON promotions (strategy_id, strategy_version);
CREATE INDEX IF NOT EXISTS ix_promotions_at_utc ON promotions (at_utc);

CREATE TABLE IF NOT EXISTS health_records (
    health_id                                 TEXT PRIMARY KEY,
    strategy_id                                TEXT NOT NULL,
    strategy_version                            TEXT NOT NULL,
    health_state                                 TEXT NOT NULL,
    observed_at_utc                               TEXT NOT NULL,
    producer                                       TEXT NOT NULL,
    observed_trigger_rate                           REAL,
    regime_conditioned_expected_trigger_rate         REAL,
    expectancy_r                                      REAL,
    win_rate                                           REAL,
    drawdown                                            REAL,
    mae                                                  REAL,
    mfe                                                   REAL,
    holding_time                                           REAL,
    execution_slippage_quality                              REAL,
    regime_distribution_json                                 TEXT,
    strategy_chain_strength_distribution_json                 TEXT,
    baseline_comparison_json                                   TEXT,
    confidence                                                  REAL,
    reason                                                       TEXT NOT NULL,
    evidence_ids_json                                             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_health_strategy_id ON health_records (strategy_id);
CREATE INDEX IF NOT EXISTS ix_health_strategy_id_version ON health_records (strategy_id, strategy_version);
CREATE INDEX IF NOT EXISTS ix_health_observed_at_utc ON health_records (observed_at_utc);
