"""Doctor profile model."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.enums import DoctorStatus


class DoctorProfile(Base):
    __tablename__ = "doctor_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), primary_key=True
    )
    reg_number: Mapped[str] = mapped_column(String(100), nullable=False)
    council: Mapped[str] = mapped_column(String(255), nullable=False)
    reg_year: Mapped[int] = mapped_column(Integer, nullable=False)
    specialization: Mapped[str] = mapped_column(String(255), nullable=False)
    clinic_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    clinic_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    clinic_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[DoctorStatus] = mapped_column(
        Enum(DoctorStatus, name="doctor_status", native_enum=True),
        default=DoctorStatus.pending,
        nullable=False,
        index=True,
    )
    license_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", name="fk_doctor_profiles_license_file_id", use_alter=True),
        nullable=True,
    )
    verified_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    suspension_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", back_populates="doctor_profile", foreign_keys=[user_id])

    __table_args__ = (
        # Partial unique index: unique (council, reg_number) where status != rejected
        Index(
            "ix_doctor_profiles_council_reg_unique",
            "council",
            "reg_number",
            unique=True,
            postgresql_where=(status != DoctorStatus.rejected),
            sqlite_where=(status != DoctorStatus.rejected),
        ),
    )
