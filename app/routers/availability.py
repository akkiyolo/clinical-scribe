"""Doctor availability: weekly hours, time off, and the open slots patients book from."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user, require_verified_doctor
from app.models.availability import DoctorAvailability, DoctorTimeOff
from app.models.doctor import DoctorProfile
from app.models.enums import DoctorStatus, UserRole
from app.models.user import User
from app.schemas.availability import AvailabilityUpdate, TimeOffCreate
from app.services.audit import audit
from app.services.scheduling import (
    MAX_DAYS_AHEAD,
    SLOT_CHOICES,
    WEEKDAYS,
    clinic_today,
    clinic_zone,
    has_open_appointments_between,
    open_slots,
)

router = APIRouter(prefix="/api", tags=["availability"])


def _hhmm(value) -> str:
    return value.strftime("%H:%M")


def _rule(rule: DoctorAvailability) -> dict:
    return {
        "id": str(rule.id),
        "weekday": rule.weekday,
        "weekday_name": WEEKDAYS[rule.weekday],
        "start_time": _hhmm(rule.start_time),
        "end_time": _hhmm(rule.end_time),
        "slot_minutes": rule.slot_minutes,
    }


def _time_off(off: DoctorTimeOff) -> dict:
    return {
        "id": str(off.id),
        "start_date": off.start_date.isoformat(),
        "end_date": off.end_date.isoformat(),
        "reason": off.reason,
    }


def _availability(db: Session, doctor_id: UUID) -> dict:
    rules = (
        db.query(DoctorAvailability)
        .filter(DoctorAvailability.doctor_id == doctor_id)
        .order_by(DoctorAvailability.weekday, DoctorAvailability.start_time)
        .all()
    )
    time_off = (
        db.query(DoctorTimeOff)
        .filter(DoctorTimeOff.doctor_id == doctor_id, DoctorTimeOff.end_date >= clinic_today())
        .order_by(DoctorTimeOff.start_date)
        .all()
    )
    return {
        "timezone": get_settings().CLINIC_TIMEZONE,
        "slot_choices": list(SLOT_CHOICES),
        "rules": [_rule(r) for r in rules],
        "time_off": [_time_off(t) for t in time_off],
    }


@router.get("/doctor/availability")
def get_availability(
    db: Session = Depends(get_db), current_user: User = Depends(require_verified_doctor)
) -> dict:
    """The doctor's weekly hours and upcoming time off."""
    return _availability(db, current_user.id)


@router.put("/doctor/availability")
def replace_availability(
    request: Request,
    body: AvailabilityUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Replace the weekly hours. Existing appointments are kept even if they fall outside."""
    db.query(DoctorAvailability).filter(DoctorAvailability.doctor_id == current_user.id).delete()
    for rule in body.rules:
        db.add(
            DoctorAvailability(
                doctor_id=current_user.id,
                weekday=rule.weekday,
                start_time=rule.start_time,
                end_time=rule.end_time,
                slot_minutes=rule.slot_minutes,
            )
        )
    audit(
        db,
        current_user,
        "availability.update",
        "doctor_profile",
        str(current_user.id),
        request,
        {"windows": len(body.rules)},
    )
    db.commit()
    return _availability(db, current_user.id)


@router.post("/doctor/time-off", status_code=201)
def add_time_off(
    request: Request,
    body: TimeOffCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    """Block whole days. Appointments already booked on those days are not cancelled."""
    if body.end_date < clinic_today():
        raise HTTPException(status_code=400, detail="end_date: time off must not be in the past")
    off = DoctorTimeOff(
        doctor_id=current_user.id,
        start_date=body.start_date,
        end_date=body.end_date,
        reason=body.reason,
    )
    db.add(off)
    db.flush()
    conflicts = has_open_appointments_between(db, current_user.id, body.start_date, body.end_date)
    audit(
        db,
        current_user,
        "availability.time_off.add",
        "doctor_time_off",
        str(off.id),
        request,
        {"start_date": body.start_date.isoformat(), "end_date": body.end_date.isoformat()},
    )
    db.commit()
    return {**_time_off(off), "conflicting_appointments": conflicts}


@router.delete("/doctor/time-off/{time_off_id}")
def remove_time_off(
    time_off_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_verified_doctor),
) -> dict:
    off = db.get(DoctorTimeOff, time_off_id)
    if not off or off.doctor_id != current_user.id:
        raise HTTPException(status_code=404, detail="Time off not found")
    db.delete(off)
    audit(
        db,
        current_user,
        "availability.time_off.remove",
        "doctor_time_off",
        str(time_off_id),
        request,
    )
    db.commit()
    return {"detail": "Time off removed"}


@router.get("/doctors/{doctor_id}/slots")
def list_open_slots(
    doctor_id: UUID,
    days: int = 14,
    start: date | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Open slots for a verified doctor, grouped by clinic-local day.

    Patients, admins and verified doctors may look; only verified patients can book.
    """
    if current_user.role == UserRole.doctor:
        own = db.get(DoctorProfile, current_user.id)
        if not own or own.status != DoctorStatus.verified:
            raise HTTPException(status_code=403, detail="Your account is not verified yet")
    profile = db.get(DoctorProfile, doctor_id)
    doctor = db.get(User, doctor_id)
    if not profile or not doctor or profile.status != DoctorStatus.verified or not doctor.is_active:
        raise HTTPException(status_code=404, detail="Doctor not found")

    today = clinic_today()
    first = max(start or today, today)
    if first > today + timedelta(days=MAX_DAYS_AHEAD):
        raise HTTPException(status_code=400, detail="start: too far ahead")
    zone = clinic_zone()
    by_day = open_slots(db, doctor_id, first, days)
    has_schedule = (
        db.query(DoctorAvailability.id).filter(DoctorAvailability.doctor_id == doctor_id).first()
        is not None
    )
    return {
        "doctor_id": str(doctor_id),
        "timezone": get_settings().CLINIC_TIMEZONE,
        "has_schedule": has_schedule,
        "days": [
            {
                "date": day.isoformat(),
                "weekday": WEEKDAYS[day.weekday()],
                "slots": [
                    {
                        "start": slot.start.isoformat(),
                        "time": _hhmm(slot.start.astimezone(zone)),
                        "minutes": slot.minutes,
                    }
                    for slot in slots
                ],
            }
            for day, slots in by_day.items()
        ],
    }
