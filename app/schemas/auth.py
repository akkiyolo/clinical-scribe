"""Auth request/response schemas."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
DOCTOR_REQUIRED = ("reg_number", "council", "reg_year", "specialization")


def trim_strings(values: Any) -> Any:
    """Trim every string in a request body; blank optional strings become None."""
    if isinstance(values, dict):
        return {k: (v.strip() or None) if isinstance(v, str) else v for k, v in values.items()}
    return values


class RegisterRequest(BaseModel):
    _trim = model_validator(mode="before")(classmethod(lambda cls, v: trim_strings(v)))

    email: str = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)
    role: Literal["patient", "doctor"]
    phone: str | None = Field(default=None, max_length=20)

    # Patient extras
    dob: date | None = None
    gender: str | None = Field(default=None, max_length=20)
    blood_group: str | None = Field(default=None, max_length=10)
    allergies: str | None = Field(default=None, max_length=2000)

    # Doctor extras
    reg_number: str | None = Field(default=None, max_length=100)
    council: str | None = Field(default=None, max_length=255)
    reg_year: int | None = None
    specialization: str | None = Field(default=None, max_length=255)
    clinic_name: str | None = Field(default=None, max_length=255)
    clinic_address: str | None = Field(default=None, max_length=1000)
    clinic_phone: str | None = Field(default=None, max_length=20)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: str) -> str:
        if not EMAIL_PATTERN.match(v):
            raise ValueError("Invalid email format")
        return v

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        if not re.search(r"[a-zA-Z]", v):
            raise ValueError("Password must contain at least one letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit")
        return v

    @field_validator("reg_year")
    @classmethod
    def validate_reg_year(cls, v: int | None) -> int | None:
        return check_reg_year(v)

    @field_validator("dob")
    @classmethod
    def validate_dob(cls, v: date | None) -> date | None:
        if v is not None and not (date(1900, 1, 1) <= v <= date.today()):
            raise ValueError("Date of birth must be between 1900 and today")
        return v

    @model_validator(mode="after")
    def doctor_fields_required(self) -> "RegisterRequest":
        if self.role == "doctor":
            missing = [name for name in DOCTOR_REQUIRED if getattr(self, name) in (None, "")]
            if missing:
                raise ValueError("Doctors must provide: " + ", ".join(missing))
        return self


def check_reg_year(v: int | None) -> int | None:
    """Registration year must be between 1950 and the current year."""
    if v is not None:
        current_year = date.today().year
        if v < 1950 or v > current_year:
            raise ValueError(f"Registration year must be between 1950 and {current_year}")
    return v


class LoginRequest(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=128)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.strip().lower()


class AuthResponse(BaseModel):
    id: UUID
    email: str
    full_name: str
    role: str
    doctor_status: str | None = None
    rejection_reason: str | None = None
    suspension_reason: str | None = None
    profile_photo_url: str | None = None


class MeResponse(BaseModel):
    id: UUID
    email: str
    full_name: str
    role: str
    phone: str | None = None
    doctor_status: str | None = None
    rejection_reason: str | None = None
    suspension_reason: str | None = None
    profile_photo_url: str | None = None
