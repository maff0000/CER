-- CER metadata store -- optional producer-scoped idempotency keys for
-- artifact registration (schema_migrations version 4).
--
-- Why: POST /v1/artifacts accepted an Idempotency-Key header and threw it
-- away. Two identical retries of one logical registration produced two
-- ArtifactRecords with two different artifact_ids and no way to tell they
-- were the same submission. Content-addressing deduplicates the BLOB, not
-- the RECORD: the bytes were stored once, but the registry held two
-- indistinguishable registrations of them, so "duplicate submissions must
-- be detectable via idempotency key or equivalent" (PID, "Idempotency and
-- immutability") was not satisfied for a required v1 capability
-- ("register artifact", PID "Producer / consumer contract"). Accepting a
-- header that silently does nothing is worse than rejecting it -- the same
-- reasoning that put optional keys on /v1/experiments, /v1/promotions and
-- /v1/health-records in 003.
--
-- The key is OPTIONAL, exactly as it is for experiments, promotions,
-- health records and runs: a caller that supplies none gets today's
-- behaviour -- a fresh artifact_id and a new row. Uniqueness is therefore
-- enforced with the same partial-index pattern as 002/003, indexing only
-- the rows that actually carry a key.
--
-- Scoping is (producer, idempotency_key), never the bare key -- see
-- 002_producer_scoped_idempotency.sql. Note the ONE asymmetry with the
-- other five write paths: ArtifactRecord has no producer field, and
-- POST /v1/artifacts has no producer in its body (the body is the
-- artifact's raw bytes). The producer is supplied by the caller as the
-- X-CER-Producer header, purely to scope the key, and is stored HERE --
-- on the row -- rather than on the contract model, so no artifact record
-- gains a field it did not have. Supplying an idempotency key without a
-- producer is a contract violation (400), identical to the rule the other
-- endpoints already enforce; an artifact registered without a key stores
-- NULL in both columns and is unaffected.
--
-- idempotency_fingerprint is what makes "same key, materially different
-- body" detectable as an IdempotencyConflictError rather than a silently
-- returned stale record. It is deliberately a SECOND fingerprint column
-- alongside content_fingerprint, not a replacement for it: the two answer
-- different questions and must not share one value.
--
--   content_fingerprint      -- "is this the same artifact under the same
--                               artifact_id?" (immutability by id). Covers
--                               uri and registered_at.
--   idempotency_fingerprint  -- "is this the same logical registration?"
--                               (idempotency by producer+key). Excludes
--                               artifact_id, run_id/evidence_id (attach is
--                               the sanctioned path for those), and the
--                               store-assigned uri and registered_at.
--
-- registered_at is excluded for the same reason every other server-assigned
-- creation timestamp is excluded from every other fingerprint in this
-- store (see cer.metadata.sqlite_store's module docstring): it is stamped
-- per attempt, so including it would make every honest retry hash
-- differently and report a false conflict. uri is excluded on the same
-- grounds -- it is assigned by the artifact store, not authored by the
-- caller. What remains is exactly what the caller submitted: sha256,
-- size_bytes, content_type and filename.
--
-- Forward-only: once applied, this file's bytes must never change (see
-- cer.metadata.migrations). A further schema change is a new numbered
-- file -- never an edit to this one, or to 001/002/003.
--
-- Note on re-application: SQLite has no `ALTER TABLE ... ADD COLUMN IF
-- NOT EXISTS`, so as with 003 the statements below are not individually
-- re-runnable. The migration runner never re-runs an already-recorded
-- version, so the ordinary path is unaffected; only two processes racing
-- to apply this exact version to the same not-yet-migrated database could
-- hit it, and that surfaces as a loud MetadataStoreError at store
-- construction, never as silent schema drift.

ALTER TABLE artifacts ADD COLUMN producer TEXT;
ALTER TABLE artifacts ADD COLUMN idempotency_key TEXT;
ALTER TABLE artifacts ADD COLUMN idempotency_fingerprint TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_artifacts_producer_idempotency_key
    ON artifacts (producer, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
