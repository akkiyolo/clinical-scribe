"""Prescriptions router: review, edit (new version), approve and reject AI-drafted prescriptions.

Rules enforced here, in the backend:
- only the owning, verified doctor with an active patient consent can edit, approve or reject;
- an approved prescription is immutable: editing creates a new draft version instead;
- every unresolved high-risk safety flag must be acknowledged before approval;
- patients only ever see approved prescriptions, never drafts, flags or source quotes.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, require_verified_doctor
from app.models.consent import Consent
from app.models.consult import Consult, Prescription, SOAPNote
from app.models.enums import ConsultStatus, PrescriptionStatus, UserRole
from app.models.patient import PatientProfile
from app.models.user import User
from app.schemas.prescription import PrescriptionApproval, PrescriptionReject, PrescriptionUpdate
from app.services.access import require_consent
from app.services.audit import audit
from app.services.jobs import run_in_background
from app.services.prescription_agent import run_prescription_agent
from app.services.prescriptions import (
    admin_metadata,
    approval_code_for,
    doctor_full_response,
    patient_view,
    queue_prescription_run,
    render_docx,
    store_docx,
)
from app.services.safety import high_flag_ids, run_safety_checks
from app.services.timeutil import utcnow

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/prescriptions", tags=["prescriptions"])

PS = PrescriptionStatus


def _own_prescription(
    db: Session, prescription_id: UUID, doctor: User, lock: bool = False
) -> Prescription:
    """The doctor's own prescription, with the patient's consent still active."""
    query = db.query(Prescription).filter(Prescription.id == prescription_id)
    p = (query.with_for_update() if lock else query).first()
    if not p or p.doctor_id != doctor.id:
        raise HTTPException(status_code=404, detail="Prescription not found")
    require_consent(db, doctor.id, p.patient_id)
    return p


def _latest_version(db: Session, consult_id: UUID) -> int:
    row = (
        db.query(Prescription.version)
        .filter(Prescription.consult_id == consult_id)
        .order_by(Prescription.version.desc())
        .first()
    )
    return row[0] if row else 0


@router.get("")
def list_prescriptions(
    consult_id: UUID | None = None,
    status: str = "",
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Role-scoped list: patients get approved ones, doctors their own, admins metadata only."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    query = db.query(Prescription)
    if consult_id:
        query = query.filter(Prescription.consult_id == consult_id)

    if current_user.role == UserRole.patient:
        query = query.filter(
            Prescription.patient_id == current_user.id, Prescription.status == PS.approved
        )
    elif current_user.role == UserRole.doctor:
        require_verified_doctor(db=db, current_user=current_user)
        query = query.filter(
            Prescription.doctor_id == current_user.id,
            db.query(Consent.id)
            .filter(
                Consent.doctor_id == Prescription.doctor_id,
                Consent.patient_id == Prescription.patient_id,
                Consent.revoked_at.is_(None),
            )
            .exists(),
        )
    if status:
        try:
            wanted = PS(status)
        except ValueError:
            raise HTTPException(status_code=400, detail="Unknown prescription status")
        if not (current_user.role == UserRole.patient and wanted != PS.approved):
            query = query.filter(Prescription.status == wanted)

    total = query.count()
    rows = query.order_by(Prescription.created_at.desc()).offset(offset).limit(limit).all()
    if current_user.role == UserRole.patient:
        items = [patient_view(p, db) for p in rows]
    elif current_user.role == UserRole.doctor:
        items = [doctor_full_response(p, db) for p in rows]
    else:
        items = [admin_metadata(p, db) for p in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{prescription_id}")
def get_prescription(
    prescription_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """One prescription, shaped for the viewer's role."""
    if current_user.role == UserRole.doctor:
        require_verified_doctor(db=db, current_user=current_user)
        return doctor_full_response(_own_prescription(db, prescription_id, current_user), db)

    p = db.get(Prescription, prescription_id)
    if current_user.role == UserRole.patient:
        if not p or p.patient_id != current_user.id or p.status != PS.approved:
            # Drafts and other patients' prescriptions are indistinguishable from "not found".
            raise HTTPException(status_code=404, detail="Prescription not found")
        return patient_view(p, db)
    if not p:
        raise HTTPException(status_code=404, detail="Prescription not found")
    return admin_metadata(p, db)


