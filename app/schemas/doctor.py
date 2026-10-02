"""Doctor-related schemas."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.auth import check_reg_year, trim_strings


class DoctorVerificationResponse(BaseModel):
    user_id: UUID
    reg_number: str
    council: str
    reg_year: int
    specialization: str
    clinic_name: str | None = None
    clinic_address: str | None = None
    clinic_phone: str | None = None
    status: str
    editable: bool = False
    license_file_id: UUID | None = None
    profile_photo_file_id: UUID | None = None
    rejection_reason: str | None = None
    suspension_reason: str | None = None
    submitted_at: datetime | None = None
    verified_at: datetime | None = None


class DoctorVerificationUpdate(BaseModel):
    _trim = model_validator(mode="before")(classmethod(lambda cls, v: trim_strings(v)))

    reg_number: str | None = Field(default=None, max_length=100)
    council: str | None = Field(default=None, max_length=255)
    reg_year: int | None = None
    specialization: str | None = Field(default=None, max_length=255)
    clinic_name: str | None = Field(default=None, max_length=255)
    clinic_address: str | None = Field(default=None, max_length=1000)
    clinic_phone: str | None = Field(default=None, max_length=20)

    @field_validator("reg_year")
    @classmethod
    def validate_reg_year(cls, v: int | None) -> int | None:
        return check_reg_year(v)


class DoctorListItem(BaseModel):
    id: UUID
    full_name: str
    specialization: str
    clinic_name: str | None = None
    clinic_address: str | None = None
    profile_photo_url: str | None = None
    is_verified: bool = True


class DoctorPatientItem(BaseModel):
    id: UUID
    full_name: str
    email: str
    phone: str | None = None
    dob: str | None = None
    gender: str | None = None
    blood_group: str | None = None
    allergies: str | None = None
    consent_granted_at: str | None = None
