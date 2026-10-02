"""FastAPI dependencies: auth, role checks, CSRF validation."""

from __future__ import annotations

import secrets
from typing import Sequence
from uuid import UUID

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User
from app.security import decode_access_token, validate_csrf_token


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    access_token: str | None = Cookie(default=None),
) -> "User":
    """Load the current user from the DB on every request.

    Rejects if token is missing/invalid or user is inactive.
    """
    from app.models.user import User

    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    claims = decode_access_token(access_token)
    if not claims or "sub" not in claims:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    try:
        user_id = UUID(claims["sub"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")

    # Store user on request state for audit logging
    request.state.current_user = user
    return user


def require_role(allowed_roles: Sequence[str]):
    """Dependency factory that checks the user has one of the allowed roles."""

    def dependency(current_user: "User" = Depends(get_current_user)) -> "User":
        if current_user.role.value not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return current_user

    return dependency


def require_verified_doctor(
    db: Session = Depends(get_db),
    current_user: "User" = Depends(get_current_user),
) -> "User":
    """Require that the user is a doctor with verified status. Reads from DB each request."""
    from app.models.doctor import DoctorProfile

    if current_user.role.value != "doctor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only doctors can access this resource",
        )

    profile = db.query(DoctorProfile).filter(DoctorProfile.user_id == current_user.id).first()
    if not profile or profile.status.value != "verified":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is not verified yet",
        )
    return current_user


def csrf_is_valid(cookie_token: str | None, header_token: str | None) -> bool:
    """Double-submit check: header must equal the cookie and carry a valid server signature."""
    if not cookie_token or not header_token:
        return False
    if not secrets.compare_digest(cookie_token, header_token):
        return False
    return validate_csrf_token(cookie_token)


def require_patient_or_verified_doctor(
    db: Session = Depends(get_db),
    current_user: "User" = Depends(get_current_user),
) -> "User":
    """Patients, or doctors who are verified right now (read from the DB on every request)."""
    from app.models.doctor import DoctorProfile

    if current_user.role.value == "patient":
        return current_user
    if current_user.role.value != "doctor":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions"
        )
    profile = db.query(DoctorProfile).filter(DoctorProfile.user_id == current_user.id).first()
    if not profile or profile.status.value != "verified":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Your account is not verified yet"
        )
    return current_user
