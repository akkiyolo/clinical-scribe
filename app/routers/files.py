"""Files router: the only way to read a stored file, always permission-checked and audited."""

from __future__ import annotations

import re
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models.consent import Consent
from app.models.consult import Prescription
from app.models.doctor import DoctorProfile
from app.models.enums import DoctorStatus, FileCategory, PrescriptionStatus, UserRole
from app.models.file import File
from app.models.user import User
from app.services.audit import audit
from app.services.storage import get_storage_service
from app.services.timeutil import utcnow

router = APIRouter(prefix="/api/files", tags=["files"])

DENIED = HTTPException(status_code=403, detail="Access denied")


def _is_verified_doctor(db: Session, user: User) -> bool:
    if user.role != UserRole.doctor:
        return False
    profile = db.get(DoctorProfile, user.id)
    return bool(profile and profile.status == DoctorStatus.verified)


def _prescription_for_file(db: Session, file_id: UUID) -> Prescription | None:
    return db.query(Prescription).filter(Prescription.docx_file_id == file_id).first()


def _authorize(db: Session, user: User, record: File) -> None:
    """Raise 403 unless `user` may read `record`. Rules follow the permission matrix."""
    category, owner = record.category, record.owner_id

    if category == FileCategory.profile_photo:
        if user.id == owner or user.role == UserRole.admin:
            return
        owner_user = db.get(User, owner)
        if owner_user and owner_user.role == UserRole.doctor:
            # Directory avatars: verified doctors' photos are visible to signed-in users.
            profile = db.get(DoctorProfile, owner)
            if profile and profile.status == DoctorStatus.verified:
                return
        elif owner_user and owner_user.role == UserRole.patient and _is_verified_doctor(db, user):
            consent = (
                db.query(Consent.id)
                .filter(
                    Consent.doctor_id == user.id,
                    Consent.patient_id == owner,
                    Consent.revoked_at.is_(None),
                )
                .first()
            )
            if consent:
                return
        raise DENIED

    if category in (FileCategory.license_certificate, FileCategory.patient_id_document):
        if user.id == owner or user.role == UserRole.admin:
            return
        raise DENIED

    if category == FileCategory.consult_audio:
        if user.id == owner and _is_verified_doctor(db, user):
            return
        raise DENIED

    if category in (FileCategory.prescription_docx, FileCategory.prescription_audio):
        if user.id == owner and (user.role == UserRole.patient or _is_verified_doctor(db, user)):
            return  # the doctor who generated it, or the patient who owns the cached audio
        if user.role == UserRole.patient:
            # Patients only ever reach the document of an approved prescription that is theirs.
            prescription = _prescription_for_file(db, record.id)
            if (
                prescription
                and prescription.patient_id == user.id
                and prescription.status == PrescriptionStatus.approved
            ):
                return
        raise DENIED

    raise DENIED


def _download_name(db: Session, record: File) -> str | None:
    if record.category != FileCategory.prescription_docx:
        return None
    prescription = _prescription_for_file(db, record.id)
    patient = db.get(User, prescription.patient_id) if prescription else None
    name = (
        re.sub(r"[^A-Za-z0-9]+", "-", patient.full_name if patient else "Patient").strip("-")
        or "Patient"
    )
    moment = prescription.approved_at if prescription and prescription.approved_at else utcnow()
    return f"Prescription-{name}-{moment.strftime('%Y-%m-%d')}.docx"


@router.get("/{file_id}")
def get_file(
    file_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Authorize, audit, then redirect (302) to a short-lived presigned URL."""
    record = db.get(File, file_id)
    if not record:
        raise HTTPException(status_code=404, detail="File not found")
    _authorize(db, current_user, record)

    download_name = _download_name(db, record)
    audit(
        db,
        current_user,
        "file.access",
        "file",
        str(file_id),
        request,
        {"category": record.category.value},
    )
    if record.category == FileCategory.prescription_docx:
        prescription = _prescription_for_file(db, record.id)
        if prescription:
            audit(
                db,
                current_user,
                "prescription.download",
                "prescription",
                str(prescription.id),
                request,
                {"version": prescription.version, "status": prescription.status.value},
            )
    db.commit()

    storage = get_storage_service()
    if storage.is_local:
        content = storage.download_bytes(record.s3_key)
        filename = download_name or record.original_filename
        disposition = (
            "attachment" if record.category == FileCategory.prescription_docx else "inline"
        )
        headers = {"Cache-Control": "private, no-store"}
        if filename:
            headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(filename)}"
        return Response(
            content=content,
            media_type=record.content_type or "application/octet-stream",
            headers=headers,
        )

    url = storage.presigned_get_url(record.s3_key, download_filename=download_name)
    return RedirectResponse(
        url=url, status_code=302, headers={"Cache-Control": "private, no-store"}
    )
