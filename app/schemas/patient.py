"""Patient-related schemas."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.auth import trim_strings


class PatientProfileUpdate(BaseModel):
    _trim = model_validator(mode="before")(classmethod(lambda cls, v: trim_strings(v)))

    full_name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=20)
    dob: date | None = None
    gender: str | None = Field(default=None, max_length=20)
    blood_group: str | None = Field(default=None, max_length=10)
    allergies: str | None = Field(default=None, max_length=2000)

    @field_validator("dob")
    @classmethod
    def validate_dob(cls, v: date | None) -> date | None:
        if v is not None and not (date(1900, 1, 1) <= v <= date.today()):
            raise ValueError("Date of birth must be between 1900 and today")
        return v


class PatientProfileResponse(BaseModel):
    id: UUID
    email: str
    full_name: str
    phone: str | None = None
    dob: date | None = None
    gender: str | None = None
    blood_group: str | None = None
    allergies: str | None = None
    profile_photo_url: str | None = None
