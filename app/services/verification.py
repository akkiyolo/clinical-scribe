"""Doctor license verification: state machine, registry checker and auto-check evidence.

The registry auto-check is evidence for the admin reviewing a license. It never changes a
doctor's status; only `transition_doctor_status` does, and only for the actors the spec allows.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from types import SimpleNamespace

from pydantic import BaseModel
from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from app.models.doctor import DoctorProfile
from app.models.enums import DoctorStatus, VerificationAction
from app.models.registry import RegistryRecord, VerificationCheck, VerificationEvent
from app.services.audit import audit

logger = logging.getLogger(__name__)

# The system actor performs the (new) -> pending transition at registration.
SYSTEM_ACTOR = SimpleNamespace(id=None, role=SimpleNamespace(value="system"))

MIN_REASON_LENGTH = 10

# (from_status, to_status) -> (required actor role, reason required, verification action)
ALLOWED_TRANSITIONS: dict[tuple[str | None, str], tuple[str, bool, VerificationAction]] = {
    (None, "pending"): ("system", False, VerificationAction.submitted),
    ("pending", "verified"): ("admin", False, VerificationAction.approved),
    ("pending", "rejected"): ("admin", True, VerificationAction.rejected),
    ("rejected", "pending"): ("doctor", False, VerificationAction.resubmitted),
    ("verified", "suspended"): ("admin", True, VerificationAction.suspended),
    ("suspended", "verified"): ("admin", True, VerificationAction.reinstated),
}

# Audit action names use the spec's dot notation, e.g. verification.approve.
AUDIT_ACTION_NAMES = {
    VerificationAction.submitted: "verification.submit",
    VerificationAction.approved: "verification.approve",
    VerificationAction.rejected: "verification.reject",
    VerificationAction.resubmitted: "verification.resubmit",
    VerificationAction.suspended: "verification.suspend",
    VerificationAction.reinstated: "verification.reinstate",
}


class TransitionError(ValueError):
    """Raised when a verification transition is refused (wrong actor, missing reason, ...)."""


class InvalidTransition(TransitionError):
    """The requested (from, to) pair is not in the transition table."""


def transition_doctor_status(
    db: Session,
    doctor: DoctorProfile,
    to_status: str,
    actor: object,
    reason: str | None = None,
    request=None,
) -> DoctorProfile:
    """Move a doctor between verification states. This is the only place status changes.

    Validates the transition table (including the actor and the reason rule), updates the
    profile, then writes an append-only verification event and an audit log entry.
    Raises TransitionError for anything not allowed.
    """
    from_status = doctor.status.value if doctor.status else None
    entry = ALLOWED_TRANSITIONS.get((from_status, to_status))
    if entry is None:
        raise InvalidTransition(f"Invalid status transition: {from_status or 'new'} -> {to_status}")

    required_actor, reason_required, action = entry
    actor_role = getattr(getattr(actor, "role", None), "value", None)
    if actor_role != required_actor:
        raise TransitionError(f"A {required_actor} is required for this transition")
    if required_actor == "doctor" and getattr(actor, "id", None) != doctor.user_id:
        raise TransitionError("Only the doctor concerned can resubmit")

    clean_reason = (reason or "").strip()
    if reason_required and len(clean_reason) < MIN_REASON_LENGTH:
        raise TransitionError(f"A reason of at least {MIN_REASON_LENGTH} characters is required")
    if to_status == "verified" and from_status == "pending" and not doctor.license_file_id:
        raise TransitionError("The doctor has not uploaded a license certificate")

    now = datetime.now(timezone.utc)
    doctor.status = DoctorStatus(to_status)
    if to_status == "verified":
        doctor.rejection_reason = None
        doctor.suspension_reason = None
        if from_status == "pending":
            doctor.verified_by = actor.id
            doctor.verified_at = now
    elif to_status == "rejected":
        doctor.rejection_reason = clean_reason
    elif to_status == "suspended":
        doctor.suspension_reason = clean_reason
    elif to_status == "pending":
        doctor.submitted_at = now
        doctor.rejection_reason = None

    db.add(
        VerificationEvent(
            doctor_id=doctor.user_id,
            actor_id=getattr(actor, "id", None),
            action=action,
            from_status=from_status,
            to_status=to_status,
            reason=clean_reason or None,
        )
    )
    audit(
        db,
        actor if getattr(actor, "id", None) else None,
        AUDIT_ACTION_NAMES[action],
        "doctor_profile",
        str(doctor.user_id),
        request,
        {"from": from_status, "to": to_status, "actor_role": actor_role},
    )
    db.flush()
    return doctor


# ── Registry checker interface and implementations ────────────────────────────


class RegistryResult(BaseModel):
    registry_match: bool
    name_match_score: float  # 0-100
    matched_record: dict | None
    notes: str


class RegistryChecker(ABC):
    @abstractmethod
    def check(self, reg_number: str, council: str, full_name: str) -> RegistryResult: ...


_TITLES = re.compile(r"\b(dr|doctor|prof|professor|mr|mrs|ms|miss)\b\.?", re.IGNORECASE)


def normalize_name(name: str) -> str:
    """Lower-case a name, drop titles such as 'Dr.' and punctuation, collapse whitespace."""
    name = _TITLES.sub(" ", name or "")
    name = re.sub(r"[^a-zA-Z\s]", " ", name)
    return " ".join(name.lower().split())


def normalize_reg_number(reg_number: str) -> str:
    """Case, spacing and punctuation insensitive registration number (MH-2015/12345 == mh201512345)."""
    return re.sub(r"[^a-z0-9]", "", (reg_number or "").lower())


def normalize_council(council: str) -> str:
    return " ".join((council or "").lower().split())


class MockRegistryChecker(RegistryChecker):
    """Looks the doctor up in the registry_records table and scores name similarity."""

    def __init__(self, db: Session):
        self.db = db

    def check(self, reg_number: str, council: str, full_name: str) -> RegistryResult:
        wanted_reg = normalize_reg_number(reg_number)
        wanted_council = normalize_council(council)
        record = next(
            (
                r
                for r in self.db.query(RegistryRecord).all()
                if normalize_reg_number(r.reg_number) == wanted_reg
                and normalize_council(r.council) == wanted_council
            ),
            None,
        )
        if record is None:
            return RegistryResult(
                registry_match=False,
                name_match_score=0.0,
                matched_record=None,
                notes="No matching registration found in the registry",
            )

        score = float(
            fuzz.token_sort_ratio(normalize_name(full_name), normalize_name(record.full_name))
        )
        return RegistryResult(
            registry_match=True,
            name_match_score=round(score, 2),
            matched_record={
                "reg_number": record.reg_number,
                "council": record.council,
                "full_name": record.full_name,
                "reg_year": record.reg_year,
                "is_active": record.is_active,
            },
            notes=(
                f"Registry match found. Name similarity: {score:.1f}%. "
                f"{'Active' if record.is_active else 'INACTIVE'} registration."
            ),
        )


class HPRRegistryChecker(RegistryChecker):
    """Stub for the ABDM Healthcare Professionals Registry (HPR).

    In production this would call the ABDM HPR API to verify the doctor's registration
    against the national registry. Not implemented, and there is deliberately no scraping
    of any medical council website.
    """

    def check(self, reg_number: str, council: str, full_name: str) -> RegistryResult:
        raise NotImplementedError(
            "HPR integration is not implemented. "
            "In production this would call the ABDM Healthcare Professionals Registry API."
        )


def get_registry_checker(db: Session) -> RegistryChecker:
    """Registry checker selected by configuration (the mock is the only working one)."""
    return MockRegistryChecker(db)


def has_duplicate_registration(db: Session, doctor: DoctorProfile) -> bool:
    """Another non-rejected doctor already uses the same (council, registration number)."""
    wanted_reg = normalize_reg_number(doctor.reg_number)
    wanted_council = normalize_council(doctor.council)
    others = (
        db.query(DoctorProfile)
        .filter(
            DoctorProfile.user_id != doctor.user_id,
            DoctorProfile.status != DoctorStatus.rejected,
        )
        .all()
    )
    return any(
        normalize_reg_number(o.reg_number) == wanted_reg
        and normalize_council(o.council) == wanted_council
        for o in others
    )


def run_auto_check(db: Session, doctor: DoctorProfile) -> VerificationCheck:
    """Record registry evidence for a doctor. Never changes the doctor's status."""
    result = get_registry_checker(db).check(
        reg_number=doctor.reg_number,
        council=doctor.council,
        full_name=doctor.user.full_name if doctor.user else "",
    )
    check = VerificationCheck(
        doctor_id=doctor.user_id,
        registry_match=result.registry_match,
        name_match_score=result.name_match_score,
        duplicate_reg_flag=has_duplicate_registration(db, doctor),
        raw_result=result.model_dump(),
    )
    db.add(check)

    current = doctor.status.value if doctor.status else None
    db.add(
        VerificationEvent(
            doctor_id=doctor.user_id,
            actor_id=None,
            action=VerificationAction.auto_checked,
            from_status=current,
            to_status=current,
            reason=result.notes,
        )
    )
    db.flush()
    return check


def safe_auto_check(db: Session, doctor: DoctorProfile) -> VerificationCheck | None:
    """run_auto_check that never lets a checker failure block registration or resubmission."""
    try:
        with db.begin_nested():
            return run_auto_check(db, doctor)
    except Exception:
        logger.exception("Registry auto-check failed for doctor %s", doctor.user_id)
        return None
