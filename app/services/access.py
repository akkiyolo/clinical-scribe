"""Access rules shared by routers: consent and ownership checks."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.consent import Consent
from app.models.consult import Consult
from app.models.user import User


def has_active_consent(db: Session, doctor_id: UUID, patient_id: UUID) -> bool:
    return (
        db.query(Consent.id)
        .filter(
            Consent.doctor_id == doctor_id,
            Consent.patient_id == patient_id,
            Consent.revoked_at.is_(None),
        )
        .first()
        is not None
    )


def require_consent(db: Session, doctor_id: UUID, patient_id: UUID) -> None:
    """Raise 403 unless the patient currently allows this doctor access (revocation is immediate)."""
    if not has_active_consent(db, doctor_id, patient_id):
        raise HTTPException(status_code=403, detail="No active consent from this patient")


def owned_consult(db: Session, consult_id: UUID, doctor: User, lock: bool = False) -> Consult:
    """The doctor's own consult, with an active patient consent. Others get 404 (not yours) or 403."""
    query = db.query(Consult).filter(Consult.id == consult_id, Consult.doctor_id == doctor.id)
    consult = (query.with_for_update() if lock else query).first()
    if not consult:
        raise HTTPException(status_code=404, detail="Consult not found")
    require_consent(db, doctor.id, consult.patient_id)
    return consult
