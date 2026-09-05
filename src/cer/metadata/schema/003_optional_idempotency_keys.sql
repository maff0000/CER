-- CER metadata store -- optional producer-scoped idempotency keys for
-- experiments, promotions and health records (schema_migrations version 3).
--
-- Why: the PID's idempotency clause ("ingestion must tolerate safe
-- retries; duplicate submissions must be detectable via idempotency key
-- or equivalent") is not scoped to evidence and runs. Before this
-- migration, evidence and runs carried an idempotency_key column while
-- experiments, promotions and health_records did not -- so those three
-- write paths accepted an Idempotency-Key, returned 201 and minted a new
-- row on every retry. One intended promotion transition could be
-- recorded three times, corrupting the promotion history that is itself
-- a first-class PID deliverable.
--
-- The key stays OPTIONAL on all three (unlike evidence, where it is
-- required): a caller that supplies no key gets today's behaviour --
-- a fresh id and a new row. So uniqueness is enforced with the same
-- partial-index pattern already used for runs in 002, which indexes only
-- the rows that actually carry a key.
--
-- Scoping is (producer, idempotency_key), never the bare key -- see
-- 002_producer_scoped_idempotency.sql and cer.metadata.sqlite_store's
-- module docstring. For promotions and health_records the producer is
-- taken from the record itself (PromotionTransition.producer /
-- StrategyHealthRecord.producer), consistent with how create_run and
-- append_evidence scope theirs.
--
-- content_fingerprint mirrors the runs/evidence columns: it is what makes
-- "same key, materially different body" detectable as an
-- IdempotencyConflictError rather than a silently-returned stale record.
-- The fingerprint's field exclusions (in particular the server-assigned
-- creation timestamps experiments.created_at, promotions.at_utc and
-- health_records.observed_at_utc, which the API layer fills with
-- wall-clock time per attempt) are documented in
-- cer.metadata.sqlite_store.
--
-- Forward-only: once applied, this file's bytes must never change (see
-- cer.metadata.migrations). A further schema change is a new numbered
-- file -- never an edit to this one, or to 001/002.
--
-- Note on re-application: SQLite has no `ALTER TABLE ... ADD COLUMN IF
-- NOT EXISTS`, so unlike 001/002 the statements below are not
-- individually re-runnable. The migration runner never re-runs an
-- already-recorded version, so the ordinary path (including re-applying
-- the whole schema directory) is unaffected; only two processes racing
-- to apply this exact version to the same not-yet-migrated database
-- could hit it, and that surfaces as a loud MetadataStoreError at store
-- construction, never as silent schema drift.

ALTER TABLE experiments ADD COLUMN idempotency_key TEXT;
ALTER TABLE experiments ADD COLUMN content_fingerprint TEXT;

ALTER TABLE promotions ADD COLUMN idempotency_key TEXT;
ALTER TABLE promotions ADD COLUMN content_fingerprint TEXT;

ALTER TABLE health_records ADD COLUMN idempotency_key TEXT;
ALTER TABLE health_records ADD COLUMN content_fingerprint TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_experiments_producer_idempotency_key
    ON experiments (producer, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_promotions_producer_idempotency_key
    ON promotions (producer, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_health_records_producer_idempotency_key
    ON health_records (producer, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
