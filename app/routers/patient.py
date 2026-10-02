"""Profile router: view and edit the signed-in user's profile, upload a profile photo."""

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi import File as FastAPIFile
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models.enums import FileCategory, UserRole
from app.models.patient import PatientProfile
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.patient import PatientProfileResponse, PatientProfileUpdate
from app.services.audit import audit
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
