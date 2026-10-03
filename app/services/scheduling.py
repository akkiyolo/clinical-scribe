"""Appointment slots from a doctor's weekly hours, time off and existing bookings.

Weekly hours are wall-clock times in the clinic's time zone (CLINIC_TIMEZONE). Slots are
generated on the fly, never stored: a slot is open when it lies inside a weekly window, the day
is not marked as time off, it starts at least MIN_LEAD from now, and no open (requested or
confirmed) appointment already holds it. Appointments store the slot start in UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.appointment import Appointment
from app.models.availability import DoctorAvailability, DoctorTimeOff
from app.models.enums import AppointmentStatus
from app.services.timeutil import as_utc, utcnow

SLOT_CHOICES = (10, 15, 20, 30, 45, 60)
MAX_DAYS_AHEAD = 60
MIN_LEAD = timedelta(minutes=15)
OPEN_STATUSES = (AppointmentStatus.requested, AppointmentStatus.confirmed)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class Slot:
    start: datetime  # UTC
    minutes: int


@lru_cache
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def clinic_zone() -> ZoneInfo:
    return _zone(get_settings().CLINIC_TIMEZONE)


def clinic_today() -> date:
    return utcnow().astimezone(clinic_zone()).date()


def _normalise(moment: datetime) -> datetime:
    return as_utc(moment).astimezone(timezone.utc).replace(microsecond=0)


def _day_slots(day: date, rules: list[DoctorAvailability], zone: ZoneInfo) -> list[Slot]:
    slots: list[Slot] = []
    for rule in rules:
        if rule.weekday != day.weekday():
            continue
        step = timedelta(minutes=rule.slot_minutes)
        cursor = datetime.combine(day, rule.start_time, zone)
        end = datetime.combine(day, rule.end_time, zone)
        while cursor + step <= end:
            slots.append(Slot(_normalise(cursor), rule.slot_minutes))
            cursor += step
    return sorted(slots, key=lambda s: s.start)


def _booked(db: Session, doctor_id: UUID, start: datetime, end: datetime) -> set[datetime]:
    rows = (
        db.query(Appointment.scheduled_at)
        .filter(
            Appointment.doctor_id == doctor_id,
            Appointment.status.in_(OPEN_STATUSES),
            Appointment.scheduled_at >= start,
            Appointment.scheduled_at < end,
        )
        .all()
    )
    return {_normalise(when) for (when,) in rows}


def _time_off_days(db: Session, doctor_id: UUID, first: date, last: date) -> set[date]:
    days: set[date] = set()
    for off in (
        db.query(DoctorTimeOff)
        .filter(
            DoctorTimeOff.doctor_id == doctor_id,
            DoctorTimeOff.start_date <= last,
            DoctorTimeOff.end_date >= first,
        )
        .all()
    ):
        day = max(off.start_date, first)
        while day <= min(off.end_date, last):
            days.add(day)
            day += timedelta(days=1)
    return days


def open_slots(
    db: Session, doctor_id: UUID, first: date, days: int, now: datetime | None = None
) -> dict[date, list[Slot]]:
    """Open slots per clinic-local day for `days` days starting at `first` (days without any
    open slot are left out)."""
    zone = clinic_zone()
    days = max(1, min(days, MAX_DAYS_AHEAD))
    last = first + timedelta(days=days - 1)
    rules = db.query(DoctorAvailability).filter(DoctorAvailability.doctor_id == doctor_id).all()
    if not rules:
        return {}
    earliest = (now or utcnow()) + MIN_LEAD
    window_start = datetime.combine(first, datetime.min.time(), zone)
    window_end = datetime.combine(last + timedelta(days=1), datetime.min.time(), zone)
    booked = _booked(db, doctor_id, window_start, window_end)
    off = _time_off_days(db, doctor_id, first, last)

    result: dict[date, list[Slot]] = {}
    day = first
    while day <= last:
        if day not in off:
            free = [
                s
                for s in _day_slots(day, rules, zone)
                if s.start >= earliest and s.start not in booked
            ]
            if free:
                result[day] = free
        day += timedelta(days=1)
    return result


class SlotUnavailable(Exception):
    """The requested time is not bookable; the message says why."""


def bookable_slot(db: Session, doctor_id: UUID, when: datetime) -> Slot:
    """The open slot starting exactly at `when`, or SlotUnavailable."""
    when = _normalise(when)
    zone = clinic_zone()
    day = when.astimezone(zone).date()
    rules = db.query(DoctorAvailability).filter(DoctorAvailability.doctor_id == doctor_id).all()
    slot = next((s for s in _day_slots(day, rules, zone) if s.start == when), None)
    if slot is None or day in _time_off_days(db, doctor_id, day, day):
        raise SlotUnavailable("That time is not one of the doctor's open slots. Pick another.")
    if when < utcnow() + MIN_LEAD:
        raise SlotUnavailable("That slot starts too soon to book. Pick a later one.")
    if when in _booked(db, doctor_id, when, when + timedelta(seconds=1)):
        raise SlotUnavailable("That slot has just been booked. Pick another.")
    return slot


def has_open_appointments_between(db: Session, doctor_id: UUID, first: date, last: date) -> int:
    """How many open appointments fall on clinic-local days first..last (for time-off warnings)."""
    zone = clinic_zone()
    start = datetime.combine(first, datetime.min.time(), zone)
    end = datetime.combine(last + timedelta(days=1), datetime.min.time(), zone)
    return len(_booked(db, doctor_id, start, end))
