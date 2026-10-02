"""Consult and SOAP schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.schemas.auth import trim_strings


class ConsultCreate(BaseModel):
    patient_id: UUID
    appointment_id: UUID | None = None


class RegenerateRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class LatestPrescription(BaseModel):
    id: UUID
    version: int
    status: str


class ConsultResponse(BaseModel):
    """Full consult as the owning doctor sees it."""

    id: UUID
    appointment_id: UUID | None = None
    doctor_id: UUID
    patient_id: UUID
    audio_file_id: UUID | None = None
    transcription_status: str
    transcription_provider: str
    llm_provider: str
    transcript_text: str | None = None
    transcript_edited: bool = False
    status: str
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    patient_name: str | None = None
    doctor_name: str | None = None
    latest_prescription: LatestPrescription | None = None


class ConsultMeta(BaseModel):
    """Metadata only: what patients and admins may see (no transcript, SOAP or notes)."""

    id: UUID
    appointment_id: UUID | None = None
    doctor_id: UUID
    patient_id: UUID
    status: str
    created_at: datetime
    patient_name: str | None = None
    doctor_name: str | None = None


class TranscriptUpdate(BaseModel):
    transcript_text: str = Field(min_length=1, max_length=100000)


class ICD10Item(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    description: str = Field(default="", max_length=300)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class SOAPResponse(BaseModel):
    id: UUID
    consult_id: UUID
    subjective: str | None = None
    objective: str | None = None
    assessment: str | None = None
    plan: str | None = None
    icd10_codes: list[ICD10Item] | None = None
    status: str
    approved_at: datetime | None = None


class SOAPUpdate(BaseModel):
    _trim = model_validator(mode="before")(classmethod(lambda cls, v: trim_strings(v)))

    subjective: str | None = Field(default=None, max_length=10000)
    objective: str | None = Field(default=None, max_length=10000)
    assessment: str | None = Field(default=None, max_length=10000)
    plan: str | None = Field(default=None, max_length=10000)
    icd10_codes: list[ICD10Item] | None = Field(default=None, max_length=30)