@router.put("/{prescription_id}")
def edit_prescription(
    prescription_id: UUID,
    request: Request,
    body: PrescriptionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Save edits as a NEW version (v+1) with a fresh safety check and regenerated document.

    A draft being edited becomes 'superseded'. An approved prescription is never modified: it
    stays approved (and visible to the patient) until the new version is reviewed and approved.
    """
    p = _own_prescription(db, prescription_id, current_user, lock=True)
    if p.status not in (PS.draft, PS.approved):
        raise HTTPException(
            status_code=409, detail="This version was already replaced or rejected; reload the page"
        )
    if body.expected_version is not None and body.expected_version != p.version:
        raise HTTPException(
            status_code=409, detail="This draft changed since you opened it; reload the page"
        )
    if p.version != _latest_version(db, p.consult_id):
        raise HTTPException(status_code=409, detail="A newer version exists; reload the page")

    consult = db.query(Consult).filter(Consult.id == p.consult_id).with_for_update().one()
    soap = db.query(SOAPNote).filter(SOAPNote.consult_id == p.consult_id).first()
    profile = db.get(PatientProfile, p.patient_id)
    content = body.content.model_dump()
    flags = run_safety_checks(
        draft=content,
        transcript=consult.transcript_text,
        soap_plan=soap.plan if soap else None,
        patient_allergies=profile.allergies if profile else None,
    )

    new = Prescription(
        consult_id=p.consult_id,
        doctor_id=p.doctor_id,
        patient_id=p.patient_id,
        version=p.version + 1,
        status=PS.draft,
        content=content,
        safety_flags=flags,
        agent_run_id=None,
    )
    docx = render_docx(db, new, content, flags, is_draft=True)
    new.docx_file_id = store_docx(
        db, p.doctor_id, p.consult_id, new.version, docx, approved=False
    ).id
    if p.status == PS.draft:
        p.status = PS.superseded
    db.add(new)
    consult.status = ConsultStatus.prescription_ready
    db.flush()
    audit(
        db,
        current_user,
        "prescription.edit",
        "prescription",
        str(new.id),
        request,
        {"version": new.version, "from_version": p.version},
    )
    db.commit()
    return doctor_full_response(new, db)


@router.post("/{prescription_id}/approve")
def approve_prescription(
    prescription_id: UUID,
    request: Request,
    body: PrescriptionApproval,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Approve a draft: issues the approval code and the final document, and makes it patient-visible."""
    p = _own_prescription(db, prescription_id, current_user, lock=True)
    if p.status == PS.approved:
        return doctor_full_response(p, db)  # a double click is harmless
    if p.status != PS.draft:
        raise HTTPException(status_code=409, detail="Only the current draft can be approved")

    missing = high_flag_ids(p.safety_flags) - set(body.acknowledged_flag_ids)
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Review and acknowledge all {len(missing)} remaining high-risk flag(s) before approving",
        )

    approved_at = utcnow()
    acknowledged = set(body.acknowledged_flag_ids)
    flags_with_acks = [
        {**f, "acknowledged": f["id"] in acknowledged} if f.get("severity") == "high" else f
        for f in (p.safety_flags or [])
    ]

    for attempt in range(5):
        code = approval_code_for(db, p, attempt)
        try:
            with db.begin_nested():
                docx = render_docx(
                    db,
                    p,
                    p.content or {},
                    [],
                    is_draft=False,
                    approval_code=code,
                    approved_at=approved_at,
                )
                record = store_docx(db, p.doctor_id, p.consult_id, p.version, docx, approved=True)
                # Older approved versions of this consult are now superseded.
                for older in (
                    db.query(Prescription)
                    .filter(
                        Prescription.consult_id == p.consult_id,
                        Prescription.id != p.id,
                        Prescription.status == PS.approved,
                    )
                    .all()
                ):
                    older.status = PS.superseded
                p.status = PS.approved
                p.approved_by = current_user.id
                p.approved_at = approved_at
                p.approval_code = code
                p.docx_file_id = record.id
                p.safety_flags = flags_with_acks
            break
        except IntegrityError:
            if attempt == 4:
                raise HTTPException(
                    status_code=409, detail="Could not issue an approval code; please try again"
                )
    consult = db.get(Consult, p.consult_id)
    if consult:
        consult.status = ConsultStatus.completed
        consult.error_message = None
    audit(
        db,
        current_user,
        "prescription.approve",
        "prescription",
        str(p.id),
        request,
        {
            "approval_code": p.approval_code,
            "version": p.version,
            "acknowledged_high_flags": sorted(acknowledged & high_flag_ids(p.safety_flags)),
        },
    )
    db.commit()
    return doctor_full_response(p, db)


@router.post("/{prescription_id}/reject")
def reject_prescription(
    prescription_id: UUID,
    request: Request,
    body: PrescriptionReject | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Reject a draft, optionally asking the agent to regenerate it (with an extra doctor note)."""
    body = body or PrescriptionReject()
    p = _own_prescription(db, prescription_id, current_user, lock=True)
    if p.status != PS.draft:
        raise HTTPException(status_code=409, detail="Only a draft can be rejected")

    consult = db.query(Consult).filter(Consult.id == p.consult_id).with_for_update().one()
    p.status = PS.rejected
    audit(
        db,
        current_user,
        "prescription.reject",
        "prescription",
        str(p.id),
        request,
        {"regenerate": body.regenerate, "has_note": bool(body.note)},
    )

    run = None
    if body.regenerate:
        run = queue_prescription_run(db, consult, (body.note or "").strip() or None)
    else:
        has_approved = (
            db.query(Prescription.id)
            .filter(Prescription.consult_id == consult.id, Prescription.status == PS.approved)
            .first()
        )
        consult.status = ConsultStatus.completed if has_approved else ConsultStatus.soap_approved
    db.commit()

    if run is not None:
        run_in_background(
            run_prescription_agent, str(consult.id), str(run.id), task_id=f"rx-{run.id}"
        )
    return {
        "detail": "Draft rejected",
        "regenerating": run is not None,
        "rejected_at": utcnow().isoformat(),
    }
