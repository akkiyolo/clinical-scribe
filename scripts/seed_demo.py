"""Create SYNTHETIC demo data for local development. Refuses to run when ENV=production.

Run (after `alembic upgrade head`): python -m scripts.seed_demo

Creates a verified demo patient, a pending patient for the admin's patient queue, a verified
demo doctor (with a matching registry record and weekly hours Mon-Sat 10:00-13:00 and
17:00-19:00 in 30-minute slots), a pending doctor for the license queue, one confirmed
appointment, an active consent, and a consult with a sample synthetic transcript ready for
SOAP generation. The admin account is created by
`python -m scripts.create_admin`. All names, numbers and passwords below are fake.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from app.config import get_settings
from app.db import SessionLocal
from app.models.appointment import Appointment
from app.models.availability import DoctorAvailability
from app.models.consent import Consent
from app.models.consult import Consult
from app.models.doctor import DoctorProfile
from app.models.enums import (
    AppointmentStatus,
    DoctorStatus,
    PatientStatus,
    TranscriptionStatus,
    UserRole,
)
from app.models.patient import PatientProfile
from app.models.user import User
from app.security import hash_password
from app.services.speech import MockSTT
from app.services.verification import SYSTEM_ACTOR, run_auto_check, transition_doctor_status
from scripts.seed_registry import seed_registry

DEMO_PASSWORD = "DemoPass123"  # synthetic accounts only
DOCTOR_EMAIL = "dr.demo@example.com"
PENDING_DOCTOR_EMAIL = "dr.pending@example.com"
PATIENT_EMAIL = "patient.demo@example.com"
PENDING_PATIENT_EMAIL = "patient.pending@example.com"
DEMO_HOURS = ((time(10, 0), time(13, 0)), (time(17, 0), time(19, 0)))  # Mon-Sat


def _doctor(db, email, name, reg_number, council, year, specialization, verified):
    user = User(
        email=email,
        password_hash=hash_password(DEMO_PASSWORD),
        role=UserRole.doctor,
        full_name=name,
        phone="+91-9000000001",
    )
    db.add(user)
    db.flush()
    profile = DoctorProfile(
        user_id=user.id,
        reg_number=reg_number,
        council=council,
        reg_year=year,
        specialization=specialization,
        clinic_name=f"{name.replace('Dr. ', '')} Clinic",
        clinic_address="12 Demo Street, Mumbai, Maharashtra",
        clinic_phone="+91-22-00000000",
    )
    transition_doctor_status(db, profile, "pending", SYSTEM_ACTOR)
    db.add(profile)
    db.flush()
    run_auto_check(db, profile)
    if verified:
        profile.status = DoctorStatus.verified  # demo seed only; real doctors need an admin
        profile.verified_at = datetime.now(timezone.utc)
    return user


def seed_demo() -> None:
    if get_settings().is_production:
        raise SystemExit("seed_demo refuses to run when ENV=production")

    seed_registry()
    with SessionLocal() as db:
        if db.query(User).filter(User.email == DOCTOR_EMAIL).first():
            print("Demo data already exists; nothing to do.")
            return

        doctor = _doctor(
            db,
            DOCTOR_EMAIL,
            "Dr. Priya Sharma",
            "MH-2015-12345",
            "Maharashtra Medical Council",
            2015,
            "General Medicine",
            verified=True,
        )
        _doctor(
            db,
            PENDING_DOCTOR_EMAIL,
            "Dr. Sneha Kumar",
            "KA-2020-11111",
            "Karnataka Medical Council",
            2020,
            "Dermatology",
            verified=False,
        )

        patient = User(
            email=PATIENT_EMAIL,
            password_hash=hash_password(DEMO_PASSWORD),
            role=UserRole.patient,
            full_name="Rahul Verma",
            phone="+91-9000000002",
        )
        db.add(patient)
        db.flush()
        db.add(
            PatientProfile(
                user_id=patient.id,
                dob=date(1990, 5, 15),
                gender="male",
                blood_group="B+",
                allergies="Penicillin",
                status=PatientStatus.verified,  # demo seed only; real patients need an admin
                verified_at=datetime.now(timezone.utc),
            )
        )
        pending_patient = User(
            email=PENDING_PATIENT_EMAIL,
            password_hash=hash_password(DEMO_PASSWORD),
            role=UserRole.patient,
            full_name="Meena Iyer",
            phone="+91-9000000003",
        )
        db.add(pending_patient)
        db.flush()
        db.add(PatientProfile(user_id=pending_patient.id, dob=date(1988, 2, 20), gender="female"))
        for weekday in range(6):
            for start, end in DEMO_HOURS:
                db.add(
                    DoctorAvailability(
                        doctor_id=doctor.id,
                        weekday=weekday,
                        start_time=start,
                        end_time=end,
                        slot_minutes=30,
                    )
                )
        appointment = Appointment(
            patient_id=patient.id,
            doctor_id=doctor.id,
            scheduled_at=datetime.now(timezone.utc) + timedelta(hours=2),
            status=AppointmentStatus.confirmed,
            reason_for_visit="Headache for a week",
        )
        db.add(appointment)
        db.add(Consent(patient_id=patient.id, doctor_id=doctor.id))
        db.flush()
        db.add(
            Consult(
                appointment_id=appointment.id,
                doctor_id=doctor.id,
                patient_id=patient.id,
                transcript_text=MockSTT.MOCK_TRANSCRIPT,
                transcription_status=TranscriptionStatus.ready,
            )
        )
        db.commit()

    print("Demo data created (all synthetic):")
    print(f"  Verified doctor : {DOCTOR_EMAIL} / {DEMO_PASSWORD}")
    print(
        f"  Pending doctor  : {PENDING_DOCTOR_EMAIL} / {DEMO_PASSWORD}  (approve in the admin queue)"
    )
    print(f"  Patient         : {PATIENT_EMAIL} / {DEMO_PASSWORD}  (verified)")
    print(
        f"  Pending patient : {PENDING_PATIENT_EMAIL} / {DEMO_PASSWORD}"
        "  (upload an ID, then approve it in the admin patient queue)"
    )
    print(
        "  The patient has granted consent to the verified doctor; one consult is ready for SOAP."
        "\n  The verified doctor has weekly hours, so patients can book open slots."
    )


if __name__ == "__main__":
    seed_demo()
