"""Security utilities: password hashing (argon2), JWT tokens, CSRF tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import HashingError, VerificationError, VerifyMismatchError

from app.config import get_settings

ph = PasswordHasher()

# A pre-computed dummy hash for constant-time comparison when user not found
_DUMMY_HASH = ph.hash("dummy-password-for-timing-safety")


# ── Password Hashing ──────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Hash a password with argon2."""
    return ph.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a password against its hash. Constant-time on failure."""
    try:
        return ph.verify(hashed, plain)
    except (VerifyMismatchError, VerificationError, HashingError):
        return False


def verify_password_dummy(plain: str) -> bool:
    """Run a dummy verification to prevent timing leaks when user is not found."""
    verify_password(plain, _DUMMY_HASH)
    return False


# ── JWT Tokens ─────────────────────────────────────────────────────────────────


def create_access_token(user_id: UUID, role: str) -> str:
    """Create a signed JWT with sub, role, iat, exp claims."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def decode_access_token(token: str) -> dict | None:
    """Decode and validate a JWT. Returns claims dict or None."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        return payload
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


# ── CSRF Tokens ────────────────────────────────────────────────────────────────


def generate_csrf_token() -> str:
    """Generate a signed CSRF token: random_bytes.signature."""
    settings = get_settings()
    random_part = secrets.token_hex(32)
    sig = hmac.new(settings.SECRET_KEY.encode(), random_part.encode(), hashlib.sha256).hexdigest()
    return f"{random_part}.{sig}"


def validate_csrf_token(token: str) -> bool:
    """Validate CSRF token signature."""
    settings = get_settings()
    if not token or "." not in token:
        return False
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    random_part, sig = parts
    expected = hmac.new(
        settings.SECRET_KEY.encode(), random_part.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(sig, expected)
