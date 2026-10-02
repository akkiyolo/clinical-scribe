"""Enum types shared across models."""

from __future__ import annotations

import enum


class UserRole(str, enum.Enum):
    patient = "patient"
    doctor = "doctor"
    admin = "admin"


class DoctorStatus(str, enum.Enum):
    pending = "pending"
    verified = "verified"
    rejected = "rejected"
    suspended = "suspended"


class FileCategory(str, enum.Enum):
    profile_photo = "profile_photo"
    license_certificate = "license_certificate"
    consult_audio = "consult_audio"
    prescription_docx = "prescription_docx"
    prescription_audio = "prescription_audio"


class AppointmentStatus(str, enum.Enum):
    requested = "requested"
    confirmed = "confirmed"
    completed = "completed"
    cancelled = "cancelled"


class TranscriptionStatus(str, enum.Enum):
    none = "none"
    uploaded = "uploaded"
    transcribing = "transcribing"
    ready = "ready"
    failed = "failed"


class ConsultStatus(str, enum.Enum):
    draft = "draft"
    soap_generating = "soap_generating"
    soap_ready = "soap_ready"
    soap_approved = "soap_approved"
    prescription_generating = "prescription_generating"
    prescription_ready = "prescription_ready"
    completed = "completed"
    failed = "failed"


class SOAPStatus(str, enum.Enum):
    draft = "draft"
    approved = "approved"


class PrescriptionStatus(str, enum.Enum):
    draft = "draft"
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"


class AgentRunStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class VerificationAction(str, enum.Enum):
    submitted = "submitted"
    auto_checked = "auto_checked"
    approved = "approved"
    rejected = "rejected"
    resubmitted = "resubmitted"
    suspended = "suspended"
    reinstated = "reinstated"
