"""Admin-specific schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.auth import trim_strings


class AdminDoctorItem(BaseModel):
    user_id: UUID
    email: str
    full_name: str
    reg_number: str
    council: str
    reg_year: int
    specialization: str
    status: str
    submitted_at: datetime | None = None
    registry_match: bool | None = None
    name_match_score: float | None = None
    duplicate_reg_flag: bool | None = None
    license_file_id: UUID | None = None


class AdminDoctorDetail(AdminDoctorItem):
    clinic_name: str | None = None
    clinic_address: str | None = None
    clinic_phone: str | None = None
    rejection_reason: str | None = None
    suspension_reason: str | None = None
    verified_at: datetime | None = None
    verified_by: UUID | None = None
    events: list[dict] = Field(default_factory=list)
    checks: list[dict] = Field(default_factory=list)


class AdminActionRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 10:
            raise ValueError("Reason must be at least 10 characters")
        return v


class AdminStatsResponse(BaseModel):
    pending_licenses: int
    verified_doctors: int
    suspended_doctors: int
    rejected_doctors: int
    open_reports: int
    pending_patients: int = 0
    verified_patients: int = 0
    total_patients: int
    total_consults: int


class ReportResponse(BaseModel):
    id: UUID
    reporter_id: UUID
    doctor_id: UUID
    consult_id: UUID | None = None
    reason: str
    details: str | None = None
    created_at: datetime
    resolved: bool = False
    resolved_by: UUID | None = None
    resolution_note: str | None = None
    reporter_name: str | None = None
    doctor_name: str | None = None


class ReportCreate(BaseModel):
    _trim = model_validator(mode="before")(classmethod(lambda cls, v: trim_strings(v)))

    doctor_id: UUID
    consult_id: UUID | None = None
    reason: str = Field(min_length=5, max_length=255)
    details: str | None = Field(default=None, max_length=5000)


class ReportResolve(BaseModel):
    _trim = model_validator(mode="before")(classmethod(lambda cls, v: trim_strings(v)))

    resolution_note: str = Field(min_length=5, max_length=2000)
