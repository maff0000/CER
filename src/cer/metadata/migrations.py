"""Forward-only SQL migration runner for the CER metadata store.

Migrations are plain ``.sql`` files under a schema directory, named
``<version>_<description>.sql`` where ``<version>`` is an integer used
both for ordering and as the primary key of the ``schema_migrations``
tracking table (e.g. ``001_initial.sql``, ``002_add_x.sql``, ...).

Applying a migration set is idempotent: a version already recorded in
``schema_migrations`` is skipped -- construction of a second store on the
same database file, or a second call to :func:`apply_migrations`, is a
no-op against an up-to-date schema. Every already-applied migration's
checksum (sha256 of the exact file bytes) is re-verified on *every* run
against what was recorded when it was applied. A mismatch means the
migration file was edited after being applied to this database -- that
is exactly the "schema/migration discipline" violation the PID calls
out, so it fails loudly with :class:`~cer.contract.errors.MetadataStoreError`
rather than silently re-running the edited file or ignoring the drift.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cer.contract.errors import MetadataStoreError
from cer.runtime.clock import isoformat_utc, utcnow

_MIGRATION_FILENAME_RE = re.compile(r"^(\d+)_[A-Za-z0-9_]+\.sql$")

__all__ = ["apply_migrations"]


@dataclass(frozen=True)
class _Migration:
    version: int
    filename: str
    sql: str
    checksum: str


def _load_migrations(schema_dir: Path) -> list[_Migration]:
    if not schema_dir.is_dir():
        raise MetadataStoreError(f"migration schema directory {schema_dir} does not exist")

    migrations: list[_Migration] = []
    for path in sorted(schema_dir.glob("*.sql")):
        match = _MIGRATION_FILENAME_RE.match(path.name)
        if not match:
            raise MetadataStoreError(
                f"migration file {path.name!r} does not match the required naming "
                "convention '<version>_<description>.sql'"
            )
        version = int(match.group(1))
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        migrations.append(_Migration(version=version, filename=path.name, sql=sql, checksum=checksum))

    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise MetadataStoreError(
            f"duplicate migration version numbers found in {schema_dir}: {sorted(versions)}"
        )
    migrations.sort(key=lambda m: m.version)
    return migrations


def apply_migrations(conn: sqlite3.Connection, schema_dir: Path) -> int | None:
    """Apply every pending migration in ``schema_dir`` to ``conn``, in order.

    Safe to call every time a store is constructed: already-applied
    versions are skipped, but only after their recorded checksum is
    confirmed to still match the current file content. ``conn`` is
    expected to be in autocommit mode (``isolation_level=None``); this
    function manages its own transaction boundaries explicitly.

    Returns the highest migration version found in ``schema_dir`` -- the
    version this store's schema is expected to be fully migrated to,
    whether that version was just applied by this call or already applied
    by a previous one -- or ``None`` if ``schema_dir`` contains no
    migration files. Callers (see ``SQLiteMetadataStore.health()``) use
    this to detect a schema that has gone missing or been reset out from
    under a live store.
    """
    migrations = _load_migrations(schema_dir)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version        INTEGER PRIMARY KEY,
            filename       TEXT NOT NULL,
            checksum       TEXT NOT NULL,
            applied_at_utc TEXT NOT NULL
        )
        """
    )

    applied_rows = conn.execute(
        "SELECT version, filename, checksum FROM schema_migrations"
    ).fetchall()
    applied_by_version = {row[0]: (row[1], row[2]) for row in applied_rows}

    for migration in migrations:
        if migration.version in applied_by_version:
            _applied_filename, applied_checksum = applied_by_version[migration.version]
            if applied_checksum != migration.checksum:
                raise MetadataStoreError(
                    f"migration {migration.filename!r} (version {migration.version}) has "
                    f"been modified since it was applied to this database: recorded "
                    f"checksum {applied_checksum} does not match the current file's "
                    f"checksum {migration.checksum}. Migrations are forward-only and must "
                    "never be edited after being applied -- add a new numbered migration "
                    "file instead."
                )
            continue

        # Note: Connection.executescript() implicitly commits any open
        # transaction before it runs (a CPython sqlite3 quirk), so a
        # BEGIN...COMMIT wrapped around it here would not actually cover
        # it -- each DDL statement below is applied by SQLite as it is
        # encountered rather than atomically as one unit. That is made
        # safe by every statement in the schema files being written as
        # `... IF NOT EXISTS`: applying them twice (e.g. two threads
        # racing to construct the first store against a brand-new
        # database file) is a harmless no-op, not a partial/inconsistent
        # schema. The tracking-row insert is the only step that must not
        # duplicate, and `INSERT OR IGNORE` plus `version INTEGER PRIMARY
        # KEY` makes that race-safe too: at most one insert of a given
        # version ever lands, and it always has the correct checksum
        # because both racers compute it from the same file content.
        try:
            conn.executescript(migration.sql)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations "
                "(version, filename, checksum, applied_at_utc) VALUES (?, ?, ?, ?)",
                (migration.version, migration.filename, migration.checksum, isoformat_utc(utcnow())),
            )
        except sqlite3.Error as exc:
            raise MetadataStoreError(
                f"failed applying migration {migration.filename!r}: {exc}"
            ) from exc

    return migrations[-1].version if migrations else None
