"""Prescription schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class Medication(BaseModel):
    drug_name: str = Field(max_length=200)
    strength: str | None = Field(default=None, max_length=100)
    dose: str | None = Field(default=None, max_length=100)
    route: str | None = Field(default=None, max_length=100)
    frequency: str | None = Field(default=None, max_length=200)
    duration: str | None = Field(default=None, max_length=100)
    instructions: str | None = Field(default=None, max_length=500)
    source_quote: str | None = Field(default=None, max_length=1000)


class PrescriptionContent(BaseModel):
    diagnosis: list[str] = Field(default_factory=list, max_length=30)
    icd10: list[dict] = Field(default_factory=list, max_length=30)
    medications: list[Medication] = Field(default_factory=list, max_length=30)
    tests_advised: list[str] = Field(default_factory=list, max_length=30)
    advice: list[str] = Field(default_factory=list, max_length=30)
    follow_up: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)


class SafetyFlag(BaseModel):
    id: str
    type: str
    severity: str  # high | medium | low
    field_ref: str | None = None
    message: str
    acknowledged: bool | None = None  # set on approval for high-severity flags


class PrescriptionResponse(BaseModel):
    id: UUID
    consult_id: UUID
    doctor_id: UUID
    patient_id: UUID
    version: int
    status: str
    content: PrescriptionContent | None = None
    safety_flags: list[SafetyFlag] | None = None
    docx_file_id: UUID | None = None
    agent_run_id: UUID | None = None
    approved_by: UUID | None = None
    approved_at: datetime | None = None
    approval_code: str | None = None
    created_at: datetime
    doctor_name: str | None = None
    patient_name: str | None = None


class PrescriptionUpdate(BaseModel):
    content: PrescriptionContent
    # Version the doctor was editing; a mismatch means someone else changed it (409).
    expected_version: int | None = None


class PrescriptionApproval(BaseModel):
    acknowledged_flag_ids: list[str] = Field(default_factory=list, max_length=200)


class PrescriptionReject(BaseModel):
    regenerate: bool = False
    note: str | None = Field(default=None, max_length=1000)


class PrescriptionPatientView(BaseModel):
    """What a patient sees — no drafts, flags, or source quotes."""

    id: UUID
    consult_id: UUID
    doctor_id: UUID
    doctor_name: str | None = None
    version: int
    status: str
    diagnosis: list[str] = Field(default_factory=list)
    medications: list[dict] = Field(default_factory=list)
    tests_advised: list[str] = Field(default_factory=list)
    advice: list[str] = Field(default_factory=list)
    follow_up: str | None = None
    notes: str | None = None
    approved_at: datetime | None = None
    approval_code: str | None = None
    docx_file_id: UUID | None = None
