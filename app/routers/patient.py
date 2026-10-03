"""Profile router: view and edit the signed-in user's profile, upload a profile photo."""

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi import File as FastAPIFile
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, require_role
from app.models.enums import FileCategory, PatientStatus, UserRole
from app.models.patient import PatientProfile
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.patient import PatientProfileResponse, PatientProfileUpdate
from app.services.audit import audit
from app.services.timeutil import utcnow
from app.services.uploads import read_validated_upload, store_upload

router = APIRouter(prefix="/api", tags=["profile"])


def _photo_url(user: User) -> str | None:
    return f"/api/files/{user.profile_photo_file_id}" if user.profile_photo_file_id else None


@router.get("/me/profile")
def get_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """The signed-in user's profile (patients also get their medical basics)."""
    base = {
        "id": str(current_user.id),
        "email": current_user.email,
        "full_name": current_user.full_name,
        "phone": current_user.phone,
        "role": current_user.role.value,
        "profile_photo_url": _photo_url(current_user),
    }
    if current_user.role == UserRole.patient:
        profile = db.get(PatientProfile, current_user.id)
        base.update(
            PatientProfileResponse(
                id=current_user.id,
                email=current_user.email,
                full_name=current_user.full_name,
                phone=current_user.phone,
                dob=profile.dob if profile else None,
                gender=profile.gender if profile else None,
                blood_group=profile.blood_group if profile else None,
                allergies=profile.allergies if profile else None,
                profile_photo_url=_photo_url(current_user),
            ).model_dump(mode="json")
        )
    return base


@router.patch("/me/profile")
def update_profile(
    request: Request,
    body: PatientProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Update name and phone; patients may also update date of birth, gender, blood group, allergies."""
    data = body.model_dump(exclude_unset=True)
    if data.get("full_name"):
        current_user.full_name = data["full_name"]
    if "phone" in data:
        current_user.phone = data["phone"]

    if current_user.role == UserRole.patient:
        profile = db.get(PatientProfile, current_user.id)
        if profile:
            for field in ("dob", "gender", "blood_group", "allergies"):
                if field in data:
                    setattr(profile, field, data[field])

    audit(
        db,
        current_user,
        "profile.update",
        "user",
        str(current_user.id),
        request,
        {"fields": sorted(data)},
    )
    db.commit()
    return {"detail": "Profile updated"}


@router.post("/me/photo")
@limiter.limit("20/minute")
def upload_photo(
    request: Request,
    file: UploadFile = FastAPIFile(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Upload a profile photo (jpg/png/webp, max 5 MB)."""
    upload = read_validated_upload(file, "profile_photo")
    record = store_upload(
        db, current_user.id, current_user.role.value, FileCategory.profile_photo, upload
    )
    current_user.profile_photo_file_id = record.id
    audit(db, current_user, "profile.photo.upload", "file", str(record.id), request)
    db.commit()
    return {"detail": "Photo uploaded", "file_id": str(record.id)}


# ── Identity verification ─────────────────────────────────────────────────────────────


def _patient_profile(db: Session, user: User) -> PatientProfile:
    profile = db.get(PatientProfile, user.id)
    if not profile:
        raise HTTPException(status_code=404, detail="Patient profile not found")
    return profile


def patient_verification_state(profile: PatientProfile) -> dict:
    return {
        "status": profile.status.value,
        "id_document_file_id": (
            str(profile.id_document_file_id) if profile.id_document_file_id else None
        ),
        "submitted_at": profile.submitted_at.isoformat() if profile.submitted_at else None,
        "verified_at": profile.verified_at.isoformat() if profile.verified_at else None,
        "rejection_reason": profile.rejection_reason,
    }


@router.get("/patient/verification")
def get_patient_verification(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["patient"])),
) -> dict:
    """The patient's own identity-verification status."""
    return patient_verification_state(_patient_profile(db, current_user))


@router.post("/patient/id-document")
@limiter.limit("20/minute")
def upload_id_document(
    request: Request,
    file: UploadFile = FastAPIFile(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["patient"])),
) -> dict:
    """Upload a government ID (pdf/jpg/png, max 10 MB) for an administrator to check.

    After a rejection, a new upload resubmits the patient for review. Verified patients cannot
    replace their document (that would silently re-open a closed review).
    """
    profile = _patient_profile(db, current_user)
    if profile.status == PatientStatus.verified:
        raise HTTPException(status_code=409, detail="Your identity is already verified")
    upload = read_validated_upload(file, "patient_id_document")
    record = store_upload(db, current_user.id, "patient", FileCategory.patient_id_document, upload)
    resubmitted = profile.status == PatientStatus.rejected
    profile.id_document_file_id = record.id
    profile.submitted_at = utcnow()
    profile.status = PatientStatus.pending
    profile.rejection_reason = None
    audit(
        db,
        current_user,
        "patient.id_document.upload",
        "patient_profile",
        str(current_user.id),
        request,
        {"file_id": str(record.id), "resubmitted": resubmitted},
    )
    db.commit()
    return {
        "detail": "ID uploaded. An administrator will review it.",
        **patient_verification_state(profile),
    }
