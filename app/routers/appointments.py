"""Appointments router: patients book and cancel, verified doctors confirm and complete."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session, aliased

from app.db import get_db
from app.deps import get_current_user, require_patient_or_verified_doctor, require_role
from app.models.appointment import Appointment
from app.models.doctor import DoctorProfile
from app.models.enums import AppointmentStatus, DoctorStatus, UserRole
from app.models.user import User
from app.schemas.appointment import AppointmentCreate, AppointmentResponse, AppointmentUpdate
from app.services.audit import audit

router = APIRouter(prefix="/api/appointments", tags=["appointments"])

S = AppointmentStatus
# who may move an appointment from one status to another
PATIENT_TRANSITIONS = {(S.requested, S.cancelled), (S.confirmed, S.cancelled)}
DOCTOR_TRANSITIONS = {(S.requested, S.confirmed), (S.confirmed, S.completed)}


def _response(
    appt: Appointment, patient_name: str | None, doctor_name: str | None
) -> AppointmentResponse:
    return AppointmentResponse(
        id=appt.id,
        patient_id=appt.patient_id,
        doctor_id=appt.doctor_id,
        scheduled_at=appt.scheduled_at,
        status=appt.status.value,
        reason_for_visit=appt.reason_for_visit,
        created_at=appt.created_at,
        patient_name=patient_name,
        doctor_name=doctor_name,
    )


def _require_verified(db: Session, user: User) -> None:
    profile = db.get(DoctorProfile, user.id)
    if not profile or profile.status != DoctorStatus.verified:
        raise HTTPException(status_code=403, detail="Your account is not verified yet")


@router.post("", status_code=201)
def create_appointment(
    request: Request,
    body: AppointmentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["patient"])),
) -> AppointmentResponse:
    """Book an appointment (patients only, with verified doctors only)."""
    profile = db.get(DoctorProfile, body.doctor_id)
    doctor = db.get(User, body.doctor_id)
    if not profile or not doctor or profile.status != DoctorStatus.verified or not doctor.is_active:
        raise HTTPException(status_code=400, detail="Doctor is not verified or does not exist")

    appt = Appointment(
        patient_id=current_user.id,
        doctor_id=body.doctor_id,
        scheduled_at=body.scheduled_at,
        reason_for_visit=body.reason_for_visit,
        status=AppointmentStatus.requested,
    )
    db.add(appt)
    db.flush()
    audit(
        db,
        current_user,
        "appointment.create",
        "appointment",
        str(appt.id),
        request,
        {"doctor_id": str(body.doctor_id)},
    )
    db.commit()
    db.refresh(appt)
    return _response(appt, current_user.full_name, doctor.full_name)


@router.get("")
def list_appointments(
    status: str = "",
    limit: int = 20,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Role-scoped list: a patient's own, a verified doctor's own, or (admin) all metadata."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    patient_user, doctor_user = aliased(User), aliased(User)
    query = (
        db.query(Appointment, patient_user.full_name, doctor_user.full_name)
        .join(patient_user, patient_user.id == Appointment.patient_id)
        .join(doctor_user, doctor_user.id == Appointment.doctor_id)
    )
    if current_user.role == UserRole.patient:
        query = query.filter(Appointment.patient_id == current_user.id)
    elif current_user.role == UserRole.doctor:
        _require_verified(db, current_user)
        query = query.filter(Appointment.doctor_id == current_user.id)
    if status:
        try:
            query = query.filter(Appointment.status == AppointmentStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail="Unknown appointment status")

    total = query.count()
    rows = query.order_by(Appointment.scheduled_at.desc()).offset(offset).limit(limit).all()
    return {
        "items": [_response(a, p, d) for a, p, d in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.patch("/{appointment_id}")
def update_appointment(
    appointment_id: UUID,
    request: Request,
    body: AppointmentUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_patient_or_verified_doctor),
) -> dict:
    """Patient: cancel own. Verified doctor: confirm or complete own. Invalid moves return 409."""
    appt = db.query(Appointment).filter(Appointment.id == appointment_id).with_for_update().first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    target = AppointmentStatus(body.status)
    if current_user.role == UserRole.patient:
        if appt.patient_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not your appointment")
        allowed = PATIENT_TRANSITIONS
        if target != S.cancelled:
            raise HTTPException(status_code=400, detail="Patients can only cancel appointments")
    else:
        if appt.doctor_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not your appointment")
        allowed = DOCTOR_TRANSITIONS
        if target not in (S.confirmed, S.completed):
            raise HTTPException(
                status_code=400, detail="Doctors can only confirm or complete appointments"
            )

    if (appt.status, target) not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"A {appt.status.value} appointment cannot be marked {target.value}",
        )

    appt.status = target
    audit(db, current_user, f"appointment.{target.value}", "appointment", str(appt.id), request)
    db.commit()
    return {"detail": f"Appointment {target.value}", "status": target.value}
