"""Consent schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class ConsentCreate(BaseModel):
    doctor_id: UUID


class ConsentResponse(BaseModel):
    id: UUID
    patient_id: UUID
    doctor_id: UUID
    granted_at: datetime
    revoked_at: datetime | None = None
    doctor_name: str | None = None
    patient_name: str | None = None
