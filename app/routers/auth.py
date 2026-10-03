"""Auth router: register, login, logout, me. Email + password stored in PostgreSQL."""

import logging

from fastapi import APIRouter, Body, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.models.doctor import DoctorProfile
from app.models.enums import UserRole
from app.models.patient import PatientProfile
from app.models.user import User
from app.rate_limit import limiter, login_email_limiter
from app.schemas.auth import AuthResponse, LoginRequest, MeResponse, RegisterRequest
from app.security import (
    create_access_token,
    hash_password,
    verify_password,
    verify_password_dummy,
)
from app.services.audit import audit
from app.services.verification import SYSTEM_ACTOR, safe_auto_check, transition_doctor_status

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

GENERIC_REGISTER_ERROR = "Could not create account"


def set_session_cookie(response: Response, user: User) -> None:
    settings = get_settings()
    response.set_cookie(
        key="access_token",
        value=create_access_token(user.id, user.role.value),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


def _doctor_state(db: Session, user: User) -> dict:
    """Verification fields for auth responses: doctor license state or patient identity state."""
    if user.role == UserRole.patient:
        patient = db.get(PatientProfile, user.id)
        if not patient:
            return {}
        return {
            "patient_status": patient.status.value,
            "rejection_reason": patient.rejection_reason,
        }
    if user.role != UserRole.doctor:
        return {}
    profile = db.get(DoctorProfile, user.id)
    if not profile:
        return {}
    return {
        "doctor_status": profile.status.value,
        "rejection_reason": profile.rejection_reason,
        "suspension_reason": profile.suspension_reason,
    }


@router.post("/register", status_code=status.HTTP_201_CREATED)
@limiter.limit("5/hour")
def register(
    request: Request,
    response: Response,
    body: RegisterRequest = Body(...),
    db: Session = Depends(get_db),
) -> AuthResponse:
    """Create a patient or doctor account and log the new user in."""
    if db.query(User.id).filter(User.email == body.email).first():
        raise HTTPException(status_code=400, detail=GENERIC_REGISTER_ERROR)

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        role=UserRole(body.role),
        full_name=body.full_name,
        phone=body.phone,
    )
    try:
        db.add(user)
        db.flush()

        if body.role == "patient":
            db.add(
                PatientProfile(
                    user_id=user.id,
                    dob=body.dob,
                    gender=body.gender,
                    blood_group=body.blood_group,
                    allergies=body.allergies,
                )
            )
        else:
            profile = DoctorProfile(
                user_id=user.id,
                reg_number=body.reg_number,
                council=body.council,
                reg_year=body.reg_year,
                specialization=body.specialization,
                clinic_name=body.clinic_name,
                clinic_address=body.clinic_address,
                clinic_phone=body.clinic_phone,
            )
            # (new) -> pending, performed by the system actor; writes the 'submitted' event.
            transition_doctor_status(db, profile, "pending", SYSTEM_ACTOR, request=request)
            db.add(profile)
            db.flush()
            safe_auto_check(db, profile)

        audit(db, user, "auth.register", "user", str(user.id), request, {"role": body.role})
        db.commit()
    except IntegrityError:
        # Duplicate email (race) or duplicate council + registration number.
        db.rollback()
        raise HTTPException(status_code=400, detail=GENERIC_REGISTER_ERROR)

    set_session_cookie(response, user)
    return AuthResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role.value,
        **_doctor_state(db, user),
    )


@router.post("/login")
@limiter.limit("5/minute")
def login(
    request: Request,
    response: Response,
    body: LoginRequest = Body(...),
    db: Session = Depends(get_db),
) -> AuthResponse:
    """Verify credentials and set the session cookie."""
    if not login_email_limiter.allow(body.email):
        raise HTTPException(
            status_code=429, detail="Too many attempts. Please wait a minute and try again."
        )

    user = db.query(User).filter(User.email == body.email).first()

    # Always run one password verification so timing does not reveal whether the email exists.
    if user is None:
        verify_password_dummy(body.password)
        password_ok = False
    else:
        password_ok = verify_password(body.password, user.password_hash)

    if not password_ok:
        audit(
            db,
            None,
            "auth.login.failure",
            "user",
            str(user.id) if user else None,
            request,
            {"email": body.email},
        )
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        audit(
            db,
            None,
            "auth.login.failure",
            "user",
            str(user.id),
            request,
            {"email": body.email, "reason": "disabled"},
        )
        db.commit()
        raise HTTPException(status_code=403, detail="Account disabled")

    audit(db, user, "auth.login.success", "user", str(user.id), request)
    db.commit()
    set_session_cookie(response, user)
    return AuthResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role.value,
        **_doctor_state(db, user),
    )


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    access_token: str | None = Cookie(default=None),
) -> dict:
    """Clear the session cookie (idempotent: works even when the session already expired)."""
    user = None
    try:
        user = get_current_user(request, db, access_token)
    except HTTPException:
        pass

    settings = get_settings()
    response.delete_cookie(
        "access_token", path="/", httponly=True, samesite="lax", secure=settings.cookie_secure
    )
    if user:
        audit(db, user, "auth.logout", "user", str(user.id), request)
        db.commit()
    return {"detail": "Logged out"}


@router.get("/me")
def me(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> MeResponse:
    """The signed-in user, their role and (for doctors) verification status."""
    photo_url = (
        f"/api/files/{current_user.profile_photo_file_id}"
        if current_user.profile_photo_file_id
        else None
    )
    return MeResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role.value,
        phone=current_user.phone,
        profile_photo_url=photo_url,
        **_doctor_state(db, current_user),
    )
