"""Bring an existing, Alembic-unaware PostgreSQL schema under migration control.

Some databases were created from the models directly (not through Alembic), so they hold every
table and enum type of the initial migration but no `alembic_version` table and none of its
foreign keys. `alembic upgrade head` then fails on `CREATE TYPE ... already exists`.

This script detects that state, adds the initial migration's missing foreign keys and stamps the
database at that revision, all in one transaction, so `alembic upgrade head` can apply the rest.
It never drops anything. Fresh databases and databases Alembic already tracks are left alone.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection

from app.config import get_settings

INITIAL_REVISION = "b4278c718bba"

INITIAL_TABLES = {
    "agent_runs", "appointments", "audit_logs", "consents", "consults", "doctor_profiles",
    "files", "patient_profiles", "prescriptions", "registry_records", "reports", "soap_notes",
    "users", "verification_checks", "verification_events",
}  # fmt: skip

# (table, column, referred table) for every foreign key the initial migration creates.
INITIAL_FOREIGN_KEYS = [
    ("agent_runs", "consult_id", "consults"),
    ("consults", "appointment_id", "appointments"),
    ("consults", "audio_file_id", "files"),
    ("consults", "doctor_id", "users"),
    ("consults", "patient_id", "users"),
    ("doctor_profiles", "user_id", "users"),
    ("patient_profiles", "user_id", "users"),
    ("prescriptions", "agent_run_id", "agent_runs"),
    ("prescriptions", "approved_by", "users"),
    ("prescriptions", "consult_id", "consults"),
    ("prescriptions", "doctor_id", "users"),
    ("prescriptions", "docx_file_id", "files"),
    ("prescriptions", "patient_id", "users"),
    ("soap_notes", "consult_id", "consults"),
]


def _missing_foreign_keys(conn: Connection) -> list[tuple[str, str, str]]:
    inspector = inspect(conn)
    missing = []
    for table, column, referred in INITIAL_FOREIGN_KEYS:
        present = any(
            fk["constrained_columns"] == [column] and fk["referred_table"] == referred
            for fk in inspector.get_foreign_keys(table)
        )
        if not present:
            missing.append((table, column, referred))
    return missing


def _orphan_count(conn: Connection, table: str, column: str, referred: str) -> int:
    return conn.execute(
        text(
            f'SELECT count(*) FROM "{table}" t WHERE t."{column}" IS NOT NULL AND NOT EXISTS '
            f'(SELECT 1 FROM "{referred}" r WHERE r.id = t."{column}")'
        )
    ).scalar_one()


def adopt(conn: Connection) -> str:
    """Adopt an unmanaged schema on `conn`. Returns a one-line description of what happened."""
    tables = set(inspect(conn).get_table_names())
    if "alembic_version" in tables:
        return "schema already managed by Alembic; nothing to do"
    if not tables & INITIAL_TABLES:
        return "empty database; migrations will create the schema"
    if not INITIAL_TABLES <= tables:
        raise SystemExit(
            "Database has some ClinicalScribe tables but not all of them and no alembic_version "
            f"table (missing: {', '.join(sorted(INITIAL_TABLES - tables))}). Refusing to guess; "
            "fix or reset the database by hand."
        )

    missing = _missing_foreign_keys(conn)
    orphans = {
        f"{table}.{column} -> {referred}": count
        for table, column, referred in missing
        if (count := _orphan_count(conn, table, column, referred))
    }
    if orphans:
        detail = ", ".join(f"{name} ({count} rows)" for name, count in orphans.items())
        raise SystemExit(f"Cannot add foreign keys: rows point at missing records: {detail}")

    for table, column, referred in missing:
        conn.execute(
            text(
                f'ALTER TABLE "{table}" ADD CONSTRAINT "{table}_{column}_fkey" '
                f'FOREIGN KEY ("{column}") REFERENCES "{referred}" (id)'
            )
        )
    # Same table Alembic itself creates.
    conn.execute(
        text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
    )
    conn.execute(
        text("INSERT INTO alembic_version (version_num) VALUES (:rev)"), {"rev": INITIAL_REVISION}
    )
    return (
        f"adopted existing schema: added {len(missing)} foreign keys, "
        f"stamped revision {INITIAL_REVISION}"
    )


def main() -> None:
    url = get_settings().DATABASE_URL
    if not url.startswith("postgresql"):
        print("adopt_schema: not PostgreSQL; skipping")
        return
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            print(f"adopt_schema: {adopt(conn)}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
