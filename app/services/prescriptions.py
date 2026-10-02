"""Prescription helpers: response shapes, document (re)generation and approval codes."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.consult import AgentRun, Consult, Prescription
from app.models.doctor import DoctorProfile
from app.models.enums import AgentRunStatus, ConsultStatus, FileCategory
from app.models.file import File
from app.models.patient import PatientProfile
from app.models.user import User
from app.schemas.prescription import (
    PrescriptionContent,
    PrescriptionPatientView,
    PrescriptionResponse,
    SafetyFlag,
)
from app.services.docx_builder import DOCX_MIME, build_prescription_docx
from app.services.prescription_agent import doctor_header, download_doctor_photo, patient_header
from app.services.storage import build_prescription_key, compute_sha256, get_storage_service
from app.services.timeutil import utcnow


def doctor_full_response(p: Prescription, db: Session) -> dict:
    """Everything the owning doctor sees: content, safety flags and source quotes."""
    doctor, patient = db.get(User, p.doctor_id), db.get(User, p.patient_id)
    return PrescriptionResponse(
        id=p.id,
        consult_id=p.consult_id,
        doctor_id=p.doctor_id,
        patient_id=p.patient_id,
        version=p.version,
        status=p.status.value,
        content=PrescriptionContent(**p.content) if p.content else None,
        safety_flags=[SafetyFlag(**f) for f in (p.safety_flags or [])],
        docx_file_id=p.docx_file_id,
        agent_run_id=p.agent_run_id,
        approved_by=p.approved_by,
        approved_at=p.approved_at,
        approval_code=p.approval_code,
        created_at=p.created_at,
        doctor_name=doctor.full_name if doctor else None,
        patient_name=patient.full_name if patient else None,
    ).model_dump(mode="json")


def admin_metadata(p: Prescription, db: Session) -> dict:
    """Admins see ids, dates, statuses and names but never the clinical content."""
    doctor, patient = db.get(User, p.doctor_id), db.get(User, p.patient_id)
    return {
        "id": str(p.id),
        "consult_id": str(p.consult_id),
        "version": p.version,
        "status": p.status.value,
        "created_at": p.created_at.isoformat(),
        "approved_at": p.approved_at.isoformat() if p.approved_at else None,
        "approval_code": p.approval_code,
        "doctor_name": doctor.full_name if doctor else None,
        "patient_name": patient.full_name if patient else None,
    }


def patient_view(p: Prescription, db: Session) -> dict:
    """What a patient sees: no drafts, flags or source quotes."""
    doctor = db.get(User, p.doctor_id)
    content = p.content or {}
    medications = [
        {k: v for k, v in m.items() if k != "source_quote"} for m in content.get("medications", [])
    ]
    return PrescriptionPatientView(
        id=p.id,
        consult_id=p.consult_id,
        doctor_id=p.doctor_id,
        doctor_name=doctor.full_name if doctor else None,
        version=p.version,
        status=p.status.value,
        diagnosis=content.get("diagnosis", []),
        medications=medications,
        tests_advised=content.get("tests_advised", []),
        advice=content.get("advice", []),
        follow_up=content.get("follow_up"),
        notes=content.get("notes"),
        approved_at=p.approved_at,
        approval_code=p.approval_code,
        docx_file_id=p.docx_file_id,
    ).model_dump(mode="json")


def render_docx(
    db: Session,
    p: Prescription,
    content: dict,
    flags: list[dict],
    is_draft: bool,
    approval_code: str | None = None,
    approved_at: datetime | None = None,
) -> bytes:
    """Render a prescription document with the doctor's current details at the top."""
    doctor_user = db.get(User, p.doctor_id)
    profile = db.get(DoctorProfile, p.doctor_id)
    patient_user = db.get(User, p.patient_id)
    header = doctor_header(doctor_user, profile)
    photo = download_doctor_photo(db, header.get("profile_photo_file_id"), get_storage_service())
    return build_prescription_docx(
        content=content,
        flags=flags,
        doctor=header,
        patient=patient_header(patient_user, db.get(PatientProfile, p.patient_id)),
        is_draft=is_draft,
        approval_code=approval_code,
        approved_at=approved_at,
        consult_id=str(p.consult_id),
        prescription_id=str(p.id) if not is_draft else None,
        doctor_photo_bytes=photo,
    )


def store_docx(
    db: Session,
    doctor_id: uuid.UUID,
    consult_id: uuid.UUID,
    version: int,
    data: bytes,
    approved: bool,
) -> File:
    """Upload a rendered document under a fresh key and add its files row."""
    key = build_prescription_key(str(doctor_id), str(consult_id), version, approved=approved)
    get_storage_service().upload_bytes(key, data, DOCX_MIME)
    record = File(
        owner_id=doctor_id,
        category=FileCategory.prescription_docx,
        s3_key=key,
        original_filename=f"prescription-v{version}{'' if approved else '-draft'}.docx",
        content_type=DOCX_MIME,
        size_bytes=len(data),
        sha256=compute_sha256(data),
    )
    db.add(record)
    db.flush()
    return record


def approval_code_for(db: Session, p: Prescription, attempt: int = 0) -> str:
    """RX-<year>-<6-digit number>-v<version>. The number is shared by all versions of a consult."""
    earlier = (
        db.query(Prescription.approval_code)
        .filter(Prescription.consult_id == p.consult_id, Prescription.approval_code.isnot(None))
        .first()
    )
    if earlier:
        base = earlier[0].split("-")[2]
    else:
        issued = (
            db.query(func.count(func.distinct(Prescription.consult_id)))
            .filter(Prescription.approval_code.isnot(None))
            .scalar()
            or 0
        )
        base = f"{issued + 1 + attempt:06d}"
    return f"RX-{utcnow().year}-{base}-v{p.version}"


def queue_prescription_run(
    db: Session, consult: Consult, doctor_note: str | None = None
) -> AgentRun | None:
    """Create the agent run for a consult unless one is already active (idempotent).

    Must be called with the consult row locked. Returns the new run, or None if one is active.
    """
    active = (
        db.query(AgentRun)
        .filter(
            AgentRun.consult_id == consult.id,
            AgentRun.status.in_([AgentRunStatus.queued, AgentRunStatus.running]),
        )
        .first()
    )
    if active:
        return None
    run = AgentRun(
        consult_id=consult.id,
        status=AgentRunStatus.queued,
        graph_state={"doctor_note": doctor_note} if doctor_note else None,
    )
    db.add(run)
    db.flush()
    consult.status = ConsultStatus.prescription_generating
    consult.error_message = None
    return run
