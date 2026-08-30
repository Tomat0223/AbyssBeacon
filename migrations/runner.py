from datetime import datetime, timezone

from . import source_aware_merged_media_v1


MIGRATIONS = (
    source_aware_merged_media_v1,
)


def _ensure_migrations_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS app_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            details TEXT DEFAULT ''
        )
        """
    )


def _already_applied(cursor, migration_id):
    return cursor.execute(
        "SELECT 1 FROM app_migrations WHERE name=? LIMIT 1",
        (str(migration_id),),
    ).fetchone() is not None


def _mark_applied(cursor, migration_id, details=""):
    cursor.execute(
        "INSERT OR REPLACE INTO app_migrations(name,applied_at,details) VALUES(?,?,?)",
        (
            str(migration_id),
            datetime.now(timezone.utc).isoformat(),
            str(details or ""),
        ),
    )


def run_pending_migrations(cursor):
    """Run each unapplied migration once using the caller's transaction.

    A failed migration is deliberately left unmarked so a later startup can
    safely retry it. Migrations should therefore be written to be idempotent.
    """
    _ensure_migrations_table(cursor)
    results = []

    for migration in MIGRATIONS:
        migration_id = str(getattr(migration, "MIGRATION_ID", "")).strip()
        if not migration_id or _already_applied(cursor, migration_id):
            continue

        try:
            result = migration.run(cursor) or {}
            if not isinstance(result, dict):
                result = {"details": str(result)}
            _mark_applied(cursor, migration_id, result.get("details", ""))
            result = dict(result)
            result["migration_id"] = migration_id
            results.append(result)
        except Exception as exc:
            results.append(
                {
                    "migration_id": migration_id,
                    "error": str(exc),
                    "message": (
                        f"Migration {migration_id} deferred: {exc}. "
                        "AbyssBeacon will retry it on the next launch."
                    ),
                }
            )

    return results
