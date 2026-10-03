"""Patient identity verification by an admin, doctor weekly hours, and booking open slots."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models.appointment import Appointment
from app.models.enums import AppointmentStatus
from app.services.scheduling import clinic_today, clinic_zone
from tests.conftest import (
    JPEG_BYTES,
    PDF_BYTES,
    book_slot,
    new_doctor,
    new_patient,
    open_slots,
    publish_hours,
    verified_doctor,
)


def upload_id(patient, data=PDF_BYTES, name="aadhaar.pdf", mime="application/pdf"):
    return patient.upload("/api/patient/id-document", name, data, mime)


def me(api) -> dict:
    return api.get("/api/auth/me").json()


class TestPatientVerification:
    def test_new_patients_start_pending_and_cannot_book_or_grant_consent(self, doctor):
        publish_hours(doctor)
        patient = new_patient(verified=False)
        assert me(patient)["patient_status"] == "pending"
        slot = open_slots(patient, doctor)[0]  # browsing doctors and slots is allowed
        booking = patient.post(
            "/api/appointments", {"doctor_id": doctor.id, "scheduled_at": slot["start"]}
        )
        assert booking.status_code == 403 and "not verified" in booking.json()["detail"]
        assert patient.post("/api/consents", {"doctor_id": doctor.id}).status_code == 403

    def test_full_review_flow_unlocks_booking(self, admin, doctor):
        publish_hours(doctor)
        patient = new_patient(verified=False, name="Asha Rao")
        assert upload_id(patient).status_code == 200
        state = patient.get("/api/patient/verification").json()
        assert (
            state["status"] == "pending" and state["id_document_file_id"] and state["submitted_at"]
        )

        queue = admin.get("/api/admin/patients?status=pending&limit=100").json()["items"]
        mine = next(p for p in queue if p["user_id"] == patient.id)
        assert mine["id_document_file_id"] == state["id_document_file_id"]
        assert queue.index(mine) < next(  # uploaded IDs come before patients without one
            (i for i, p in enumerate(queue) if not p["id_document_file_id"]), len(queue)
        )
        assert admin.post(f"/api/admin/patients/{patient.id}/approve").status_code == 200
        assert me(patient)["patient_status"] == "verified"
        assert book_slot(patient, doctor)["status"] == "confirmed"

        detail = admin.get(f"/api/admin/patients/{patient.id}").json()
        assert [h["action"] for h in detail["history"]] == [
            "patient.id_document.upload",
            "patient.verification.approve",
        ]
        assert detail["status"] == "verified" and detail["verified_at"]

    def test_approval_needs_an_id_document_and_a_pending_patient(self, admin):
        patient = new_patient(verified=False)
        response = admin.post(f"/api/admin/patients/{patient.id}/approve")
        assert response.status_code == 400 and "ID document" in response.json()["detail"]
        upload_id(patient)
        assert admin.post(f"/api/admin/patients/{patient.id}/approve").status_code == 200
        assert admin.post(f"/api/admin/patients/{patient.id}/approve").status_code == 409
        assert (
            admin.post(
                f"/api/admin/patients/{patient.id}/reject", {"reason": "Too late to reject"}
            ).status_code
            == 409
        )
        assert admin.post(f"/api/admin/patients/{uuid.uuid4()}/approve").status_code == 404

    def test_rejection_reason_is_shown_and_a_new_upload_resubmits(self, admin):
        patient = new_patient(verified=False)
        upload_id(patient)
        assert (
            admin.post(f"/api/admin/patients/{patient.id}/reject", {"reason": "short"}).status_code
            == 422
        )
        reason = "The photo on the ID is not readable."
        assert (
            admin.post(f"/api/admin/patients/{patient.id}/reject", {"reason": reason}).status_code
            == 200
        )
        state = patient.get("/api/patient/verification").json()
        assert state["status"] == "rejected" and state["rejection_reason"] == reason
        assert me(patient)["rejection_reason"] == reason

        assert upload_id(patient, JPEG_BYTES, "id.jpg", "image/jpeg").status_code == 200
        state = patient.get("/api/patient/verification").json()
        assert state["status"] == "pending" and state["rejection_reason"] is None
        history = admin.get(f"/api/admin/patients/{patient.id}").json()["history"]
        assert [h["action"] for h in history][-1] == "patient.id_document.upload"
        assert any(h["reason"] == reason for h in history)

    def test_verified_patients_cannot_replace_their_document(self):
        patient = new_patient()
        assert upload_id(patient).status_code == 409

    def test_id_upload_validates_the_file(self):
        patient = new_patient(verified=False)
        assert upload_id(patient, b"not a pdf", "id.pdf", "application/pdf").status_code == 400
        assert upload_id(patient, PDF_BYTES, "id.exe", "application/pdf").status_code == 400

    def test_id_document_is_visible_to_its_owner_and_admins_only(self, admin, doctor):
        patient = new_patient(verified=False)
        upload_id(patient)
        file_id = patient.get("/api/patient/verification").json()["id_document_file_id"]
        assert patient.get(f"/api/files/{file_id}").status_code == 200
        assert admin.get(f"/api/files/{file_id}").status_code == 200
        assert new_patient().get(f"/api/files/{file_id}").status_code == 403
        assert doctor.get(f"/api/files/{file_id}").status_code == 403

    def test_only_admins_reach_the_patient_review_endpoints(self, doctor):
        patient = new_patient(verified=False)
        for api in (patient, doctor, new_patient()):
            assert api.get("/api/admin/patients").status_code == 403
            assert api.post(f"/api/admin/patients/{patient.id}/approve").status_code == 403
        assert doctor.post("/api/patient/id-document").status_code in (403, 422)
        assert doctor.get("/api/patient/verification").status_code == 403

    def test_stats_count_pending_and_verified_patients(self, admin):
        before = admin.get("/api/admin/stats").json()
        new_patient(verified=False)
        new_patient()
        after = admin.get("/api/admin/stats").json()
        assert after["pending_patients"] == before["pending_patients"] + 1
        assert after["verified_patients"] == before["verified_patients"] + 1

    def test_admin_list_filters_and_searches(self, admin):
        patient = new_patient(verified=False, name="Zubin Searchable")
        found = admin.get("/api/admin/patients?status=pending&q=Zubin Searchable").json()
        assert [p["user_id"] for p in found["items"]] == [patient.id]
        assert admin.get("/api/admin/patients?status=nonsense").status_code == 400


def future_weekday(days_ahead: int = 2):
    day = clinic_today() + timedelta(days=days_ahead)
    return day, day.weekday()


class TestAvailability:
    def test_doctor_publishes_hours_and_patients_see_exact_slots(self, doctor):
        day, weekday = future_weekday()
        publish_hours(
            doctor,
            [{"weekday": weekday, "start_time": "10:00", "end_time": "12:00", "slot_minutes": 30}],
        )
        saved = doctor.get("/api/doctor/availability").json()
        assert saved["timezone"] == "Asia/Kolkata"
        assert saved["rules"] == [
            {
                "id": saved["rules"][0]["id"],
                "weekday": weekday,
                "weekday_name": day.strftime("%A"),
                "start_time": "10:00",
                "end_time": "12:00",
                "slot_minutes": 30,
            }
        ]
        body = new_patient().get(f"/api/doctors/{doctor.id}/slots?days=7").json()
        days = {d["date"]: d for d in body["days"]}
        assert body["has_schedule"] is True and list(days) == [day.isoformat()]
        assert [s["time"] for s in days[day.isoformat()]["slots"]] == [
            "10:00",
            "10:30",
            "11:00",
            "11:30",
        ]
        first = datetime.fromisoformat(days[day.isoformat()]["slots"][0]["start"])
        local = first.astimezone(clinic_zone())
        assert (local.date(), local.hour, local.minute) == (day, 10, 0)

    @pytest.mark.parametrize(
        "rules,problem",
        [
            (
                [{"weekday": 0, "start_time": "12:00", "end_time": "10:00", "slot_minutes": 30}],
                "after start",
            ),
            (
                [{"weekday": 0, "start_time": "10:00", "end_time": "10:20", "slot_minutes": 30}],
                "shorter than one slot",
            ),
            (
                [{"weekday": 0, "start_time": "10:00", "end_time": "12:00", "slot_minutes": 25}],
                "Slot length",
            ),
            (
                [{"weekday": 7, "start_time": "10:00", "end_time": "12:00", "slot_minutes": 30}],
                "weekday",
            ),
            (
                [
                    {"weekday": 2, "start_time": "10:00", "end_time": "12:00", "slot_minutes": 30},
                    {"weekday": 2, "start_time": "11:00", "end_time": "13:00", "slot_minutes": 30},
                ],
                "overlap",
            ),
        ],
    )
    def test_invalid_hours_are_rejected(self, doctor, rules, problem):
        response = doctor.put("/api/doctor/availability", {"rules": rules})
        assert response.status_code == 422 and problem in response.json()["detail"]

    def test_clearing_hours_removes_every_slot(self, doctor):
        publish_hours(doctor)
        assert open_slots(new_patient(), doctor)
        assert doctor.put("/api/doctor/availability", {"rules": []}).status_code == 200
        body = new_patient().get(f"/api/doctors/{doctor.id}/slots").json()
        assert body == {**body, "has_schedule": False, "days": []}

    def test_only_verified_doctors_manage_hours(self, admin):
        pending = new_doctor()
        assert pending.get("/api/doctor/availability").status_code == 403
        assert pending.put("/api/doctor/availability", {"rules": []}).status_code == 403
        assert new_patient().put("/api/doctor/availability", {"rules": []}).status_code == 403
        assert admin.get("/api/doctor/availability").status_code == 403

    def test_slots_of_unverified_or_unknown_doctors_are_not_listed(self, doctor):
        patient = new_patient()
        assert patient.get(f"/api/doctors/{new_doctor().id}/slots").status_code == 404
        assert patient.get(f"/api/doctors/{uuid.uuid4()}/slots").status_code == 404
        assert new_doctor().get(f"/api/doctors/{doctor.id}/slots").status_code == 403

    def test_time_off_hides_slots_and_warns_about_existing_bookings(self, doctor):
        day, weekday = future_weekday()
        publish_hours(
            doctor,
            [{"weekday": weekday, "start_time": "10:00", "end_time": "12:00", "slot_minutes": 30}],
        )
        patient = new_patient()
        booked = book_slot(patient, doctor)
        off = doctor.post(
            "/api/doctor/time-off",
            {"start_date": day.isoformat(), "end_date": day.isoformat(), "reason": "Conference"},
        )
        assert off.status_code == 201 and off.json()["conflicting_appointments"] == 1
        assert open_slots(patient, doctor, days=7) == []
        assert (
            doctor.get("/api/doctor/availability").json()["time_off"][0]["reason"] == "Conference"
        )
        # the existing booking is kept; the doctor decides whether to cancel it
        mine = patient.get("/api/appointments").json()["items"]
        assert [a["status"] for a in mine if a["id"] == booked["id"]] == ["confirmed"]

        other = verified_doctor_for(doctor)
        assert other.delete(f"/api/doctor/time-off/{off.json()['id']}").status_code == 404
        assert doctor.delete(f"/api/doctor/time-off/{off.json()['id']}").status_code == 200
        assert len(open_slots(patient, doctor, days=7)) == 3  # one of the four is booked

    def test_time_off_dates_are_validated(self, doctor):
        today = clinic_today()
        bad_order = {
            "start_date": (today + timedelta(days=3)).isoformat(),
            "end_date": today.isoformat(),
        }
        assert doctor.post("/api/doctor/time-off", bad_order).status_code == 422
        past = {"start_date": "2020-01-01", "end_date": "2020-01-02"}
        assert doctor.post("/api/doctor/time-off", past).status_code == 400


def verified_doctor_for(_doctor):
    """A second verified doctor (uses the same session-wide admin flow as the fixtures)."""
    from tests.conftest import new_admin

    return verified_doctor(new_admin())


class TestBooking:
    def test_a_time_that_is_not_an_open_slot_cannot_be_booked(self, doctor):
        day, weekday = future_weekday()
        publish_hours(
            doctor,
            [{"weekday": weekday, "start_time": "10:00", "end_time": "12:00", "slot_minutes": 30}],
        )
        patient = new_patient()
        slot = datetime.fromisoformat(open_slots(patient, doctor)[0]["start"])
        for when in (
            slot + timedelta(minutes=10),
            slot + timedelta(hours=5),
            slot + timedelta(days=1),
        ):
            response = patient.post(
                "/api/appointments", {"doctor_id": doctor.id, "scheduled_at": when.isoformat()}
            )
            assert response.status_code == 409 and "open slots" in response.json()["detail"]

    def test_a_slot_can_only_be_taken_once(self, doctor):
        publish_hours(doctor)
        alice, bob = new_patient(), new_patient()
        taken = book_slot(alice, doctor)
        response = bob.post(
            "/api/appointments", {"doctor_id": doctor.id, "scheduled_at": taken["scheduled_at"]}
        )
        assert response.status_code == 409 and "just been booked" in response.json()["detail"]
        assert taken["scheduled_at"] not in [s["start"] for s in open_slots(bob, doctor)]

    def test_the_database_refuses_two_open_appointments_in_one_slot(self, doctor):
        patient = new_patient()
        when = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)
        with SessionLocal() as db:
            for _ in range(2):
                db.add(
                    Appointment(
                        patient_id=uuid.UUID(patient.id),
                        doctor_id=uuid.UUID(doctor.id),
                        scheduled_at=when,
                        status=AppointmentStatus.confirmed,
                    )
                )
            with pytest.raises(IntegrityError):
                db.commit()

    def test_upcoming_and_past_filters(self, doctor):
        patient = new_patient()
        booked = book_slot(patient, doctor)
        with SessionLocal() as db:
            db.add(
                Appointment(
                    patient_id=uuid.UUID(patient.id),
                    doctor_id=uuid.UUID(doctor.id),
                    scheduled_at=datetime.now(timezone.utc) - timedelta(days=5),
                    status=AppointmentStatus.completed,
                )
            )
            db.commit()
        upcoming = patient.get("/api/appointments?when=upcoming").json()["items"]
        past = patient.get("/api/appointments?when=past").json()["items"]
        assert [a["id"] for a in upcoming] == [booked["id"]]
        assert [a["status"] for a in past] == ["completed"]
        assert patient.get("/api/appointments?when=someday").status_code == 422
