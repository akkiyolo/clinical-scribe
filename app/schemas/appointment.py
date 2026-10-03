"""Appointment schemas."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.auth import trim_strings


class AppointmentCreate(BaseModel):
    _trim = model_validator(mode="before")(classmethod(lambda cls, v: trim_strings(v)))

    doctor_id: UUID
    scheduled_at: datetime
    reason_for_visit: str | None = Field(default=None, max_length=1000)

    @field_validator("scheduled_at")
    @classmethod
    def must_be_upcoming(cls, v: datetime) -> datetime:
        aware = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        if aware < datetime.now(timezone.utc) - timedelta(minutes=5):
            raise ValueError("Appointment time must be in the future")
        return aware


class AppointmentUpdate(BaseModel):
    status: Literal["confirmed", "completed", "cancelled"]


class AppointmentResponse(BaseModel):
    id: UUID
    patient_id: UUID
    doctor_id: UUID
    scheduled_at: datetime
    duration_minutes: int | None = None
    status: str
    reason_for_visit: str | None = None
    created_at: datetime
    patient_name: str | None = None
    doctor_name: str | None = None
