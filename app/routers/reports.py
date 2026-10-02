"""Reports router: patients report a doctor, optionally pointing at one of their own consults."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_role
from app.models.consult import Consult
from app.models.doctor import DoctorProfile
from app.models.report import Report
from app.models.user import User
from app.schemas.admin import ReportCreate
from app.services.audit import audit

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.post("", status_code=201)
def create_report(
    request: Request,
    body: ReportCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["patient"])),
) -> dict:
    """File a report about a doctor. A referenced consult must be the reporter's own with that doctor."""
    if not db.get(DoctorProfile, body.doctor_id):
        raise HTTPException(status_code=404, detail="Doctor not found")

    if body.consult_id:
        consult = db.get(Consult, body.consult_id)
        if (
            not consult
            or consult.patient_id != current_user.id
            or consult.doctor_id != body.doctor_id
        ):
            raise HTTPException(
                status_code=400, detail="That consult is not one of yours with this doctor"
            )

    report = Report(
        reporter_id=current_user.id,
        doctor_id=body.doctor_id,
        consult_id=body.consult_id,
        reason=body.reason,
        details=body.details,
    )
    db.add(report)
    db.flush()
    audit(
        db,
        current_user,
        "report.create",
        "report",
        str(report.id),
        request,
        {
            "doctor_id": str(body.doctor_id),
            "consult_id": str(body.consult_id) if body.consult_id else None,
        },
    )
    db.commit()
    return {"detail": "Report submitted", "id": str(report.id)}
