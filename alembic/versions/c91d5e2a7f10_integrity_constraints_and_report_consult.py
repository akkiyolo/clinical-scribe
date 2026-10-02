"""Foreign keys, report->consult link, SQLite partial indexes, append-only triggers

Why: the initial migration left most user/file/consult references as bare UUID columns, so the
database could not enforce referential integrity the spec requires (section 7). Approval codes become unique. Agent runs gain a created_at so
the sweeper can age out never-started runs. Reports also had
no way to point at a consult, which the admin "report context" access rule needs. On SQLite the
partial unique indexes were created as full unique indexes, which blocks legitimate resubmission
and re-consent flows. On PostgreSQL, the append-only tables (audit_logs, verification_events) now
reject UPDATE, DELETE and TRUNCATE at the database level (spec 7.3).

Revision ID: c91d5e2a7f10
Revises: b4278c718bba
Create Date: 2026-10-02 14:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c91d5e2a7f10"
down_revision: Union[str, None] = "b4278c718bba"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, constraint name, local column, referred table)
FOREIGN_KEYS = [
    ("users", "fk_users_profile_photo_file_id", "profile_photo_file_id", "files"),
    ("doctor_profiles", "fk_doctor_profiles_license_file_id", "license_file_id", "files"),
    ("doctor_profiles", "fk_doctor_profiles_verified_by", "verified_by", "users"),
    ("files", "fk_files_owner_id", "owner_id", "users"),
    ("appointments", "fk_appointments_patient_id", "patient_id", "users"),
    ("appointments", "fk_appointments_doctor_id", "doctor_id", "users"),
    ("consents", "fk_consents_patient_id", "patient_id", "users"),
    ("consents", "fk_consents_doctor_id", "doctor_id", "users"),
    ("reports", "fk_reports_reporter_id", "reporter_id", "users"),
    ("reports", "fk_reports_doctor_id", "doctor_id", "users"),
    ("reports", "fk_reports_resolved_by", "resolved_by", "users"),
    ("reports", "fk_reports_consult_id", "consult_id", "consults"),
    ("verification_checks", "fk_verification_checks_doctor_id", "doctor_id", "users"),
    ("verification_events", "fk_verification_events_doctor_id", "doctor_id", "users"),
    ("verification_events", "fk_verification_events_actor_id", "actor_id", "users"),
]

APPEND_ONLY_TABLES = ("audit_logs", "verification_events")


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Lets the stuck-job sweeper age out runs that were queued but never started.
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            )
        )

    op.create_index(
        "ix_prescriptions_approval_code", "prescriptions", ["approval_code"], unique=True
    )

    op.add_column("reports", sa.Column("consult_id", sa.UUID(), nullable=True))
    op.create_index(op.f("ix_reports_consult_id"), "reports", ["consult_id"], unique=False)

    by_table: dict[str, list[tuple[str, str, str]]] = {}
    for table, name, column, referred in FOREIGN_KEYS:
        by_table.setdefault(table, []).append((name, column, referred))
    for table, items in by_table.items():
        with op.batch_alter_table(table) as batch:
            for name, column, referred in items:
                batch.create_foreign_key(name, referred, [column], ["id"])

    if dialect == "sqlite":
        op.drop_index("ix_consents_active_unique", table_name="consents")
        op.create_index(
            "ix_consents_active_unique",
            "consents",
            ["patient_id", "doctor_id"],
            unique=True,
            sqlite_where=sa.text("revoked_at IS NULL"),
        )
        op.drop_index("ix_doctor_profiles_council_reg_unique", table_name="doctor_profiles")
        op.create_index(
            "ix_doctor_profiles_council_reg_unique",
            "doctor_profiles",
            ["council", "reg_number"],
            unique=True,
            sqlite_where=sa.text("status != 'rejected'"),
        )

    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION forbid_append_only_mutation() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in APPEND_ONLY_TABLES:
            op.execute(
                f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION forbid_append_only_mutation()"
            )
            op.execute(
                f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_mutation()"
            )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        for table in APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update_delete ON {table}")
        op.execute("DROP FUNCTION IF EXISTS forbid_append_only_mutation()")

    by_table: dict[str, list[str]] = {}
    for table, name, _column, _referred in FOREIGN_KEYS:
        by_table.setdefault(table, []).append(name)
    for table, names in by_table.items():
        with op.batch_alter_table(table) as batch:
            for name in names:
                batch.drop_constraint(name, type_="foreignkey")

    op.drop_index("ix_prescriptions_approval_code", table_name="prescriptions")
    op.drop_index(op.f("ix_reports_consult_id"), table_name="reports")
    with op.batch_alter_table("reports") as batch:
        batch.drop_column("consult_id")
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_column("created_at")
