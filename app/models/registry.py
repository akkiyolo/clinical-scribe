"""Registry records and verification models."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, generate_uuid
from app.models.enums import VerificationAction


class RegistryRecord(Base):
    """Mock medical registry records for verification demos."""

    __tablename__ = "registry_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid
    )
    reg_number: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    council: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    reg_year: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class VerificationCheck(Base):
    """Results of automated registry checks."""

    __tablename__ = "verification_checks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    registry_match: Mapped[bool] = mapped_column(Boolean, nullable=False)
    name_match_score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    duplicate_reg_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_result: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )


class VerificationEvent(Base):
    """Append-only log of verification state transitions.

    MUST NOT be updated or deleted. Only insert operations are exposed.
    """

    __tablename__ = "verification_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=generate_uuid
    )
    doctor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    action: Mapped[VerificationAction] = mapped_column(
        Enum(VerificationAction, name="verification_action", native_enum=True),
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
