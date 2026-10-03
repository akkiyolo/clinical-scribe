"""IDOR, consent and admin-visibility rules. Every {id} route is exercised with a stranger."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.report import AuditLog
from tests.conftest import (
    PDF_BYTES,
    Api,
    book_slot,
    grant_consent,
    new_doctor,
    new_patient,
    open_slots,
    publish_hours,
    run_to_prescription,
    verified_doctor,
)


def tomorrow() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()


def book(patient: Api, doctor: Api, index: int = 0) -> str:
    return book_slot(patient, doctor, index)["id"]


class TestAppointments:
    def test_patient_books_an_open_slot_and_it_is_confirmed(self, admin, doctor):
        publish_hours(doctor)
        patient = new_patient()
        slot = open_slots(patient, doctor)[0]
        appointment = patient.post(
            "/api/appointments",
            {"doctor_id": doctor.id, "scheduled_at": slot["start"], "reason_for_visit": "Cough"},
        ).json()
        assert appointment["status"] == "confirmed" and appointment["reason_for_visit"] == "Cough"
        assert appointment["duration_minutes"] == 30

    def test_only_verified_doctors_can_be_booked(self, doctor):
        patient = new_patient()
        for doctor_id in (new_doctor().id, str(uuid.uuid4())):
            response = patient.post(
                "/api/appointments", {"doctor_id": doctor_id, "scheduled_at": tomorrow()}
            )
            assert response.status_code == 400

    def test_past_appointment_times_are_rejected(self, doctor):
        past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        assert (
            new_patient()
            .post("/api/appointments", {"doctor_id": doctor.id, "scheduled_at": past})
            .status_code
            == 422
        )

    def test_doctors_and_admins_cannot_book(self, admin, doctor):
        for api in (doctor, admin, new_doctor()):
            assert (
                api.post(
                    "/api/appointments", {"doctor_id": doctor.id, "scheduled_at": tomorrow()}
                ).status_code
                == 403
            )

    def test_lists_are_scoped_to_the_viewer(self, admin, doctor):
        alice, bob = new_patient(name="Alice A"), new_patient(name="Bob B")
        a_id = book(alice, doctor)
        assert [i["id"] for i in alice.get("/api/appointments").json()["items"]] == [a_id]
        assert bob.get("/api/appointments").json()["total"] == 0
        assert a_id in [i["id"] for i in doctor.get("/api/appointments").json()["items"]]
        assert verified_doctor(admin).get("/api/appointments").json()["total"] == 0
        assert a_id in [i["id"] for i in admin.get("/api/appointments?limit=100").json()["items"]]

    def test_stranger_cannot_modify_someone_elses_appointment(self, admin, doctor):
        alice, mallory = new_patient(), new_patient()
        a_id = book(alice, doctor)
        assert (
            mallory.patch(f"/api/appointments/{a_id}", {"status": "cancelled"}).status_code == 403
        )
        other_doctor = verified_doctor(admin)
        assert (
            other_doctor.patch(f"/api/appointments/{a_id}", {"status": "completed"}).status_code
            == 403
        )
        assert admin.patch(f"/api/appointments/{a_id}", {"status": "completed"}).status_code == 403
        assert (
            alice.patch(f"/api/appointments/{uuid.uuid4()}", {"status": "cancelled"}).status_code
            == 404
        )

    def test_status_transitions_follow_the_rules(self, doctor):
        patient = new_patient()
        a_id = book(patient, doctor)  # confirmed on booking
        assert (
            patient.patch(f"/api/appointments/{a_id}", {"status": "completed"}).status_code == 400
        )
        assert doctor.patch(f"/api/appointments/{a_id}", {"status": "confirmed"}).status_code == 409
        assert doctor.patch(f"/api/appointments/{a_id}", {"status": "completed"}).status_code == 200
        assert (
            patient.patch(f"/api/appointments/{a_id}", {"status": "cancelled"}).status_code == 409
        )
        assert doctor.patch(f"/api/appointments/{a_id}", {"status": "cancelled"}).status_code == 409
        assert patient.patch(f"/api/appointments/{a_id}", {"status": "bogus"}).status_code == 422

    def test_patient_and_doctor_can_cancel_and_the_slot_reopens(self, doctor):
        patient = new_patient()
        first = book_slot(patient, doctor)
        assert first["scheduled_at"] not in [s["start"] for s in open_slots(patient, doctor)]
        assert (
            patient.patch(f"/api/appointments/{first['id']}", {"status": "cancelled"}).status_code
            == 200
        )
        assert (
            doctor.patch(f"/api/appointments/{first['id']}", {"status": "completed"}).status_code
            == 409
        )
        again = book_slot(patient, doctor)  # the same slot is open again
        assert again["scheduled_at"] == first["scheduled_at"]
        assert (
            doctor.patch(f"/api/appointments/{again['id']}", {"status": "cancelled"}).status_code
            == 200
        )

    def test_pagination_limits_are_clamped(self, doctor):
        patient = new_patient()
        for index in range(3):
            book(patient, doctor, index)
        body = patient.get("/api/appointments?limit=2&offset=1").json()
        assert (
            body["limit"] == 2
            and body["offset"] == 1
            and len(body["items"]) == 2
            and body["total"] == 3
        )
        assert patient.get("/api/appointments?limit=9999").json()["limit"] == 100
        assert patient.get("/api/appointments?limit=0").json()["limit"] == 1


class TestConsents:
    def test_grant_list_and_duplicate(self, doctor):
        patient = new_patient()
        consent_id = grant_consent(patient, doctor)
        assert patient.post("/api/consents", {"doctor_id": doctor.id}).status_code == 409
        items = patient.get("/api/consents").json()["items"]
        assert [i["id"] for i in items] == [consent_id] and items[0]["doctor_name"]
        assert patient.post("/api/consents", {"doctor_id": new_doctor().id}).status_code == 400

    def test_only_the_owner_can_revoke_and_only_once(self, doctor):
        patient, mallory = new_patient(), new_patient()
        consent_id = grant_consent(patient, doctor)
        assert mallory.delete(f"/api/consents/{consent_id}").status_code == 403
        assert doctor.delete(f"/api/consents/{consent_id}").status_code == 403
        assert patient.delete(f"/api/consents/{consent_id}").status_code == 200
        assert patient.delete(f"/api/consents/{consent_id}").status_code == 409
        assert patient.delete(f"/api/consents/{uuid.uuid4()}").status_code == 404

    def test_regranting_after_revoking_works(self, doctor):
        patient = new_patient()
        patient.delete(f"/api/consents/{grant_consent(patient, doctor)}")
        grant_consent(patient, doctor)
        assert [
            c["revoked_at"] is None for c in patient.get("/api/consents").json()["items"]
        ].count(True) == 1

    def test_doctors_cannot_grant_and_see_only_their_active_consents(self, doctor):
        patient = new_patient()
        assert doctor.post("/api/consents", {"doctor_id": doctor.id}).status_code == 403
        consent_id = grant_consent(patient, doctor)
        assert [c["id"] for c in doctor.get("/api/consents").json()["items"]] == [consent_id]
        patient.delete(f"/api/consents/{consent_id}")
        assert doctor.get("/api/consents").json()["items"] == []


class TestDoctorPatientAccess:
    def test_doctor_sees_only_consented_patients(self, doctor):
        consenting, other = new_patient(name="Consenting Pat"), new_patient(name="Other Pat")
        grant_consent(consenting, doctor)
        ids = [p["id"] for p in doctor.get("/api/doctor/patients").json()["items"]]
        assert ids == [consenting.id]
        assert doctor.get(f"/api/doctor/patients/{consenting.id}").status_code == 200
        assert doctor.get(f"/api/doctor/patients/{other.id}").status_code == 404
        assert doctor.get(f"/api/doctor/patients/{uuid.uuid4()}").status_code == 404

    def test_other_doctor_cannot_see_the_patient_without_their_own_consent(self, admin, doctor):
        patient = new_patient()
        grant_consent(patient, doctor)
        other = verified_doctor(admin)
        assert other.get("/api/doctor/patients").json()["items"] == []
        assert other.get(f"/api/doctor/patients/{patient.id}").status_code == 404

    def test_revoking_consent_blocks_the_doctor_immediately(self, consulting):
        doc, pat, cid = consulting.doctor, consulting.patient, consulting.consult_id
        draft = run_to_prescription(consulting)
        assert doc.get(f"/api/doctor/patients/{pat.id}").status_code == 200
        assert pat.delete(f"/api/consents/{consulting.consent_id}").status_code == 200

        assert doc.get(f"/api/doctor/patients/{pat.id}").status_code == 404
        assert doc.get("/api/doctor/patients").json()["items"] == []
        assert doc.get(f"/api/consults/{cid}").status_code == 403
        assert cid not in [c["id"] for c in doc.get("/api/consults").json()["items"]]
        assert (
            doc.put(f"/api/consults/{cid}/transcript", {"transcript_text": "x"}).status_code == 403
        )
        assert doc.post(f"/api/consults/{cid}/soap/generate").status_code == 403
        assert doc.get(f"/api/prescriptions/{draft['id']}").status_code == 403
        assert (
            doc.post(
                f"/api/prescriptions/{draft['id']}/approve", {"acknowledged_flag_ids": []}
            ).status_code
            == 403
        )
        assert doc.post("/api/consults", {"patient_id": pat.id}).status_code == 403
        assert (
            doc.get(f"/api/files/{draft['docx_file_id']}").status_code == 200
        )  # own file stays: it is the doctor's
        # the doctor can no longer reach the patient's data, but the patient's own view is unaffected
        assert pat.get(f"/api/consults/{cid}").status_code == 200


class TestConsultAndPrescriptionIdor:
    def test_other_doctors_and_patients_cannot_reach_a_consult_or_prescription(
        self, admin, consulting
    ):
        draft = run_to_prescription(consulting)
        cid, pid = consulting.consult_id, draft["id"]
        stranger_doctor, stranger_patient = verified_doctor(admin), new_patient()
        grant_consent(
            consulting.patient, stranger_doctor
        )  # even with consent from the same patient

        for url in (
            f"/api/consults/{cid}",
            f"/api/consults/{cid}/prescriptions",
            f"/api/prescriptions/{pid}",
        ):
            assert stranger_doctor.get(url).status_code in (403, 404), url
            assert stranger_patient.get(url).status_code in (403, 404), url
        write_calls = [
            ("put", f"/api/consults/{cid}/transcript", {"transcript_text": "x"}),
            ("post", f"/api/consults/{cid}/soap/generate", None),
            ("put", f"/api/consults/{cid}/soap", {"plan": "x"}),
            ("post", f"/api/consults/{cid}/soap/approve", None),
            ("post", f"/api/consults/{cid}/retry", None),
            ("post", f"/api/consults/{cid}/prescriptions/regenerate", None),
            ("put", f"/api/prescriptions/{pid}", {"content": {"medications": []}}),
            ("post", f"/api/prescriptions/{pid}/approve", {"acknowledged_flag_ids": []}),
            ("post", f"/api/prescriptions/{pid}/reject", None),
        ]
        for method, url, body in write_calls:
            response = getattr(stranger_doctor, method)(
                url, **({"json": body} if body is not None else {})
            )
            assert response.status_code in (403, 404), (method, url, response.status_code)
        assert stranger_doctor.upload(
            f"/api/consults/{cid}/audio", "a.webm", b"\x1a\x45\xdf\xa3" + b"0" * 20, "audio/webm"
        ).status_code in (403, 404)
        assert (
            consulting.doctor.get(f"/api/consults/{cid}").status_code == 200
        )  # untouched for the owner

    def test_doctor_cannot_start_a_consult_for_a_non_consenting_patient_or_foreign_appointment(
        self, admin, doctor
    ):
        patient, other_patient = new_patient(), new_patient()
        assert doctor.post("/api/consults", {"patient_id": patient.id}).status_code == 403
        grant_consent(patient, doctor)
        grant_consent(other_patient, doctor)
        foreign = book(other_patient, doctor)
        assert (
            doctor.post(
                "/api/consults", {"patient_id": patient.id, "appointment_id": foreign}
            ).status_code
            == 400
        )
        mine = book(patient, doctor)
        assert (
            doctor.post(
                "/api/consults", {"patient_id": patient.id, "appointment_id": mine}
            ).status_code
            == 201
        )
        assert (
            doctor.post(
                "/api/consults", {"patient_id": patient.id, "appointment_id": mine}
            ).status_code
            == 409
        )

    def test_patients_see_consult_metadata_only(self, consulting):
        run_to_prescription(consulting)
        body = consulting.patient.get(f"/api/consults/{consulting.consult_id}").json()
        assert set(body) == {
            "id",
            "appointment_id",
            "doctor_id",
            "patient_id",
            "status",
            "created_at",
            "patient_name",
            "doctor_name",
        }
        listing = consulting.patient.get("/api/consults").json()["items"][0]
        assert "transcript_text" not in listing and "soap" not in listing

    def test_admin_sees_metadata_but_never_clinical_content(self, admin, consulting):
        draft = run_to_prescription(consulting)
        cid = consulting.consult_id
        body = admin.get(f"/api/consults/{cid}").json()
        assert (
            "transcript_text" not in body
            and "soap" not in body
            and body["status"] == "prescription_ready"
        )
        assert "transcript_text" not in str(admin.get("/api/consults").json())
        meta = admin.get(f"/api/prescriptions/{draft['id']}").json()
        assert "content" not in meta and "safety_flags" not in meta and meta["version"] == 1
        listed = admin.get("/api/prescriptions?limit=100").json()["items"]
        assert all("content" not in item for item in listed)
        assert all(
            "content" not in item
            for item in admin.get(f"/api/consults/{cid}/prescriptions").json()["items"]
        )
        assert admin.get(f"/api/files/{draft['docx_file_id']}").status_code == 403

    def test_admin_opens_consult_content_only_through_a_report_and_it_is_audited(
        self, admin, consulting, db
    ):
        run_to_prescription(consulting)
        cid, doctor_id = consulting.consult_id, consulting.doctor.id
        report = consulting.patient.post(
            "/api/reports",
            {
                "doctor_id": doctor_id,
                "consult_id": cid,
                "reason": "Wrong medication advised",
                "details": "Please review",
            },
        )
        assert report.status_code == 201
        report_id = report.json()["id"]

        content = admin.get(f"/api/admin/reports/{report_id}/consult").json()
        assert content["transcript"] and content["soap"]["plan"] and content["prescriptions"]
        row = db.scalars(
            select(AuditLog)
            .where(AuditLog.action == "report.consult.access")
            .where(AuditLog.resource_id == cid)
        ).first()
        assert (
            row is not None
            and row.metadata_["report_id"] == report_id
            and row.actor_role == "admin"
        )

        no_consult = consulting.patient.post(
            "/api/reports", {"doctor_id": doctor_id, "reason": "General complaint"}
        )
        assert admin.get(f"/api/admin/reports/{no_consult.json()['id']}/consult").status_code == 404

    def test_reports_can_only_reference_the_reporters_own_consult_with_that_doctor(
        self, consulting
    ):
        stranger = new_patient()
        doctor_id = consulting.doctor.id
        body = {
            "doctor_id": doctor_id,
            "consult_id": consulting.consult_id,
            "reason": "A genuine concern",
        }
        assert stranger.post("/api/reports", body).status_code == 400
        assert (
            consulting.patient.post(
                "/api/reports", {**body, "doctor_id": str(uuid.uuid4())}
            ).status_code
            == 404
        )
        assert consulting.doctor.post("/api/reports", body).status_code == 403

    def test_report_resolution_flow(self, admin, doctor):
        patient = new_patient()
        report_id = patient.post(
            "/api/reports", {"doctor_id": doctor.id, "reason": "Rude behaviour"}
        ).json()["id"]
        assert any(
            r["id"] == report_id
            for r in admin.get("/api/admin/reports?state=open&limit=100").json()["items"]
        )
        assert (
            admin.post(
                f"/api/admin/reports/{report_id}/resolve", {"resolution_note": "no"}
            ).status_code
            == 422
        )
        assert (
            admin.post(
                f"/api/admin/reports/{report_id}/resolve",
                {"resolution_note": "Spoke to the doctor"},
            ).status_code
            == 200
        )
        assert (
            admin.post(
                f"/api/admin/reports/{report_id}/resolve", {"resolution_note": "Again again"}
            ).status_code
            == 409
        )
        assert not any(
            r["id"] == report_id
            for r in admin.get("/api/admin/reports?state=open&limit=100").json()["items"]
        )
        assert admin.get("/api/admin/reports?state=nonsense").status_code == 422


class TestFileIdor:
    def test_profile_photos_follow_visibility_rules(self, admin, doctor):
        from tests.conftest import PNG_BYTES

        patient = new_patient()
        patient_photo = patient.upload("/api/me/photo", "me.png", PNG_BYTES, "image/png").json()[
            "file_id"
        ]
        doctor_photo = doctor.upload("/api/me/photo", "dr.png", PNG_BYTES, "image/png").json()[
            "file_id"
        ]
        assert patient.get(f"/api/files/{patient_photo}").status_code == 200
        assert (
            new_patient().get(f"/api/files/{doctor_photo}").status_code == 200
        )  # verified doctor avatars
        assert new_patient().get(f"/api/files/{patient_photo}").status_code == 403
        assert doctor.get(f"/api/files/{patient_photo}").status_code == 403  # no consent yet
        grant_consent(patient, doctor)
        assert doctor.get(f"/api/files/{patient_photo}").status_code == 200
        assert admin.get(f"/api/files/{patient_photo}").status_code == 200
        pending = new_doctor()
        pending_photo = pending.upload("/api/me/photo", "p.png", PNG_BYTES, "image/png").json()[
            "file_id"
        ]
        assert (
            new_patient().get(f"/api/files/{pending_photo}").status_code == 403
        )  # unverified: not public

    def test_unknown_file_is_404_and_anonymous_is_401(self, doctor):
        assert doctor.get(f"/api/files/{uuid.uuid4()}").status_code == 404
        assert Api().get(f"/api/files/{uuid.uuid4()}").status_code == 401

    def test_consult_audio_only_for_its_doctor(self, admin, consulting):
        from tests.conftest import WEBM_BYTES

        response = consulting.doctor.upload(
            f"/api/consults/{consulting.consult_id}/audio", "rec.webm", WEBM_BYTES, "audio/webm"
        )
        assert response.status_code == 200
        file_id = consulting.doctor.get(f"/api/consults/{consulting.consult_id}").json()[
            "audio_file_id"
        ]
        assert consulting.doctor.get(f"/api/files/{file_id}").status_code == 200
        for other in (consulting.patient, admin, verified_doctor(admin)):
            assert other.get(f"/api/files/{file_id}").status_code == 403

    def test_license_certificate_unreachable_for_other_doctors(self, doctor):
        file_id = doctor.upload(
            "/api/doctor/license", "n.pdf", PDF_BYTES, "application/pdf"
        ).json()["file_id"]
        assert new_doctor().get(f"/api/files/{file_id}").status_code == 403
