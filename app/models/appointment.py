"""Appointment model."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, generate_uuid
from app.models.enums import AppointmentStatus

# A doctor's slot can hold at most one open (requested or confirmed) appointment.
OPEN_APPOINTMENT = text("status IN ('requested', 'confirmed')")


class Appointment(Base, TimestampMixin):
    __tablename__ = "appointments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid
    )
    patient_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus, name="appointment_status", native_enum=True),
        default=AppointmentStatus.requested,
        nullable=False,
        index=True,
    )
    reason_for_visit: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index(
            "ix_appointments_doctor_slot_open",
            "doctor_id",
            "scheduled_at",
            unique=True,
            postgresql_where=OPEN_APPOINTMENT,
            sqlite_where=OPEN_APPOINTMENT,
        ),
    )
