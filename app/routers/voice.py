"""Voice router: text-to-speech read-aloud and the optional patient voice assistant.

VOICE_AGENT_PROVIDER=none turns all of this off: the endpoints answer 404 and the UI hides the
controls.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user, require_role
from app.models.consult import Prescription
from app.models.doctor import DoctorProfile
from app.models.enums import DoctorStatus, FileCategory, PrescriptionStatus, UserRole
from app.models.file import File
from app.models.user import User
from app.rate_limit import limiter
from app.services.access import require_consent
from app.services.audit import audit
from app.services.storage import build_prescription_audio_key, compute_sha256, get_storage_service
from app.services.voice_agent import FeatureDisabled, VoiceError, get_voice_agent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["voice"])

URGENT_NOTE = (
    "If you feel worse or have urgent symptoms, contact your doctor or emergency services."
)


class VoiceSessionRequest(BaseModel):
    prescription_id: UUID


def require_voice_enabled() -> None:
    if get_settings().VOICE_AGENT_PROVIDER == "none":
        raise HTTPException(status_code=404, detail="Voice features are not enabled")


def spoken_summary(content: dict) -> str:
    """Plain-language summary: drug, how much, how often, how long, advice and follow-up."""
    parts = ["Here is a summary of your prescription."]
    diagnosis = content.get("diagnosis") or []
    if diagnosis:
        parts.append(f"Diagnosis: {', '.join(diagnosis)}.")
    medications = content.get("medications") or []
    if medications:
        parts.append("Your medicines are:")
    for number, med in enumerate(medications, 1):
        line = [f"Number {number}: {med.get('drug_name', 'a medicine')}"]
        if med.get("strength"):
            line.append(f"{med['strength']}")
        if med.get("dose"):
            line.append(f"take {med['dose']}")
        if med.get("frequency"):
            line.append(med["frequency"])
        if med.get("duration"):
            line.append(f"for {med['duration']}")
        if med.get("instructions"):
            line.append(f"{med['instructions']}")
        parts.append(", ".join(line) + ".")
    if content.get("advice"):
        parts.append("Advice: " + ". ".join(content["advice"]) + ".")
    if content.get("follow_up"):
        parts.append(f"Follow up: {content['follow_up']}.")
    parts.append(URGENT_NOTE)
    return " ".join(parts)


def _require_verified_doctor(db: Session, user: User) -> None:
    profile = db.get(DoctorProfile, user.id)
    if not profile or profile.status != DoctorStatus.verified:
        raise HTTPException(status_code=403, detail="Your account is not verified yet")


def _readable_prescription(db: Session, prescription_id: UUID, user: User) -> Prescription:
    """An approved prescription the user may have read aloud: the patient's own, or the doctor's."""
    p = db.get(Prescription, prescription_id)
    if not p or p.status != PrescriptionStatus.approved:
        raise HTTPException(status_code=404, detail="Prescription not found")
    if user.role == UserRole.patient and p.patient_id == user.id:
        return p
    if user.role == UserRole.doctor and p.doctor_id == user.id:
        _require_verified_doctor(db, user)
        require_consent(db, user.id, p.patient_id)
        return p
    raise HTTPException(status_code=404, detail="Prescription not found")


@router.post("/prescriptions/{prescription_id}/speak")
@limiter.limit("10/minute")
def speak_prescription(
    request: Request,
    prescription_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Audio summary (mp3) of an approved prescription. Generated once, then served from cache."""
    if current_user.role == UserRole.doctor:
        _require_verified_doctor(db, current_user)
    require_voice_enabled()
    p = _readable_prescription(db, prescription_id, current_user)
    storage = get_storage_service()
    key = build_prescription_audio_key(str(p.patient_id), str(p.id), p.version)

    cached = db.query(File).filter(File.s3_key == key).first()
    if cached:
        audio = storage.download_bytes(key)
        audit(db, current_user, "voice.speak", "prescription", str(p.id), request, {"cached": True})
        db.commit()
        return Response(
            content=audio, media_type="audio/mpeg", headers={"Cache-Control": "private, no-store"}
        )

    try:
        audio = get_voice_agent().synthesize(spoken_summary(p.content or {}))
    except FeatureDisabled:
        raise HTTPException(status_code=404, detail="Voice features are not enabled")
    except VoiceError:
        raise HTTPException(status_code=502, detail="Voice synthesis failed. Please try again.")

    storage.upload_bytes(key, audio, "audio/mpeg")
    db.add(
        File(
            owner_id=p.patient_id,
            category=FileCategory.prescription_audio,
            s3_key=key,
            original_filename=f"prescription-{p.id}-v{p.version}.mp3",
            content_type="audio/mpeg",
            size_bytes=len(audio),
            sha256=compute_sha256(audio),
        )
    )
    audit(db, current_user, "voice.speak", "prescription", str(p.id), request, {"cached": False})
    db.commit()
    return Response(
        content=audio, media_type="audio/mpeg", headers={"Cache-Control": "private, no-store"}
    )


@router.post("/voice/session")
@limiter.limit("10/minute")
def start_voice_session(
    request: Request,
    body: VoiceSessionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["patient"])),
) -> dict:
    """Start an agent session scoped to ONE approved prescription of the signed-in patient."""
    require_voice_enabled()
    p = db.get(Prescription, body.prescription_id)
    if not p or p.patient_id != current_user.id or p.status != PrescriptionStatus.approved:
        raise HTTPException(status_code=404, detail="Prescription not found")

    context = {
        "patient_first_name": current_user.full_name.split()[0],
        "prescription_summary": spoken_summary(p.content or {}),
        "guardrails": (
            "Only explain what is written in this prescription. Do not give new medical advice, "
            "do not change doses, and do not discuss any other patient. For urgent symptoms, tell "
            "the patient to contact their doctor or emergency services."
        ),
    }
    try:
        session = get_voice_agent().start_session(context)
    except FeatureDisabled:
        raise HTTPException(status_code=404, detail="Voice features are not enabled")
    except VoiceError:
        raise HTTPException(
            status_code=502, detail="Could not start the voice assistant. Please try again."
        )

    audit(db, current_user, "voice.session.start", "prescription", str(p.id), request)
    db.commit()
    return session
