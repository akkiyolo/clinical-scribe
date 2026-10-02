"""Create the first admin account from ADMIN_BOOTSTRAP_* environment settings."""

from __future__ import annotations

from app.config import get_settings
from app.db import SessionLocal
from app.models.enums import UserRole
from app.models.user import User
from app.security import hash_password


def main() -> None:
    settings = get_settings()
    email = settings.ADMIN_BOOTSTRAP_EMAIL.strip().lower()
    password = settings.ADMIN_BOOTSTRAP_PASSWORD
    if (
        len(password) < 12
        or not any(ch.isalpha() for ch in password)
        or not any(ch.isdigit() for ch in password)
    ):
        raise SystemExit(
            "ADMIN_BOOTSTRAP_PASSWORD must be at least 12 characters and contain letters and digits"
        )

    with SessionLocal() as db:
        existing = db.query(User).filter(User.email == email).first()
        if existing:
            if existing.role != UserRole.admin:
                raise SystemExit("ADMIN_BOOTSTRAP_EMAIL is already used by a non-admin account")
            print(f"Admin account already exists: {email}")
            return

        admin = User(
            email=email,
            password_hash=hash_password(password),
            role=UserRole.admin,
            full_name="ClinicalScribe Admin",
        )
        db.add(admin)
        db.commit()
        print(f"Created admin account: {email}")


if __name__ == "__main__":
    main()
