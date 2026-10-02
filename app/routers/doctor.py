"""Doctor router: license verification, public directory, consented patients, dashboard."""

import logging
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi import File as FastAPIFile
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, require_role, require_verified_doctor
from app.models.appointment import Appointment
from app.models.consent import Consent
from app.models.consult import Consult
from app.models.doctor import DoctorProfile
from app.models.enums import (
    AppointmentStatus,
    ConsultStatus,
    DoctorStatus,
    FileCategory,
    TranscriptionStatus,
    UserRole,
)
from app.models.file import File
from app.models.patient import PatientProfile
from app.models.registry import VerificationEvent
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.doctor import (
    DoctorListItem,
    DoctorPatientItem,
    DoctorVerificationResponse,
    DoctorVerificationUpdate,
)
from app.services.audit import audit
from app.services.timeutil import as_utc
from app.services.uploads import read_validated_upload, store_upload
from app.services.verification import safe_auto_check, transition_doctor_status

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["doctor"])

EDITABLE_STATUSES = (DoctorStatus.pending, DoctorStatus.rejected)


def _profile_or_404(db: Session, user: User) -> DoctorProfile:
    profile = db.get(DoctorProfile, user.id)
    if not profile:
        raise HTTPException(status_code=404, detail="Doctor profile not found")
    return profile


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/doctor/verification")
def get_verification(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["doctor"])),
) -> DoctorVerificationResponse:
    """The doctor's own submission and verification status (available in every status)."""
    profile = _profile_or_404(db, current_user)
    return DoctorVerificationResponse(
        user_id=profile.user_id,
        reg_number=profile.reg_number,
        council=profile.council,
        reg_year=profile.reg_year,
        specialization=profile.specialization,
        clinic_name=profile.clinic_name,
        clinic_address=profile.clinic_address,
        clinic_phone=profile.clinic_phone,
        status=profile.status.value,
        editable=profile.status in EDITABLE_STATUSES,
        license_file_id=profile.license_file_id,
        profile_photo_file_id=current_user.profile_photo_file_id,
        rejection_reason=profile.rejection_reason,
        suspension_reason=profile.suspension_reason,
        submitted_at=profile.submitted_at,
        verified_at=profile.verified_at,
    )


@router.patch("/doctor/verification")
def update_verification(
    request: Request,
    body: DoctorVerificationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["doctor"])),
) -> dict:
    """Edit submitted details; only while pending or rejected."""
    profile = _profile_or_404(db, current_user)
    if profile.status not in EDITABLE_STATUSES:
        raise HTTPException(
            status_code=400, detail="Details can only be edited while pending or rejected"
        )

    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    identity_changed = any(
        k in changes and changes[k] != getattr(profile, k) for k in ("reg_number", "council")
    )
    for field, value in changes.items():
        setattr(profile, field, value)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="These registration details are already in use")

    if identity_changed and profile.status == DoctorStatus.pending:
        safe_auto_check(db, profile)  # fresh registry evidence for the admin
    audit(
        db,
        current_user,
        "doctor.verification.update",
        "doctor_profile",
        str(current_user.id),
        request,
        {"fields": sorted(changes)},
    )
    db.commit()
    return {"detail": "Details updated"}


@router.post("/doctor/license")
@limiter.limit("20/minute")
def upload_license(
    request: Request,
    file: UploadFile = FastAPIFile(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["doctor"])),
) -> dict:
    """Upload the license certificate (pdf/jpg/png, max 10 MB). The previous file is kept."""
    profile = _profile_or_404(db, current_user)
    upload = read_validated_upload(file, "license_certificate")
    record = store_upload(db, current_user.id, "doctor", FileCategory.license_certificate, upload)
    profile.license_file_id = record.id
    audit(db, current_user, "doctor.license.upload", "file", str(record.id), request)
    db.commit()
    return {
        "detail": "License uploaded",
        "file_id": str(record.id),
        "doctor_status": profile.status.value,
    }


@router.post("/doctor/resubmit")
def resubmit_verification(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["doctor"])),
) -> dict:
    """Resubmit after a rejection: needs a certificate uploaded since the rejection."""
    profile = _profile_or_404(db, current_user)
    if profile.status != DoctorStatus.rejected:
        raise HTTPException(status_code=400, detail="Only a rejected submission can be resubmitted")

    rejected_at = (
        db.query(func.max(VerificationEvent.created_at))
        .filter(
            VerificationEvent.doctor_id == profile.user_id,
            VerificationEvent.to_status == "rejected",
        )
        .scalar()
    )
    license_record = db.get(File, profile.license_file_id) if profile.license_file_id else None
    if license_record is None or (
        rejected_at is not None and as_utc(license_record.created_at) <= as_utc(rejected_at)
    ):
        raise HTTPException(
            status_code=400, detail="Upload a new license certificate before resubmitting"
        )

    transition_doctor_status(db, profile, "pending", current_user, request=request)
    safe_auto_check(db, profile)
    db.commit()
    return {"detail": "Resubmitted for verification", "doctor_status": profile.status.value}


