-- CER metadata store -- scope idempotency uniqueness per producer
-- (schema_migrations version 2).
--
-- 001_initial.sql's ux_evidence_idempotency_key / ux_runs_idempotency_key
-- were unique on idempotency_key alone, globally across all producers.
-- That let one producer's key choice permanently claim a natural key
-- string (e.g. "2026-09-04-btc-sweep") and made an unrelated producer's
-- later use of the same string collide as a false IdempotencyConflictError
-- against a record it has nothing to do with. Idempotency keys are scoped
-- to the producer: this migration drops both global indexes and replaces
-- them with composite (producer, idempotency_key) uniqueness. See
-- cer.metadata.sqlite_store's module docstring for the scoping rule and
-- SQLiteMetadataStore.create_run / .append_evidence for the matching
-- lookup-scope change.
--
-- Forward-only: once applied, this file's bytes must never change (see
-- cer.metadata.migrations). A further schema change is a new numbered
-- file -- never an edit to this one, or to 001_initial.sql.

DROP INDEX IF EXISTS ux_evidence_idempotency_key;
DROP INDEX IF EXISTS ux_runs_idempotency_key;

CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_producer_idempotency_key
    ON evidence (producer, idempotency_key);

-- idempotency_key remains optional on runs (create_run's idempotency_key
-- parameter is optional), so this stays a partial index exactly as
-- ux_runs_idempotency_key was in 001.
CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_producer_idempotency_key
    ON runs (producer, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
