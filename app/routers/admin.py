"""Admin router: license review, doctor management, reports and the audit log.

Admins see consult and prescription metadata only. The one exception is opening the consult a
report refers to; that access is audit-logged with the report id.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session, aliased

from app.db import get_db
from app.deps import require_role
from app.models.consult import Consult, Prescription, SOAPNote
from app.models.doctor import DoctorProfile
from app.models.enums import DoctorStatus, UserRole
from app.models.registry import VerificationCheck, VerificationEvent
from app.models.report import AuditLog, Report
from app.models.user import User
from app.schemas.admin import (
    AdminActionRequest,
    AdminDoctorDetail,
    AdminDoctorItem,
    AdminStatsResponse,
    ReportResolve,
    ReportResponse,
)
from app.services.audit import audit
from app.services.prescriptions import doctor_full_response
from app.services.verification import InvalidTransition, TransitionError, transition_doctor_status

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])

AdminUser = Depends(require_role(["admin"]))


def _clamp(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, 100)), max(0, offset)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ── Stats ───────────────────────────────────────────────────────────────────────────────


@router.get("/stats")
def get_stats(db: Session = Depends(get_db), current_user: User = AdminUser) -> AdminStatsResponse:
    """Dashboard counts."""

    def doctors(status: DoctorStatus) -> int:
        return (
            db.query(func.count(DoctorProfile.user_id))
            .filter(DoctorProfile.status == status)
            .scalar()
            or 0
        )

    return AdminStatsResponse(
        pending_licenses=doctors(DoctorStatus.pending),
        verified_doctors=doctors(DoctorStatus.verified),
        suspended_doctors=doctors(DoctorStatus.suspended),
        rejected_doctors=doctors(DoctorStatus.rejected),
        open_reports=db.query(func.count(Report.id)).filter(Report.resolved.is_(False)).scalar()
        or 0,
        total_patients=db.query(func.count(User.id)).filter(User.role == UserRole.patient).scalar()
        or 0,
        total_consults=db.query(func.count(Consult.id)).scalar() or 0,
    )


# ── Doctors and license review ────────────────────────────────────────────────────────────


def _doctor_item(user: User, profile: DoctorProfile, check: VerificationCheck | None) -> dict:
    return AdminDoctorItem(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        reg_number=profile.reg_number,
        council=profile.council,
        reg_year=profile.reg_year,
        specialization=profile.specialization,
        status=profile.status.value,
        submitted_at=profile.submitted_at,
        registry_match=check.registry_match if check else None,
        name_match_score=(
            float(check.name_match_score) if check and check.name_match_score is not None else None
        ),
        duplicate_reg_flag=check.duplicate_reg_flag if check else None,
        license_file_id=profile.license_file_id,
    ).model_dump(mode="json")


def _latest_checks(db: Session, doctor_ids: list[UUID]) -> dict[UUID, VerificationCheck]:
    latest: dict[UUID, VerificationCheck] = {}
    if not doctor_ids:
        return latest
    rows = (
        db.query(VerificationCheck)
        .filter(VerificationCheck.doctor_id.in_(doctor_ids))
        .order_by(VerificationCheck.checked_at.asc())
        .all()
    )
    for row in rows:  # ascending, so the last write per doctor is the newest
        latest[row.doctor_id] = row
    return latest


@router.get("/doctors")
def list_doctors(
    status_filter: str = Query("", alias="status"),
    q: str = "",
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = AdminUser,
) -> dict:
    """Doctors with registry-check badges. The pending queue is oldest first."""
    limit, offset = _clamp(limit, offset)
    query = db.query(User, DoctorProfile).join(DoctorProfile, User.id == DoctorProfile.user_id)
    if status_filter:
        try:
            query = query.filter(DoctorProfile.status == DoctorStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=400, detail="Unknown doctor status")
    if q.strip():
        like = f"%{_like_escape(q.strip())}%"
        query = query.filter(
            User.full_name.ilike(like, escape="\\") | User.email.ilike(like, escape="\\")
        )

    if status_filter == "pending":
        query = query.order_by(DoctorProfile.submitted_at.asc())
    else:
        query = query.order_by(DoctorProfile.submitted_at.desc())

    total = query.count()
    rows = query.offset(offset).limit(limit).all()
    checks = _latest_checks(db, [u.id for u, _ in rows])
    return {
        "items": [_doctor_item(u, p, checks.get(u.id)) for u, p in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/doctors/{doctor_id}")
def get_doctor_detail(
    doctor_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = AdminUser,
) -> dict:
    """Submitted details, registry evidence and the full event history for one doctor."""
    user = db.get(User, doctor_id)
    profile = db.get(DoctorProfile, doctor_id)
    if not user or not profile:
        raise HTTPException(status_code=404, detail="Doctor not found")

    events = (
        db.query(VerificationEvent)
        .filter(VerificationEvent.doctor_id == doctor_id)
        .order_by(VerificationEvent.created_at.asc())
        .all()
    )
    checks = (
        db.query(VerificationCheck)
        .filter(VerificationCheck.doctor_id == doctor_id)
        .order_by(VerificationCheck.checked_at.desc())
        .all()
    )
    detail = AdminDoctorDetail(
        **_doctor_item(user, profile, checks[0] if checks else None),
        clinic_name=profile.clinic_name,
        clinic_address=profile.clinic_address,
        clinic_phone=profile.clinic_phone,
        rejection_reason=profile.rejection_reason,
        suspension_reason=profile.suspension_reason,
        verified_at=profile.verified_at,
        verified_by=profile.verified_by,
        events=[
            {
                "action": e.action.value,
                "from_status": e.from_status,
                "to_status": e.to_status,
                "reason": e.reason,
                "created_at": e.created_at.isoformat(),
                "actor_id": str(e.actor_id) if e.actor_id else None,
            }
            for e in events
        ],
        checks=[
            {
                "registry_match": c.registry_match,
                "name_match_score": (
                    float(c.name_match_score) if c.name_match_score is not None else None
                ),
                "duplicate_reg_flag": c.duplicate_reg_flag,
                "checked_at": c.checked_at.isoformat(),
                "notes": (c.raw_result or {}).get("notes"),
                "matched_record": (c.raw_result or {}).get("matched_record"),
            }
            for c in checks
        ],
    ).model_dump(mode="json")
    detail["profile_photo_url"] = (
        f"/api/files/{user.profile_photo_file_id}" if user.profile_photo_file_id else None
    )
    detail["registry_source"] = "mock registry (demo data, not the real national registry)"
    audit(db, current_user, "verification.view", "doctor_profile", str(doctor_id), request)
    db.commit()
    return detail


def _transition(
    db: Session,
    request: Request,
    admin: User,
    doctor_id: UUID,
    to_status: str,
    reason: str | None,
    done: str,
    expected_from: DoctorStatus,
) -> dict:
    """Apply one admin action. `expected_from` keeps e.g. 'reinstate' from approving a pending doctor."""
    profile = (
        db.query(DoctorProfile).filter(DoctorProfile.user_id == doctor_id).with_for_update().first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Doctor not found")
    if profile.status != expected_from:
        raise HTTPException(
            status_code=409,
            detail=f"This action needs a {expected_from.value} doctor, but this one is {profile.status.value}",
        )
    try:
        transition_doctor_status(db, profile, to_status, admin, reason=reason, request=request)
    except InvalidTransition as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc))
    except TransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return {"detail": done, "status": profile.status.value}


@router.post("/doctors/{doctor_id}/approve")
def approve_doctor(
    doctor_id: UUID, request: Request, db: Session = Depends(get_db), current_user: User = AdminUser
) -> dict:
    """Approve a pending doctor (needs an uploaded license certificate)."""
    return _transition(
        db,
        request,
        current_user,
        doctor_id,
        "verified",
        None,
        "Doctor approved",
        DoctorStatus.pending,
    )


@router.post("/doctors/{doctor_id}/reject")
def reject_doctor(
    doctor_id: UUID,
    request: Request,
    body: AdminActionRequest,
    db: Session = Depends(get_db),
    current_user: User = AdminUser,
) -> dict:
    """Reject a pending doctor (reason of at least 10 characters)."""
    return _transition(
        db,
        request,
        current_user,
        doctor_id,
        "rejected",
        body.reason,
        "Doctor rejected",
        DoctorStatus.pending,
    )


@router.post("/doctors/{doctor_id}/suspend")
def suspend_doctor(
    doctor_id: UUID,
    request: Request,
    body: AdminActionRequest,
    db: Session = Depends(get_db),
    current_user: User = AdminUser,
) -> dict:
    """Suspend a verified doctor. Takes effect on the doctor's very next request."""
    return _transition(
        db,
        request,
        current_user,
        doctor_id,
        "suspended",
        body.reason,
        "Doctor suspended",
        DoctorStatus.verified,
    )


