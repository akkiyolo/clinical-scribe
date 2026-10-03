"""Consents router: patients grant and revoke a doctor's access to their data."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.db import get_db
from app.deps import get_current_user, require_role, require_verified_patient
from app.models.consent import Consent
from app.models.doctor import DoctorProfile
from app.models.enums import DoctorStatus, UserRole
from app.models.user import User
from app.schemas.consent import ConsentCreate, ConsentResponse
from app.services.audit import audit

router = APIRouter(prefix="/api/consents", tags=["consents"])


def _response(
    consent: Consent, doctor_name: str | None, patient_name: str | None
) -> ConsentResponse:
    return ConsentResponse(
        id=consent.id,
        patient_id=consent.patient_id,
        doctor_id=consent.doctor_id,
        granted_at=consent.granted_at,
        revoked_at=consent.revoked_at,
        doctor_name=doctor_name,
        patient_name=patient_name,
    )


@router.get("")
def list_consents(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """A patient's consents (who can see their data) or a verified doctor's (who allowed them)."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    doctor_user, patient_user = aliased(User), aliased(User)
    query = (
        db.query(Consent, doctor_user.full_name, patient_user.full_name)
        .join(doctor_user, doctor_user.id == Consent.doctor_id)
        .join(patient_user, patient_user.id == Consent.patient_id)
    )
    if current_user.role == UserRole.patient:
        query = query.filter(Consent.patient_id == current_user.id)
    elif current_user.role == UserRole.doctor:
        profile = db.get(DoctorProfile, current_user.id)
        if not profile or profile.status != DoctorStatus.verified:
            raise HTTPException(status_code=403, detail="Your account is not verified yet")
        query = query.filter(Consent.doctor_id == current_user.id, Consent.revoked_at.is_(None))
    else:
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    total = query.count()
    rows = query.order_by(Consent.granted_at.desc()).offset(offset).limit(limit).all()
    return {
        "items": [_response(c, d, p) for c, d, p in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("", status_code=201)
def grant_consent(
    request: Request,
    body: ConsentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_patient),
) -> ConsentResponse:
    """Grant a verified doctor access to your records."""
    profile = db.get(DoctorProfile, body.doctor_id)
    doctor = db.get(User, body.doctor_id)
    if not profile or not doctor or profile.status != DoctorStatus.verified:
        raise HTTPException(status_code=400, detail="Doctor not found or not verified")

    active = (
        db.query(Consent.id)
        .filter(
            Consent.patient_id == current_user.id,
            Consent.doctor_id == body.doctor_id,
            Consent.revoked_at.is_(None),
        )
        .first()
    )
    if active:
        raise HTTPException(status_code=409, detail="You have already granted this doctor access")

    consent = Consent(patient_id=current_user.id, doctor_id=body.doctor_id)
    try:
        db.add(consent)
        db.flush()
    except IntegrityError:  # two simultaneous clicks: the unique index keeps one active consent
        db.rollback()
        raise HTTPException(status_code=409, detail="You have already granted this doctor access")
    audit(
        db,
        current_user,
        "consent.grant",
        "consent",
        str(consent.id),
        request,
        {"doctor_id": str(body.doctor_id)},
    )
    db.commit()
    db.refresh(consent)
    return _response(consent, doctor.full_name, current_user.full_name)


@router.delete("/{consent_id}")
def revoke_consent(
    consent_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["patient"])),
) -> dict:
    """Revoke a consent. The doctor loses access immediately; approved prescriptions stay yours."""
    consent = db.query(Consent).filter(Consent.id == consent_id).with_for_update().first()
    if not consent:
        raise HTTPException(status_code=404, detail="Consent not found")
    if consent.patient_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your consent")
    if consent.revoked_at is not None:
        raise HTTPException(status_code=409, detail="Consent already revoked")

    consent.revoked_at = datetime.now(timezone.utc)
    audit(
        db,
        current_user,
        "consent.revoke",
        "consent",
        str(consent_id),
        request,
        {"doctor_id": str(consent.doctor_id)},
    )
    db.commit()
    return {"detail": "Consent revoked"}
