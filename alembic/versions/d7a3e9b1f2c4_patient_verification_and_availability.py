"""Patient identity verification, doctor weekly availability and slot booking

Why: patients must be approved by an admin (from an uploaded ID document) before they can book
or grant consent, and patients book from the open slots a doctor publishes instead of proposing
any time. Existing patients start as "pending" and go through the same review.

- patient_profiles: status (pending/verified/rejected), id_document_file_id, submitted_at,
  verified_by, verified_at, rejection_reason
- file_category gains patient_id_document
- doctor_availability (weekly windows) and doctor_time_off (dated days off)
- appointments.duration_minutes, plus a partial unique index so one slot holds at most one open
  (requested or confirmed) appointment

Revision ID: d7a3e9b1f2c4
Revises: c91d5e2a7f10
Create Date: 2026-10-03 15:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d7a3e9b1f2c4"
down_revision: Union[str, None] = "c91d5e2a7f10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

patient_status = sa.Enum("pending", "verified", "rejected", name="patient_status")
OPEN_APPOINTMENT = sa.text("status IN ('requested', 'confirmed')")


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        patient_status.create(bind, checkfirst=True)
        # ADD VALUE cannot run inside a transaction block on older PostgreSQL versions.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE file_category ADD VALUE IF NOT EXISTS 'patient_id_document'")

    with op.batch_alter_table("patient_profiles") as batch:
        batch.add_column(
            sa.Column(
                "status",
                patient_status,
                server_default="pending",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("id_document_file_id", sa.UUID(), nullable=True))
        batch.add_column(sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("verified_by", sa.UUID(), nullable=True))
        batch.add_column(sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("rejection_reason", sa.Text(), nullable=True))
        batch.create_foreign_key(
            "fk_patient_profiles_id_document_file_id", "files", ["id_document_file_id"], ["id"]
        )
        batch.create_foreign_key(
            "fk_patient_profiles_verified_by", "users", ["verified_by"], ["id"]
        )
    op.create_index(
        op.f("ix_patient_profiles_status"), "patient_profiles", ["status"], unique=False
    )

    op.create_table(
        "doctor_availability",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("doctor_id", sa.UUID(), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("slot_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["doctor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_doctor_availability_doctor_id"), "doctor_availability", ["doctor_id"])

    op.create_table(
        "doctor_time_off",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("doctor_id", sa.UUID(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["doctor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_doctor_time_off_doctor_id"), "doctor_time_off", ["doctor_id"])

    op.add_column("appointments", sa.Column("duration_minutes", sa.Integer(), nullable=True))
    op.create_index(
        "ix_appointments_doctor_slot_open",
        "appointments",
        ["doctor_id", "scheduled_at"],
        unique=True,
        postgresql_where=OPEN_APPOINTMENT,
        sqlite_where=OPEN_APPOINTMENT,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_appointments_doctor_slot_open", table_name="appointments")
    with op.batch_alter_table("appointments") as batch:
        batch.drop_column("duration_minutes")

    op.drop_index(op.f("ix_doctor_time_off_doctor_id"), table_name="doctor_time_off")
    op.drop_table("doctor_time_off")
    op.drop_index(op.f("ix_doctor_availability_doctor_id"), table_name="doctor_availability")
    op.drop_table("doctor_availability")

    op.drop_index(op.f("ix_patient_profiles_status"), table_name="patient_profiles")
    with op.batch_alter_table("patient_profiles") as batch:
        batch.drop_constraint("fk_patient_profiles_verified_by", type_="foreignkey")
        batch.drop_constraint("fk_patient_profiles_id_document_file_id", type_="foreignkey")
        for column in (
            "rejection_reason",
            "verified_at",
            "verified_by",
            "submitted_at",
            "id_document_file_id",
            "status",
        ):
            batch.drop_column(column)

    if bind.dialect.name == "postgresql":
        patient_status.drop(bind, checkfirst=True)
        # PostgreSQL cannot drop a single enum value; 'patient_id_document' stays in
        # file_category, unused, which is harmless.