@router.get("/doctors")
def list_doctors(
    q: str = "",
    specialization: str = "",
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Directory of verified doctors with public fields only.

    Visible to patients, admins and verified doctors; pending, rejected and suspended
    doctors cannot browse it.
    """
    if current_user.role == UserRole.doctor:
        own = db.get(DoctorProfile, current_user.id)
        if not own or own.status != DoctorStatus.verified:
            raise HTTPException(status_code=403, detail="Your account is not verified yet")

    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    query = (
        db.query(User, DoctorProfile)
        .join(DoctorProfile, User.id == DoctorProfile.user_id)
        .filter(DoctorProfile.status == DoctorStatus.verified, User.is_active.is_(True))
    )
    if q.strip():
        query = query.filter(User.full_name.ilike(f"%{_like_escape(q.strip())}%", escape="\\"))
    if specialization.strip():
        query = query.filter(
            DoctorProfile.specialization.ilike(
                f"%{_like_escape(specialization.strip())}%", escape="\\"
            )
        )

    total = query.count()
    rows = query.order_by(User.full_name).offset(offset).limit(limit).all()
    items = [
        DoctorListItem(
            id=user.id,
            full_name=user.full_name,
            specialization=profile.specialization,
            clinic_name=profile.clinic_name,
            clinic_address=profile.clinic_address,
            profile_photo_url=(
                f"/api/files/{user.profile_photo_file_id}" if user.profile_photo_file_id else None
            ),
            is_verified=True,
        )
        for user, profile in rows
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _patient_item(
    user: User, profile: PatientProfile | None, consent: Consent
) -> DoctorPatientItem:
    return DoctorPatientItem(
        id=user.id,
        full_name=user.full_name,
        email=user.email,
        phone=user.phone,
        dob=profile.dob.isoformat() if profile and profile.dob else None,
        gender=profile.gender if profile else None,
        blood_group=profile.blood_group if profile else None,
        allergies=profile.allergies if profile else None,
        consent_granted_at=consent.granted_at.isoformat() if consent.granted_at else None,
    )


@router.get("/doctor/patients")
def list_consented_patients(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Patients with an active consent to this doctor, and only those."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    query = (
        db.query(User, PatientProfile, Consent)
        .join(Consent, Consent.patient_id == User.id)
        .outerjoin(PatientProfile, PatientProfile.user_id == User.id)
        .filter(Consent.doctor_id == current_user.id, Consent.revoked_at.is_(None))
        .order_by(User.full_name)
    )
    total = query.count()
    items = [_patient_item(u, p, c) for u, p, c in query.offset(offset).limit(limit).all()]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/doctor/patients/{patient_id}")
def get_consented_patient(
    patient_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> DoctorPatientItem:
    """One consented patient. A patient without an active consent looks the same as no patient."""
    row = (
        db.query(User, Consent)
        .join(Consent, Consent.patient_id == User.id)
        .filter(
            User.id == patient_id,
            Consent.doctor_id == current_user.id,
            Consent.revoked_at.is_(None),
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Patient not found")
    user, consent = row
    audit(db, current_user, "patient.view", "user", str(patient_id), request)
    db.commit()
    return _patient_item(user, db.get(PatientProfile, patient_id), consent)


@router.get("/doctor/dashboard")
def doctor_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Today's appointments, consults needing action and headline counts."""
    start = datetime.combine(date.today(), time.min, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    todays = (
        db.query(Appointment, User)
        .join(User, User.id == Appointment.patient_id)
        .filter(
            Appointment.doctor_id == current_user.id,
            Appointment.scheduled_at >= start - timedelta(hours=14),
            Appointment.scheduled_at < end + timedelta(hours=14),
            Appointment.status.in_([AppointmentStatus.requested, AppointmentStatus.confirmed]),
        )
        .order_by(Appointment.scheduled_at)
        .limit(20)
        .all()
    )

    base = db.query(Consult).filter(Consult.doctor_id == current_user.id)
    needs = {
        "transcript_ready": base.filter(
            Consult.status == ConsultStatus.draft,
            Consult.transcription_status == TranscriptionStatus.ready,
        ).count(),
        "soap_ready": base.filter(Consult.status == ConsultStatus.soap_ready).count(),
        "prescription_ready": base.filter(
            Consult.status == ConsultStatus.prescription_ready
        ).count(),
        "failed": base.filter(Consult.status == ConsultStatus.failed).count(),
    }
    patients = (
        db.query(func.count(Consent.id))
        .filter(Consent.doctor_id == current_user.id, Consent.revoked_at.is_(None))
        .scalar()
        or 0
    )
    return {
        "appointments_today": [
            {
                "id": str(a.id),
                "patient_id": str(a.patient_id),
                "patient_name": u.full_name,
                "scheduled_at": a.scheduled_at.isoformat(),
                "status": a.status.value,
                "reason_for_visit": a.reason_for_visit,
            }
            for a, u in todays
        ],
        "needs_action": needs,
        "patients": patients,
        "consults_total": base.count(),
        "completed": base.filter(Consult.status == ConsultStatus.completed).count(),
    }
