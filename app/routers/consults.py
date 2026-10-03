"""Consults router: consult CRUD, audio upload, transcript, SOAP generation and approval.

Doctors work on their own consults and only while the patient's consent is active. Patients and
admins can list consults but only ever see metadata (no transcript, SOAP or notes).
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi import File as FastAPIFile
from sqlalchemy.orm import Session, aliased

from app.config import get_settings
from app.db import SessionLocal, get_db
from app.deps import get_current_user, require_verified_doctor
from app.models.appointment import Appointment
from app.models.consent import Consent
from app.models.consult import Consult, Prescription, SOAPNote
from app.models.enums import (
    AppointmentStatus,
    ConsultStatus,
    FileCategory,
    PrescriptionStatus,
    SOAPStatus,
    TranscriptionStatus,
    UserRole,
)
from app.models.file import File
from app.models.patient import PatientProfile
from app.models.user import User
from app.rate_limit import limiter
from app.schemas.consult import (
    ConsultCreate,
    ConsultMeta,
    ConsultResponse,
    LatestPrescription,
    RegenerateRequest,
    SOAPResponse,
    SOAPUpdate,
    TranscriptUpdate,
)
from app.services.access import owned_consult, require_consent
from app.services.audit import audit
from app.services.jobs import expire_stale_consult, run_in_background
from app.services.llm import LLMError
from app.services.prescription_agent import patient_age, run_prescription_agent
from app.services.prescriptions import admin_metadata, doctor_full_response, queue_prescription_run
from app.services.scribe import assign_speaker_roles, generate_soap
from app.services.speech import SpeechError, get_stt_service
from app.services.storage import get_storage_service
from app.services.uploads import read_validated_upload, store_upload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/consults", tags=["consults"])

CS = ConsultStatus
LOCKED_STATUSES = (
    CS.soap_approved,
    CS.prescription_generating,
    CS.prescription_ready,
    CS.completed,
)
SOAP_EDITABLE = (CS.draft, CS.soap_ready, CS.failed)


# ── Access helpers ─────────────────────────────────────────────────────────────────


def latest_prescription(db: Session, consult_id: UUID) -> LatestPrescription | None:
    row = (
        db.query(Prescription)
        .filter(Prescription.consult_id == consult_id)
        .order_by(Prescription.version.desc())
        .first()
    )
    return (
        LatestPrescription(id=row.id, version=row.version, status=row.status.value) if row else None
    )


def llm_mode() -> str:
    settings = get_settings()
    if settings.LLM_PROVIDER == "mock":
        return "mock"
    return "gemini" if settings.llm_configured else "unconfigured"


def doctor_view(db: Session, consult: Consult) -> ConsultResponse:
    patient = db.get(User, consult.patient_id)
    doctor = db.get(User, consult.doctor_id)
    return ConsultResponse(
        id=consult.id,
        appointment_id=consult.appointment_id,
        doctor_id=consult.doctor_id,
        patient_id=consult.patient_id,
        audio_file_id=consult.audio_file_id,
        transcription_status=consult.transcription_status.value,
        transcription_provider=get_settings().STT_PROVIDER,
        llm_provider=llm_mode(),
        transcript_text=consult.transcript_text,
        transcript_edited=bool(consult.transcript_edited),
        status=consult.status.value,
        error_message=consult.error_message,
        created_at=consult.created_at,
        updated_at=consult.updated_at,
        patient_name=patient.full_name if patient else None,
        doctor_name=doctor.full_name if doctor else None,
        latest_prescription=latest_prescription(db, consult.id),
    )


def meta_view(db: Session, consult: Consult) -> ConsultMeta:
    patient = db.get(User, consult.patient_id)
    doctor = db.get(User, consult.doctor_id)
    return ConsultMeta(
        id=consult.id,
        appointment_id=consult.appointment_id,
        doctor_id=consult.doctor_id,
        patient_id=consult.patient_id,
        status=consult.status.value,
        created_at=consult.created_at,
        patient_name=patient.full_name if patient else None,
        doctor_name=doctor.full_name if doctor else None,
    )


def soap_view(soap: SOAPNote) -> dict:
    return SOAPResponse(
        id=soap.id,
        consult_id=soap.consult_id,
        subjective=soap.subjective,
        objective=soap.objective,
        assessment=soap.assessment,
        plan=soap.plan,
        icd10_codes=soap.icd10_codes,
        status=soap.status.value,
        approved_at=soap.approved_at,
    ).model_dump(mode="json")


# ── Create / read ───────────────────────────────────────────────────────────────────


@router.post("", status_code=201)
def create_consult(
    request: Request,
    body: ConsultCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Start a consult for a consenting patient, optionally tied to one of the doctor's appointments."""
    require_consent(db, current_user.id, body.patient_id)

    if body.appointment_id:
        appointment = db.get(Appointment, body.appointment_id)
        if (
            not appointment
            or appointment.doctor_id != current_user.id
            or appointment.patient_id != body.patient_id
        ):
            raise HTTPException(
                status_code=400,
                detail="That appointment does not belong to this doctor and patient",
            )
        if appointment.status == AppointmentStatus.cancelled:
            raise HTTPException(status_code=400, detail="That appointment was cancelled")
        if db.query(Consult.id).filter(Consult.appointment_id == body.appointment_id).first():
            raise HTTPException(
                status_code=409, detail="A consult already exists for this appointment"
            )

    consult = Consult(
        appointment_id=body.appointment_id, doctor_id=current_user.id, patient_id=body.patient_id
    )
    db.add(consult)
    db.flush()
    audit(
        db,
        current_user,
        "consult.create",
        "consult",
        str(consult.id),
        request,
        {"patient_id": str(body.patient_id)},
    )
    db.commit()
    db.refresh(consult)
    return doctor_view(db, consult).model_dump(mode="json")


