"""Tests for cer.metadata.migrations: apply-once, idempotent re-apply,
and loud failure on a tampered checksum.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cer.contract.errors import MetadataStoreError
from cer.metadata.migrations import apply_migrations


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _write_migration(schema_dir: Path, filename: str, sql: str) -> None:
    (schema_dir / filename).write_text(sql, encoding="utf-8")


def test_migrations_apply_once_and_are_recorded(tmp_path):
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    _write_migration(schema_dir, "001_initial.sql", "CREATE TABLE widgets (id TEXT PRIMARY KEY);")

    db_path = tmp_path / "db.sqlite3"
    conn = _connect(db_path)
    try:
        apply_migrations(conn, schema_dir)

        rows = conn.execute("SELECT version, filename, checksum FROM schema_migrations").fetchall()
        assert len(rows) == 1
        assert rows[0]["version"] == 1
        assert rows[0]["filename"] == "001_initial.sql"

        # Table was actually created and is usable.
        conn.execute("INSERT INTO widgets (id) VALUES ('w1')")
        assert conn.execute("SELECT id FROM widgets").fetchone()["id"] == "w1"
    finally:
        conn.close()


def test_applying_twice_is_a_no_op(tmp_path):
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    _write_migration(schema_dir, "001_initial.sql", "CREATE TABLE widgets (id TEXT PRIMARY KEY);")

    db_path = tmp_path / "db.sqlite3"
    conn = _connect(db_path)
    try:
        apply_migrations(conn, schema_dir)
        conn.execute("INSERT INTO widgets (id) VALUES ('w1')")

        # Re-applying must not error (e.g. "table already exists") and must
        # not touch existing data or duplicate the tracking row.
        apply_migrations(conn, schema_dir)

        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        assert len(rows) == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM widgets").fetchone()["n"] == 1
    finally:
        conn.close()


def test_second_connection_sees_already_applied_migrations(tmp_path):
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    _write_migration(schema_dir, "001_initial.sql", "CREATE TABLE widgets (id TEXT PRIMARY KEY);")

    db_path = tmp_path / "db.sqlite3"
    conn1 = _connect(db_path)
    try:
        apply_migrations(conn1, schema_dir)
        conn1.execute("INSERT INTO widgets (id) VALUES ('w1')")
    finally:
        conn1.close()

    conn2 = _connect(db_path)
    try:
        apply_migrations(conn2, schema_dir)
        assert conn2.execute("SELECT COUNT(*) AS n FROM widgets").fetchone()["n"] == 1
    finally:
        conn2.close()


def test_multiple_migrations_apply_in_version_order(tmp_path):
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    _write_migration(schema_dir, "001_initial.sql", "CREATE TABLE a (id TEXT PRIMARY KEY);")
    _write_migration(schema_dir, "002_add_b.sql", "CREATE TABLE b (id TEXT PRIMARY KEY, a_id TEXT REFERENCES a(id));")

    db_path = tmp_path / "db.sqlite3"
    conn = _connect(db_path)
    try:
        apply_migrations(conn, schema_dir)
        rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        assert [r["version"] for r in rows] == [1, 2]
        conn.execute("INSERT INTO a (id) VALUES ('a1')")
        conn.execute("INSERT INTO b (id, a_id) VALUES ('b1', 'a1')")
    finally:
        conn.close()


def test_tampered_checksum_fails_loudly(tmp_path):
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    _write_migration(schema_dir, "001_initial.sql", "CREATE TABLE widgets (id TEXT PRIMARY KEY);")

    db_path = tmp_path / "db.sqlite3"
    conn = _connect(db_path)
    try:
        apply_migrations(conn, schema_dir)

        # Tamper with the already-applied migration file's content.
        _write_migration(schema_dir, "001_initial.sql", "CREATE TABLE widgets (id TEXT PRIMARY KEY, extra TEXT);")

        with pytest.raises(MetadataStoreError, match="modified since it was applied"):
            apply_migrations(conn, schema_dir)
    finally:
        conn.close()


def test_bad_migration_filename_fails_loudly(tmp_path):
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    _write_migration(schema_dir, "not_a_valid_name.sql", "CREATE TABLE widgets (id TEXT PRIMARY KEY);")

    db_path = tmp_path / "db.sqlite3"
    conn = _connect(db_path)
    try:
        with pytest.raises(MetadataStoreError, match="naming convention"):
            apply_migrations(conn, schema_dir)
    finally:
        conn.close()


def test_duplicate_version_numbers_fail_loudly(tmp_path):
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    _write_migration(schema_dir, "001_a.sql", "CREATE TABLE a (id TEXT PRIMARY KEY);")
    _write_migration(schema_dir, "001_b.sql", "CREATE TABLE b (id TEXT PRIMARY KEY);")

    db_path = tmp_path / "db.sqlite3"
    conn = _connect(db_path)
    try:
        with pytest.raises(MetadataStoreError, match="duplicate migration version"):
            apply_migrations(conn, schema_dir)
    finally:
        conn.close()


def test_real_schema_directory_applies_cleanly(tmp_path):
    """The actual shipped schema/ dir applies without error to a fresh db."""
    real_schema_dir = Path(__file__).resolve().parents[2] / "src" / "cer" / "metadata" / "schema"
    assert real_schema_dir.is_dir()

    db_path = tmp_path / "db.sqlite3"
    conn = _connect(db_path)
    try:
        apply_migrations(conn, real_schema_dir)
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "strategies",
            "strategy_versions",
            "experiments",
            "runs",
            "evidence",
            "artifacts",
            "promotions",
            "health_records",
            "schema_migrations",
        } <= tables
    finally:
        conn.close()


def test_002_applies_over_populated_001_only_database_and_is_idempotent(tmp_path):
    """Migration 002 (producer-scoped idempotency uniqueness) must apply
    cleanly as an upgrade over an existing, already-populated 001-only
    database -- not only on a fresh one -- and re-applying it must be a
    no-op that leaves pre-existing rows intact and queryable.
    """
    real_schema_dir = Path(__file__).resolve().parents[2] / "src" / "cer" / "metadata" / "schema"
    migration_001 = real_schema_dir / "001_initial.sql"
    migration_002 = real_schema_dir / "002_producer_scoped_idempotency.sql"
    assert migration_001.is_file()
    assert migration_002.is_file()

    # Step 1: a database with ONLY 001 applied -- simulating a deployment
    # from before 002 existed -- seeded with rows under the old,
    # globally-unique-on-idempotency_key schema.
    only_001_dir = tmp_path / "schema_v1_only"
    only_001_dir.mkdir()
    (only_001_dir / "001_initial.sql").write_text(migration_001.read_text(encoding="utf-8"), encoding="utf-8")

    db_path = tmp_path / "upgrade.db"
    conn = _connect(db_path)
    try:
        apply_migrations(conn, only_001_dir)

        conn.execute(
            "INSERT INTO experiments (experiment_id, objective, producer, created_at) "
            "VALUES ('exp_1', 'obj', 'HSA', '2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO runs (run_id, experiment_id, status, started_at, producer, "
            "provenance_completeness, missing_provenance_json, idempotency_key, content_fingerprint) "
            "VALUES ('run_1', 'exp_1', 'OPEN', '2026-01-01T00:00:00+00:00', 'HSA', "
            "'INCOMPLETE', '[]', 'run-key-1', 'fp1')"
        )
        conn.execute(
            "INSERT INTO evidence (evidence_id, idempotency_key, evidence_type, schema_version, "
            "producer, created_at_utc, instruments_json, timeframes_json, metrics_json, "
            "regime_tags_json, parent_evidence_ids_json, parent_run_ids_json, artifact_ids_json, "
            "notes_json, provenance_completeness, missing_provenance_json, content_fingerprint) "
            "VALUES ('ev_1', 'ev-key-1', 'BACKTEST', 1, 'HSA', '2026-01-01T00:00:00+00:00', "
            "'[]', '[]', '{}', '[]', '[]', '[]', '[]', '{}', 'INCOMPLETE', '[]', 'fp2')"
        )

        # Step 2: apply the full, real, shipped schema directory (001 + 002)
        # on top of this already-populated, 001-only database -- the actual
        # upgrade path a live deployment would take.
        apply_migrations(conn, real_schema_dir)

        index_names = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        }
        assert "ux_evidence_idempotency_key" not in index_names
        assert "ux_runs_idempotency_key" not in index_names
        assert "ux_evidence_producer_idempotency_key" in index_names
        assert "ux_runs_producer_idempotency_key" in index_names

        # Pre-existing rows survived the upgrade and are still queryable.
        assert (
            conn.execute("SELECT objective FROM experiments WHERE experiment_id = 'exp_1'").fetchone()["objective"]
            == "obj"
        )
        assert conn.execute("SELECT status FROM runs WHERE run_id = 'run_1'").fetchone()["status"] == "OPEN"
        assert (
            conn.execute("SELECT evidence_type FROM evidence WHERE evidence_id = 'ev_1'").fetchone()["evidence_type"]
            == "BACKTEST"
        )

        # The new composite uniqueness is actually enforced post-upgrade: a
        # different producer may reuse the same idempotency_key string...
        conn.execute(
            "INSERT INTO runs (run_id, experiment_id, status, started_at, producer, "
            "provenance_completeness, missing_provenance_json, idempotency_key, content_fingerprint) "
            "VALUES ('run_2', 'exp_1', 'OPEN', '2026-01-01T00:00:00+00:00', 'NEO', "
            "'INCOMPLETE', '[]', 'run-key-1', 'fp3')"
        )
        # ...but the *same* producer reusing its own key must still violate
        # the (now composite) uniqueness constraint.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO runs (run_id, experiment_id, status, started_at, producer, "
                "provenance_completeness, missing_provenance_json, idempotency_key, content_fingerprint) "
                "VALUES ('run_3', 'exp_1', 'OPEN', '2026-01-01T00:00:00+00:00', 'HSA', "
                "'INCOMPLETE', '[]', 'run-key-1', 'fp4')"
            )

        # Step 3: re-applying the full schema dir again is a no-op.
        apply_migrations(conn, real_schema_dir)
        versions = [
            r["version"] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        ]
        # Every shipped migration, in order: this list grows by one each
        # time a forward-only migration is added to the schema directory
        # (003 added optional producer-scoped idempotency keys for
        # experiments/promotions/health_records).
        assert versions == [1, 2, 3]
        assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 2
    finally:
        conn.close()


def test_003_applies_over_populated_001_002_database_and_is_idempotent(tmp_path):
    """Migration 003 (optional producer-scoped idempotency keys for
    experiments, promotions and health_records) must apply cleanly as an
    upgrade over an existing, already-populated 001+002 database -- the
    real upgrade path for a deployment that predates it -- leave those
    rows intact, and enforce the new composite uniqueness afterwards.
    """
    real_schema_dir = Path(__file__).resolve().parents[2] / "src" / "cer" / "metadata" / "schema"
    migration_003 = real_schema_dir / "003_optional_idempotency_keys.sql"
    assert migration_003.is_file()

    # A database with only 001+002 applied, seeded with rows written
    # before idempotency keys reached these three tables.
    pre_003_dir = tmp_path / "schema_pre_003"
    pre_003_dir.mkdir()
    for name in ("001_initial.sql", "002_producer_scoped_idempotency.sql"):
        (pre_003_dir / name).write_text(
            (real_schema_dir / name).read_text(encoding="utf-8"), encoding="utf-8"
        )

    db_path = tmp_path / "upgrade_003.db"
    conn = _connect(db_path)
    try:
        apply_migrations(conn, pre_003_dir)

        conn.execute(
            "INSERT INTO experiments (experiment_id, objective, producer, created_at) "
            "VALUES ('exp_old', 'legacy objective', 'HSA', '2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO promotions (transition_id, strategy_id, strategy_version, from_state, "
            "to_state, at_utc, authority, producer, evidence_ids_json, reason) "
            "VALUES ('trn_old', 'EMA_PULLBACK', 'v1.0.0', 'DRAFT', 'IMPLEMENTED', "
            "'2026-01-01T00:00:00+00:00', 'PL', 'HSA', '[\"ev_1\"]', 'legacy reason')"
        )
        conn.execute(
            "INSERT INTO health_records (health_id, strategy_id, strategy_version, health_state, "
            "observed_at_utc, producer, reason, evidence_ids_json) "
            "VALUES ('hlt_old', 'EMA_PULLBACK', 'v1.0.0', 'HEALTHY', "
            "'2026-01-01T00:00:00+00:00', 'NEO', 'legacy reason', '[\"ev_1\"]')"
        )

        # Apply the full, real, shipped schema directory on top.
        apply_migrations(conn, real_schema_dir)

        for table in ("experiments", "promotions", "health_records"):
            columns = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            assert {"idempotency_key", "content_fingerprint"} <= columns, table

        index_names = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        }
        assert "ux_experiments_producer_idempotency_key" in index_names
        assert "ux_promotions_producer_idempotency_key" in index_names
        assert "ux_health_records_producer_idempotency_key" in index_names

        # Pre-existing rows survived, with a NULL key (they never had one).
        assert (
            conn.execute(
                "SELECT objective, idempotency_key FROM experiments WHERE experiment_id = 'exp_old'"
            ).fetchone()["objective"]
            == "legacy objective"
        )
        assert (
            conn.execute(
                "SELECT idempotency_key FROM promotions WHERE transition_id = 'trn_old'"
            ).fetchone()["idempotency_key"]
            is None
        )
        assert (
            conn.execute(
                "SELECT idempotency_key FROM health_records WHERE health_id = 'hlt_old'"
            ).fetchone()["idempotency_key"]
            is None
        )

        # The key stays OPTIONAL: the partial index must not make two
        # keyless rows for the same producer collide.
        conn.execute(
            "INSERT INTO promotions (transition_id, strategy_id, strategy_version, from_state, "
            "to_state, at_utc, authority, producer, evidence_ids_json, reason) "
            "VALUES ('trn_old_2', 'EMA_PULLBACK', 'v1.0.0', 'IMPLEMENTED', 'BACKTESTED', "
            "'2026-01-02T00:00:00+00:00', 'PL', 'HSA', '[\"ev_1\"]', 'another keyless row')"
        )

        # Composite uniqueness is enforced where a key IS present: a
        # different producer may reuse the same key string...
        conn.execute(
            "INSERT INTO promotions (transition_id, strategy_id, strategy_version, from_state, "
            "to_state, at_utc, authority, producer, evidence_ids_json, reason, "
            "idempotency_key, content_fingerprint) "
            "VALUES ('trn_hsa', 'EMA_PULLBACK', 'v1.0.0', 'DRAFT', 'IMPLEMENTED', "
            "'2026-01-03T00:00:00+00:00', 'PL', 'HSA', '[\"ev_1\"]', 'r', 'shared-key', 'fp1')"
        )
        conn.execute(
            "INSERT INTO promotions (transition_id, strategy_id, strategy_version, from_state, "
            "to_state, at_utc, authority, producer, evidence_ids_json, reason, "
            "idempotency_key, content_fingerprint) "
            "VALUES ('trn_neo', 'EMA_PULLBACK', 'v1.0.0', 'DRAFT', 'IMPLEMENTED', "
            "'2026-01-03T00:00:00+00:00', 'PL', 'NEO', '[\"ev_1\"]', 'r', 'shared-key', 'fp1')"
        )
        # ...but the same producer reusing its own key must not.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO promotions (transition_id, strategy_id, strategy_version, from_state, "
                "to_state, at_utc, authority, producer, evidence_ids_json, reason, "
                "idempotency_key, content_fingerprint) "
                "VALUES ('trn_dup', 'EMA_PULLBACK', 'v1.0.0', 'DRAFT', 'IMPLEMENTED', "
                "'2026-01-03T00:00:00+00:00', 'PL', 'HSA', '[\"ev_1\"]', 'r', 'shared-key', 'fp1')"
            )

        # Re-applying the whole shipped schema directory again is a no-op
        # (003's ALTER TABLEs are never re-run once recorded).
        apply_migrations(conn, real_schema_dir)
        versions = [
            r["version"] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        ]
        assert versions == [1, 2, 3]
        assert conn.execute("SELECT COUNT(*) AS n FROM promotions").fetchone()["n"] == 4
    finally:
        conn.close()
