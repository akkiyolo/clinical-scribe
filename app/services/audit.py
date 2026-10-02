"""Audit logging service. Append-only: only an insert function is exposed.

Callers must pass ids of rows that are already flushed (resource_id is recorded as given).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.report import AuditLog

logger = logging.getLogger(__name__)


def _get_client_ip(request: Request) -> str:
    """Extract client IP, respecting X-Forwarded-For in production (first hop only)."""
    settings = get_settings()
    if settings.is_production:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "unknown")[:45]


def audit(
    db: Session,
    actor: Any | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    request: Request | None = None,
    metadata: dict | None = None,
) -> None:
    """Write an audit log entry. Never raises — logs errors silently."""
    try:
        actor_id = None
        actor_role = None
        ip = None
        user_agent = None

        if actor is not None:
            actor_id = actor.id if hasattr(actor, "id") else None
            actor_role = actor.role.value if hasattr(actor, "role") else None

        if request is not None:
            ip = _get_client_ip(request)
            user_agent = (request.headers.get("user-agent") or "")[:500]

        entry = AuditLog(
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id else None,
            ip=ip,
            user_agent=user_agent,
            metadata_=metadata,
        )
        # A SAVEPOINT keeps a failed audit insert from poisoning the caller's transaction.
        with db.begin_nested():
            db.add(entry)
    except Exception:
        logger.exception("Failed to write audit log entry for action=%s", action)