@router.post("/doctors/{doctor_id}/reinstate")
def reinstate_doctor(
    doctor_id: UUID,
    request: Request,
    body: AdminActionRequest,
    db: Session = Depends(get_db),
    current_user: User = AdminUser,
) -> dict:
    """Reinstate a suspended doctor."""
    return _transition(
        db,
        request,
        current_user,
        doctor_id,
        "verified",
        body.reason,
        "Doctor reinstated",
        DoctorStatus.suspended,
    )


# ── Reports ──────────────────────────────────────────────────────────────────────────────────


def _report_item(report: Report, reporter_name: str | None, doctor_name: str | None) -> dict:
    return ReportResponse(
        id=report.id,
        reporter_id=report.reporter_id,
        doctor_id=report.doctor_id,
        consult_id=report.consult_id,
        reason=report.reason,
        details=report.details,
        created_at=report.created_at,
        resolved=report.resolved,
        resolved_by=report.resolved_by,
        resolution_note=report.resolution_note,
        reporter_name=reporter_name,
        doctor_name=doctor_name,
    ).model_dump(mode="json")


@router.get("/reports")
def list_reports(
    state: str = Query("all", pattern="^(all|open|resolved)$"),
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = AdminUser,
) -> dict:
    """Reports, newest first, optionally only open or resolved ones."""
    limit, offset = _clamp(limit, offset)
    reporter, doctor = aliased(User), aliased(User)
    query = (
        db.query(Report, reporter.full_name, doctor.full_name)
        .join(reporter, reporter.id == Report.reporter_id)
        .join(doctor, doctor.id == Report.doctor_id)
    )
    if state == "open":
        query = query.filter(Report.resolved.is_(False))
    elif state == "resolved":
        query = query.filter(Report.resolved.is_(True))
    total = query.count()
    rows = query.order_by(Report.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "items": [_report_item(r, a, b) for r, a, b in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _report_or_404(db: Session, report_id: UUID) -> Report:
    report = db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@router.get("/reports/{report_id}")
def get_report(
    report_id: UUID, db: Session = Depends(get_db), current_user: User = AdminUser
) -> dict:
    """One report with consult metadata (no clinical content)."""
    report = _report_or_404(db, report_id)
    reporter, doctor = db.get(User, report.reporter_id), db.get(User, report.doctor_id)
    body = _report_item(
        report, reporter.full_name if reporter else None, doctor.full_name if doctor else None
    )
    consult = db.get(Consult, report.consult_id) if report.consult_id else None
    body["consult"] = (
        {
            "id": str(consult.id),
            "status": consult.status.value,
            "created_at": consult.created_at.isoformat(),
        }
        if consult
        else None
    )
    return body


@router.get("/reports/{report_id}/consult")
def get_report_consult(
    report_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = AdminUser,
) -> dict:
    """Clinical content of the consult a report refers to. Audit-logged with the report id."""
    report = _report_or_404(db, report_id)
    consult = db.get(Consult, report.consult_id) if report.consult_id else None
    if not consult:
        raise HTTPException(status_code=404, detail="This report does not reference a consult")

    soap = db.query(SOAPNote).filter(SOAPNote.consult_id == consult.id).first()
    prescriptions = (
        db.query(Prescription)
        .filter(Prescription.consult_id == consult.id)
        .order_by(Prescription.version.desc())
        .all()
    )
    audit(
        db,
        current_user,
        "report.consult.access",
        "consult",
        str(consult.id),
        request,
        {"report_id": str(report.id)},
    )
    db.commit()
    return {
        "report_id": str(report.id),
        "consult_id": str(consult.id),
        "status": consult.status.value,
        "transcript": consult.transcript_text,
        "soap": (
            {
                "subjective": soap.subjective,
                "objective": soap.objective,
                "assessment": soap.assessment,
                "plan": soap.plan,
                "status": soap.status.value,
            }
            if soap
            else None
        ),
        "prescriptions": [doctor_full_response(p, db) for p in prescriptions],
    }


@router.post("/reports/{report_id}/resolve")
def resolve_report(
    report_id: UUID,
    request: Request,
    body: ReportResolve,
    db: Session = Depends(get_db),
    current_user: User = AdminUser,
) -> dict:
    """Close a report with a resolution note."""
    report = db.query(Report).filter(Report.id == report_id).with_for_update().first()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    if report.resolved:
        raise HTTPException(status_code=409, detail="Report already resolved")
    report.resolved = True
    report.resolved_by = current_user.id
    report.resolution_note = body.resolution_note
    audit(db, current_user, "report.resolve", "report", str(report_id), request)
    db.commit()
    return {"detail": "Report resolved"}


# ── Audit log ─────────────────────────────────────────────────────────────────────────────────


def _day_bound(value: date | None, end: bool) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.max if end else time.min, tzinfo=timezone.utc)


@router.get("/audit-logs")
def list_audit_logs(
    actor: UUID | None = None,
    action: str = "",
    resource_type: str = "",
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = AdminUser,
) -> dict:
    """Filterable, paginated audit trail (newest first). Read-only."""
    limit, offset = _clamp(limit, offset)
    query = db.query(AuditLog)
    if actor:
        query = query.filter(AuditLog.actor_id == actor)
    if action.strip():
        query = query.filter(
            AuditLog.action.ilike(f"%{_like_escape(action.strip())}%", escape="\\")
        )
    if resource_type.strip():
        query = query.filter(AuditLog.resource_type == resource_type.strip())
    if date_from:
        query = query.filter(AuditLog.created_at >= _day_bound(date_from, end=False))
    if date_to:
        query = query.filter(AuditLog.created_at <= _day_bound(date_to, end=True))

    total = query.count()
    rows = query.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "items": [
            {
                "id": str(row.id),
                "actor_id": str(row.actor_id) if row.actor_id else None,
                "actor_role": row.actor_role,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "ip": row.ip,
                "metadata": row.metadata_,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