@router.get("")
def list_consults(
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Role-scoped list. Doctors see their own consults (consented patients only); others see metadata."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    query = db.query(Consult)

    if current_user.role == UserRole.doctor:
        require_verified_doctor(db=db, current_user=current_user)
        active = aliased(Consent)
        query = query.filter(
            Consult.doctor_id == current_user.id,
            db.query(active.id)
            .filter(
                active.doctor_id == Consult.doctor_id,
                active.patient_id == Consult.patient_id,
                active.revoked_at.is_(None),
            )
            .exists(),
        )
    elif current_user.role == UserRole.patient:
        query = query.filter(Consult.patient_id == current_user.id)

    total = query.count()
    rows = query.order_by(Consult.created_at.desc()).offset(offset).limit(limit).all()
    if current_user.role == UserRole.doctor:
        for consult in rows:
            expire_stale_consult(db, consult)
        items = [doctor_view(db, c).model_dump(mode="json") for c in rows]
    else:
        items = [meta_view(db, c).model_dump(mode="json") for c in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{consult_id}")
def get_consult(
    consult_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Consult detail. Full content for the owning doctor; metadata only for patient and admin."""
    if current_user.role == UserRole.doctor:
        require_verified_doctor(db=db, current_user=current_user)
        consult = owned_consult(db, consult_id, current_user)
        expire_stale_consult(db, consult)
        body = doctor_view(db, consult).model_dump(mode="json")
        soap = db.query(SOAPNote).filter(SOAPNote.consult_id == consult_id).first()
        if soap:
            body["soap"] = soap_view(soap)
        return body

    consult = db.get(Consult, consult_id)
    if not consult or (
        current_user.role == UserRole.patient and consult.patient_id != current_user.id
    ):
        raise HTTPException(status_code=404, detail="Consult not found")
    return meta_view(db, consult).model_dump(mode="json")


@router.get("/{consult_id}/prescriptions")
def consult_prescription_versions(
    consult_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Version history of a consult's prescriptions (patients: approved versions only)."""
    if current_user.role == UserRole.doctor:
        require_verified_doctor(db=db, current_user=current_user)
    consult = db.get(Consult, consult_id)
    if not consult:
        raise HTTPException(status_code=404, detail="Consult not found")

    query = db.query(Prescription).filter(Prescription.consult_id == consult_id)
    if current_user.role == UserRole.doctor:
        owned_consult(db, consult_id, current_user)
        return {
            "items": [
                doctor_full_response(p, db) for p in query.order_by(Prescription.version.desc())
            ]
        }
    if current_user.role == UserRole.patient:
        if consult.patient_id != current_user.id:
            raise HTTPException(status_code=404, detail="Consult not found")
        # Versions that were approved at some point; only the latest is still "approved".
        approved = query.filter(Prescription.approval_code.isnot(None)).order_by(
            Prescription.version.desc()
        )
        doctor = db.get(User, consult.doctor_id)
        return {
            "items": [
                {
                    "prescription_id": str(p.id),
                    "version": p.version,
                    "approved_at": p.approved_at.isoformat() if p.approved_at else None,
                    "approval_code": p.approval_code,
                    "status": p.status.value,
                    "doctor_name": doctor.full_name if doctor else None,
                }
                for p in approved
            ]
        }
    return {"items": [admin_metadata(p, db) for p in query.order_by(Prescription.version.desc())]}


# ── Audio and transcript ────────────────────────────────────────────────────────────


def transcribe_job(consult_id: str, file_id: str) -> None:
    """Background job: download the audio, transcribe it, store the transcript."""
    db = SessionLocal()
    consult = None
    try:
        consult = db.get(Consult, UUID(consult_id))
        if not consult:
            return
        consult.transcription_status = TranscriptionStatus.transcribing
        db.commit()

        record = db.get(File, UUID(file_id))
        if not record:
            raise SpeechError("The uploaded audio could not be found. Please upload it again.")
        storage = get_storage_service()
        result = get_stt_service().transcribe(
            storage.download_bytes(record.s3_key), record.original_filename or "audio.webm"
        )

        consult.transcript_text = assign_speaker_roles(result.text)
        consult.transcript_edited = False
        consult.transcription_status = TranscriptionStatus.ready
        consult.error_message = None
        db.commit()

        if get_settings().DELETE_AUDIO_AFTER_TRANSCRIPTION:
            _delete_audio(db, consult, record, storage)
    except SpeechError as exc:
        db.rollback()
        _mark_transcription_failed(db, consult, str(exc))
    except Exception:
        logger.exception("Transcription job failed for consult %s", consult_id)
        db.rollback()
        _mark_transcription_failed(db, consult, "Transcription failed. Please try again.")
    finally:
        db.close()


def _mark_transcription_failed(db: Session, consult: Consult | None, message: str) -> None:
    if consult is None:
        return
    try:
        consult.transcription_status = TranscriptionStatus.failed
        consult.error_message = message[:500]
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not record the transcription failure")


def _delete_audio(db: Session, consult: Consult, record: File, storage) -> None:
    """DELETE_AUDIO_AFTER_TRANSCRIPTION: remove the object and its files row, and audit it."""
    try:
        storage.delete(record.s3_key)
        consult.audio_file_id = None
        db.flush()
        db.delete(record)
        audit(
            db,
            None,
            "consult.audio.delete",
            "consult",
            str(consult.id),
            None,
            {"reason": "DELETE_AUDIO_AFTER_TRANSCRIPTION"},
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("Could not delete the consult audio after transcription")


@router.post("/{consult_id}/audio")
@limiter.limit("20/minute")
def upload_audio(
    consult_id: UUID,
    request: Request,
    file: UploadFile = FastAPIFile(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Upload consultation audio (mp3/wav/m4a/webm/ogg) and start transcription."""
    consult = owned_consult(db, consult_id, current_user, lock=True)
    if consult.status in LOCKED_STATUSES or consult.status == CS.soap_generating:
        raise HTTPException(
            status_code=409, detail="This consult has moved past the transcript step"
        )
    if consult.transcription_status in (
        TranscriptionStatus.uploaded,
        TranscriptionStatus.transcribing,
    ):
        raise HTTPException(status_code=409, detail="Transcription is already in progress")

    upload = read_validated_upload(file, "consult_audio")
    record = store_upload(db, current_user.id, "doctor", FileCategory.consult_audio, upload)
    consult.audio_file_id = record.id
    consult.transcription_status = TranscriptionStatus.uploaded
    consult.status = CS.draft
    consult.error_message = None
    audit(
        db,
        current_user,
        "consult.audio.upload",
        "consult",
        str(consult_id),
        request,
        {"size_bytes": len(upload.data)},
    )
    db.commit()

    run_in_background(
        transcribe_job, str(consult.id), str(record.id), task_id=f"transcribe-{consult_id}"
    )
    return {"detail": "Audio uploaded, transcription started"}


@router.put("/{consult_id}/transcript")
def update_transcript(
    consult_id: UUID,
    request: Request,
    body: TranscriptUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Paste or edit the transcript. Pasting skips audio entirely."""
    consult = owned_consult(db, consult_id, current_user, lock=True)
    if consult.status in LOCKED_STATUSES or consult.status == CS.soap_generating:
        raise HTTPException(
            status_code=409,
            detail="The transcript cannot be changed after the SOAP note is approved",
        )
    if consult.transcription_status in (
        TranscriptionStatus.uploaded,
        TranscriptionStatus.transcribing,
    ):
        raise HTTPException(status_code=409, detail="Wait for the transcription to finish")

    consult.transcript_text = body.transcript_text
    consult.transcript_edited = True
    consult.transcription_status = TranscriptionStatus.ready
    consult.error_message = None
    if consult.status == CS.failed:
        consult.status = CS.draft
    audit(
        db,
        current_user,
        "consult.transcript.update",
        "consult",
        str(consult_id),
        request,
        {"chars": len(body.transcript_text)},
    )
    db.commit()
    return {"detail": "Transcript updated"}


# ── SOAP note ────────────────────────────────────────────────────────────────────────


def generate_soap_job(consult_id: str) -> None:
    """Background job: transcript -> SOAP note + ICD-10 suggestions."""
    db = SessionLocal()
    try:
        consult = db.get(Consult, UUID(consult_id))
        if not consult or consult.status != CS.soap_generating:
            return
        profile = db.get(PatientProfile, consult.patient_id)
        result = generate_soap(
            consult.transcript_text or "",
            {
                "age": patient_age(profile.dob) if profile else None,
                "gender": profile.gender if profile else None,
                "allergies": profile.allergies if profile else None,
            },
        )
        soap = db.query(SOAPNote).filter(SOAPNote.consult_id == consult.id).first()
        if not soap:
            soap = SOAPNote(consult_id=consult.id)
            db.add(soap)
        soap.subjective = result.subjective
        soap.objective = result.objective
        soap.assessment = result.assessment
        soap.plan = result.plan
        soap.icd10_codes = [c.model_dump() for c in result.icd10_codes]
        soap.status = SOAPStatus.draft
        soap.approved_at = None
        consult.status = CS.soap_ready
        consult.error_message = None
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("SOAP generation failed for consult %s: %s", consult_id, type(exc).__name__)
        consult = db.get(Consult, UUID(consult_id))
        if consult and consult.status == CS.soap_generating:
            consult.status = CS.failed
            consult.error_message = (
                str(exc)
                if isinstance(exc, LLMError)
                else "SOAP generation failed. Please try again."
            )
            db.commit()
    finally:
        db.close()


@router.post("/{consult_id}/soap/generate")
def generate_soap_note(
    consult_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Generate (or regenerate) the SOAP draft from the transcript. Repeated clicks are harmless."""
    consult = owned_consult(db, consult_id, current_user, lock=True)
    if consult.status == CS.soap_generating:
        return {"detail": "SOAP generation is already in progress"}
    if consult.status in LOCKED_STATUSES:
        raise HTTPException(status_code=409, detail="The SOAP note is already approved")
    if consult.transcription_status in (
        TranscriptionStatus.uploaded,
        TranscriptionStatus.transcribing,
    ):
        raise HTTPException(status_code=409, detail="Wait for the transcription to finish")
    if not (consult.transcript_text or "").strip():
        raise HTTPException(status_code=400, detail="No transcript available")

    consult.status = CS.soap_generating
    consult.error_message = None
    audit(db, current_user, "consult.soap.generate", "consult", str(consult_id), request)
    db.commit()
    run_in_background(generate_soap_job, str(consult.id), task_id=f"soap-{consult_id}")
    return {"detail": "SOAP generation started"}


@router.put("/{consult_id}/soap")
def update_soap(
    consult_id: UUID,
    request: Request,
    body: SOAPUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Edit the SOAP draft (status stays soap_ready)."""
    consult = owned_consult(db, consult_id, current_user, lock=True)
    if consult.status != CS.soap_ready:
        raise HTTPException(
            status_code=409, detail="The SOAP note can only be edited while it awaits review"
        )
    soap = db.query(SOAPNote).filter(SOAPNote.consult_id == consult_id).first()
    if not soap:
        raise HTTPException(status_code=404, detail="SOAP note not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(soap, field, value)
    audit(db, current_user, "consult.soap.update", "consult", str(consult_id), request)
    db.commit()
    return {"detail": "SOAP note updated"}


@router.post("/{consult_id}/soap/approve")
def approve_soap(
    consult_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Approve the SOAP note, which starts the prescription agent. Idempotent on repeated clicks."""
    consult = owned_consult(db, consult_id, current_user, lock=True)
    if consult.status in LOCKED_STATUSES:
        return {"detail": "SOAP already approved"}
    if consult.status != CS.soap_ready:
        raise HTTPException(status_code=409, detail="There is no SOAP draft awaiting approval")
    soap = db.query(SOAPNote).filter(SOAPNote.consult_id == consult_id).first()
    if not soap:
        raise HTTPException(status_code=404, detail="SOAP note not found")

    soap.status = SOAPStatus.approved
    soap.approved_at = datetime.now(timezone.utc)
    consult.status = CS.soap_approved
    audit(db, current_user, "soap.approve", "consult", str(consult_id), request)
    run = queue_prescription_run(db, consult)
    db.commit()

    if run is not None:
        run_in_background(
            run_prescription_agent, str(consult.id), str(run.id), task_id=f"rx-{run.id}"
        )
    return {"detail": "SOAP approved, prescription drafting started"}


@router.post("/{consult_id}/retry")
def retry_consult(
    consult_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Retry whichever step failed: transcription, SOAP generation or prescription drafting."""
    consult = owned_consult(db, consult_id, current_user, lock=True)

    if consult.transcription_status == TranscriptionStatus.failed and consult.audio_file_id:
        consult.transcription_status = TranscriptionStatus.uploaded
        consult.error_message = None
        audit(
            db,
            current_user,
            "consult.retry",
            "consult",
            str(consult_id),
            request,
            {"step": "transcription"},
        )
        db.commit()
        run_in_background(
            transcribe_job,
            str(consult.id),
            str(consult.audio_file_id),
            task_id=f"transcribe-{consult_id}",
        )
        return {"detail": "Retrying transcription"}

    if consult.status != CS.failed:
        raise HTTPException(status_code=400, detail="Nothing to retry for this consult")

    soap = db.query(SOAPNote).filter(SOAPNote.consult_id == consult_id).first()
    if soap and soap.status == SOAPStatus.approved:
        run = queue_prescription_run(db, consult)
        audit(
            db,
            current_user,
            "consult.retry",
            "consult",
            str(consult_id),
            request,
            {"step": "prescription"},
        )
        db.commit()
        if run is not None:
            run_in_background(
                run_prescription_agent, str(consult.id), str(run.id), task_id=f"rx-{run.id}"
            )
        return {"detail": "Retrying prescription drafting"}

    if (consult.transcript_text or "").strip():
        consult.status = CS.soap_generating
        consult.error_message = None
        audit(
            db, current_user, "consult.retry", "consult", str(consult_id), request, {"step": "soap"}
        )
        db.commit()
        run_in_background(generate_soap_job, str(consult.id), task_id=f"soap-{consult_id}")
        return {"detail": "Retrying SOAP generation"}

    raise HTTPException(status_code=400, detail="Add a transcript or audio first")


@router.post("/{consult_id}/prescriptions/regenerate")
def regenerate_prescription(
    consult_id: UUID,
    request: Request,
    body: RegenerateRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Ask the agent for a fresh draft (optionally with a note) when no draft is awaiting review."""
    note = ((body.note if body else None) or "").strip() or None
    consult = owned_consult(db, consult_id, current_user, lock=True)
    if consult.status == CS.prescription_generating:
        return {"detail": "Prescription drafting is already in progress"}
    soap = db.query(SOAPNote).filter(SOAPNote.consult_id == consult_id).first()
    if not soap or soap.status != SOAPStatus.approved:
        raise HTTPException(status_code=409, detail="Approve the SOAP note first")
    open_draft = (
        db.query(Prescription.id)
        .filter(
            Prescription.consult_id == consult_id, Prescription.status == PrescriptionStatus.draft
        )
        .first()
    )
    if open_draft:
        raise HTTPException(status_code=409, detail="Review or reject the current draft first")

    run = queue_prescription_run(db, consult, note)
    audit(
        db,
        current_user,
        "consult.prescription.regenerate",
        "consult",
        str(consult_id),
        request,
        {"has_note": bool(note)},
    )
    db.commit()
    if run is not None:
        run_in_background(
            run_prescription_agent, str(consult.id), str(run.id), task_id=f"rx-{run.id}"
        )
    return {"detail": "Prescription drafting started"}
