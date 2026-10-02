"""Doctor license verification: state machine, registry evidence, admin review, access gate."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models.doctor import DoctorProfile
from app.models.enums import DoctorStatus, FileCategory
from app.models.file import File
from app.models.registry import VerificationCheck, VerificationEvent
from app.services.verification import (
    HPRRegistryChecker,
    InvalidTransition,
    MockRegistryChecker,
    TransitionError,
    normalize_name,
    normalize_reg_number,
    transition_doctor_status,
)
from tests.conftest import (
    PDF_BYTES,
    PNG_BYTES,
    Api,
    new_admin,
    new_doctor,
    new_patient,
    registry_record,
    verified_doctor,
)


def actor(role: str, user_id=None):
    return SimpleNamespace(id=user_id, role=SimpleNamespace(value=role))


def real_admin_actor():
    """An admin actor backed by a real users row (verified_by / actor_id are foreign keys)."""
    return actor("admin", uuid.UUID(new_admin().id))


def give_license(db, profile: DoctorProfile) -> None:
    """Attach a real files row as the license certificate (foreign keys are enforced on PostgreSQL)."""
    record = File(
        owner_id=profile.user_id,
        category=FileCategory.license_certificate,
        s3_key=f"test/{uuid.uuid4()}.pdf",
        original_filename="license.pdf",
        content_type="application/pdf",
        size_bytes=10,
        sha256="0" * 64,
    )
    db.add(record)
    db.flush()
    profile.license_file_id = record.id


def profile_of(db, doctor: Api) -> DoctorProfile:
    db.expire_all()
    return db.get(DoctorProfile, uuid.UUID(doctor.id))


class TestStateMachine:
    def test_legal_transitions_each_write_an_event(self, db):
        doctor = new_doctor()
        admin = real_admin_actor()  # created first: a pending flush would hold SQLite's write lock
        profile = profile_of(db, doctor)
        give_license(db, profile)  # approval needs a license on file
        db.flush()
        before = db.query(VerificationEvent).filter_by(doctor_id=profile.user_id).count()

        transition_doctor_status(
            db, profile, "rejected", admin, reason="License photo is unreadable"
        )
        transition_doctor_status(db, profile, "pending", actor("doctor", profile.user_id))
        transition_doctor_status(db, profile, "verified", admin)
        transition_doctor_status(
            db, profile, "suspended", admin, reason="Complaint under investigation"
        )
        transition_doctor_status(
            db, profile, "verified", admin, reason="Investigation closed in favour"
        )
        events = (
            db.query(VerificationEvent)
            .filter_by(doctor_id=profile.user_id)
            .order_by(VerificationEvent.created_at)
            .all()
        )
        assert [e.action.value for e in events][before:] == [
            "rejected",
            "resubmitted",
            "approved",
            "suspended",
            "reinstated",
        ]
        db.rollback()

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            ("pending", "pending"),
            ("pending", "suspended"),
            ("rejected", "verified"),
            ("rejected", "suspended"),
            ("verified", "pending"),
            ("verified", "rejected"),
            ("verified", "verified"),
            ("suspended", "pending"),
            ("suspended", "rejected"),
            ("suspended", "suspended"),
        ],
    )
    def test_every_illegal_transition_raises(self, db, from_status, to_status):
        doctor = new_doctor()
        profile = profile_of(db, doctor)
        profile.status = DoctorStatus(from_status)
        give_license(db, profile)
        for who in (
            actor("admin", uuid.uuid4()),
            actor("doctor", profile.user_id),
            actor("system"),
        ):
            with pytest.raises(InvalidTransition):
                transition_doctor_status(
                    db, profile, to_status, who, reason="A perfectly good reason"
                )
        db.rollback()

    def test_wrong_actor_is_refused(self, db):
        profile = profile_of(db, new_doctor())
        give_license(db, profile)
        with pytest.raises(TransitionError):
            transition_doctor_status(db, profile, "verified", actor("doctor", profile.user_id))
        with pytest.raises(TransitionError):
            transition_doctor_status(db, profile, "verified", actor("system"))
        profile.status = DoctorStatus.rejected
        with pytest.raises(TransitionError):  # another doctor cannot resubmit on someone's behalf
            transition_doctor_status(db, profile, "pending", actor("doctor", uuid.uuid4()))
        with pytest.raises(TransitionError):  # nor can an admin
            transition_doctor_status(db, profile, "pending", actor("admin", uuid.uuid4()))
        db.rollback()

    def test_reasons_are_required_where_the_spec_says(self, db):
        admin = real_admin_actor()
        profile = profile_of(db, new_doctor())
        give_license(db, profile)
        with pytest.raises(TransitionError):
            transition_doctor_status(db, profile, "rejected", admin, reason="too short")
        with pytest.raises(TransitionError):
            transition_doctor_status(db, profile, "rejected", admin, reason=None)
        transition_doctor_status(db, profile, "verified", admin)  # approve needs no reason
        with pytest.raises(TransitionError):
            transition_doctor_status(db, profile, "suspended", admin, reason="  short  ")
        transition_doctor_status(
            db, profile, "suspended", admin, reason="Valid reason for suspension"
        )
        with pytest.raises(TransitionError):
            transition_doctor_status(db, profile, "verified", admin, reason="no")
        db.rollback()

    def test_approval_requires_a_license_on_file(self, db):
        profile = profile_of(db, new_doctor())
        assert profile.license_file_id is None
        with pytest.raises(TransitionError, match="license"):
            transition_doctor_status(db, profile, "verified", real_admin_actor())
        db.rollback()

    def test_no_update_or_delete_helper_exists_for_append_only_tables(self):
        import app.services.audit as audit_module
        import app.services.verification as verification_module

        for module in (audit_module, verification_module):
            names = [n.lower() for n in dir(module) if callable(getattr(module, n))]
            assert not any(
                ("delete_" in n or "update_event" in n or "update_audit" in n) for n in names
            )


class TestRegistryChecker:
    def test_exact_match(self, db):
        result = MockRegistryChecker(db).check(
            "MH-2015-12345", "Maharashtra Medical Council", "Dr. Priya Sharma"
        )
        assert result.registry_match and result.name_match_score == 100.0
        assert result.matched_record["reg_number"] == "MH-2015-12345"

    def test_match_ignores_case_spacing_punctuation_titles_and_name_order(self, db):
        result = MockRegistryChecker(db).check(
            " mh 2015/12345 ", "MAHARASHTRA  medical council", "SHARMA priya, Dr"
        )
        assert result.registry_match and result.name_match_score == 100.0

    def test_name_mismatch_scores_low(self, db):
        result = MockRegistryChecker(db).check(
            "MH-2015-12345", "Maharashtra Medical Council", "Dr. Someone Else"
        )
        assert result.registry_match and result.name_match_score < 50

    def test_unknown_number(self, db):
        result = MockRegistryChecker(db).check(
            "ZZ-0000", "Maharashtra Medical Council", "Dr. Priya Sharma"
        )
        assert (
            not result.registry_match
            and result.matched_record is None
            and result.name_match_score == 0
        )

    def test_same_number_in_another_council_is_not_a_match(self, db):
        assert (
            not MockRegistryChecker(db)
            .check("MH-2015-12345", "Gujarat Medical Council", "Dr. Priya Sharma")
            .registry_match
        )

    def test_hpr_checker_is_a_documented_stub(self):
        with pytest.raises(NotImplementedError, match="HPR"):
            HPRRegistryChecker().check("1", "NMC", "x")

    def test_normalisers(self):
        assert normalize_name("Dr. A. B. O'Neil") == "a b o neil"
        assert normalize_reg_number("MH-2015/12345 ") == "mh201512345"

    def test_duplicate_registration_is_flagged_and_never_changes_status(self, db):
        new_doctor(reg_number="DUPE-1/A", council="NMC")
        second = new_doctor(reg_number="dupe 1 a", council="nmc")
        profile = profile_of(db, second)
        check = db.query(VerificationCheck).filter_by(doctor_id=profile.user_id).first()
        assert check.duplicate_reg_flag is True
        assert profile.status == DoctorStatus.pending  # evidence only, never an approval

    def test_a_perfect_registry_match_still_leaves_the_doctor_pending(self, db):
        doctor = new_doctor(name="Dr. Priya Sharma", reg_number=registry_record("Dr. Priya Sharma"))
        profile = profile_of(db, doctor)
        check = db.query(VerificationCheck).filter_by(doctor_id=profile.user_id).first()
        assert check.registry_match and float(check.name_match_score) == 100.0
        assert profile.status == DoctorStatus.pending and profile.verified_at is None


class TestAdminReview:
    def test_new_doctor_is_not_verified_until_an_admin_approves(self, admin):
        doctor = new_doctor()
        assert (
            doctor.upload("/api/doctor/license", "l.pdf", PDF_BYTES, "application/pdf").json()[
                "doctor_status"
            ]
            == "pending"
        )
        assert doctor.upload("/api/me/photo", "p.png", PNG_BYTES, "image/png").status_code == 200
        assert (
            doctor.get("/api/auth/me").json()["doctor_status"] == "pending"
        )  # no auto-verification
        assert doctor.get("/api/doctor/patients").status_code == 403
        assert admin.post(f"/api/admin/doctors/{doctor.id}/approve").status_code == 200
        assert doctor.get("/api/doctor/patients").status_code == 200

    def test_queue_lists_pending_oldest_first_with_evidence_badges(self, admin):
        tag = uuid.uuid4().hex[:8]
        first = new_doctor(
            name=f"Dr. Queue {tag} One", reg_number=registry_record(f"Dr. Queue {tag} One")
        )
        second = new_doctor(name=f"Dr. Queue {tag} Two")
        items = admin.get(f"/api/admin/doctors?status=pending&q={tag}&limit=100").json()["items"]
        assert [i["user_id"] for i in items] == [first.id, second.id]  # oldest submission first
        assert items[0]["registry_match"] is True and items[0]["name_match_score"] == 100.0
        assert items[1]["registry_match"] is False and items[1]["duplicate_reg_flag"] is False

    def test_approve_without_license_is_refused(self, admin):
        doctor = new_doctor()
        response = admin.post(f"/api/admin/doctors/{doctor.id}/approve")
        assert response.status_code == 400 and "license" in response.json()["detail"]

    def test_reject_needs_a_reason_of_at_least_ten_characters(self, admin):
        doctor = new_doctor()
        assert (
            admin.post(f"/api/admin/doctors/{doctor.id}/reject", {"reason": "short"}).status_code
            == 422
        )
        assert admin.post(f"/api/admin/doctors/{doctor.id}/reject", {}).status_code == 422
        assert (
            admin.post(
                f"/api/admin/doctors/{doctor.id}/reject", {"reason": "Certificate is unreadable"}
            ).status_code
            == 200
        )
        me = doctor.get("/api/auth/me").json()
        assert (
            me["doctor_status"] == "rejected"
            and me["rejection_reason"] == "Certificate is unreadable"
        )

    def test_illegal_admin_transitions_return_409(self, admin):
        doctor = new_doctor()
        assert (
            admin.post(
                f"/api/admin/doctors/{doctor.id}/suspend", {"reason": "Not verified yet really"}
            ).status_code
            == 409
        )
        assert (
            admin.post(
                f"/api/admin/doctors/{doctor.id}/reinstate", {"reason": "Not suspended at all"}
            ).status_code
            == 409
        )

    def test_resubmission_needs_a_new_certificate_and_reruns_the_check(self, admin, db):
        doctor = new_doctor()
        doctor.upload("/api/doctor/license", "first.pdf", PDF_BYTES, "application/pdf")
        admin.post(
            f"/api/admin/doctors/{doctor.id}/reject", {"reason": "Please upload a clearer scan"}
        )
        assert doctor.post("/api/doctor/resubmit").status_code == 400  # same certificate as before
        assert (
            doctor.patch("/api/doctor/verification", {"specialization": "Cardiology"}).status_code
            == 200
        )
        assert (
            doctor.upload(
                "/api/doctor/license", "second.pdf", PDF_BYTES, "application/pdf"
            ).status_code
            == 200
        )
        checks_before = (
            db.query(VerificationCheck).filter_by(doctor_id=uuid.UUID(doctor.id)).count()
        )
        response = doctor.post("/api/doctor/resubmit")
        assert response.status_code == 200 and response.json()["doctor_status"] == "pending"
        db.expire_all()
        assert (
            db.query(VerificationCheck).filter_by(doctor_id=uuid.UUID(doctor.id)).count()
            == checks_before + 1
        )
        actions = [
            e["action"] for e in admin.get(f"/api/admin/doctors/{doctor.id}").json()["events"]
        ]
        assert actions.count("resubmitted") == 1 and "rejected" in actions

    def test_details_are_locked_once_verified_and_editable_while_pending_or_rejected(self, admin):
        doctor = new_doctor()
        assert (
            doctor.patch("/api/doctor/verification", {"clinic_name": "New Clinic"}).status_code
            == 200
        )
        doctor.upload("/api/doctor/license", "l.pdf", PDF_BYTES, "application/pdf")
        admin.post(f"/api/admin/doctors/{doctor.id}/approve")
        assert (
            doctor.patch("/api/doctor/verification", {"clinic_name": "Sneaky"}).status_code == 400
        )

    def test_editing_registration_details_while_pending_reruns_the_registry_check(self, admin, db):
        doctor = new_doctor()
        before = db.query(VerificationCheck).filter_by(doctor_id=uuid.UUID(doctor.id)).count()
        number = registry_record(doctor.user["full_name"])
        assert doctor.patch("/api/doctor/verification", {"reg_number": number}).status_code == 200
        db.expire_all()
        assert (
            db.query(VerificationCheck).filter_by(doctor_id=uuid.UUID(doctor.id)).count()
            == before + 1
        )

    def test_invalid_registration_year_in_update_is_rejected(self):
        assert new_doctor().patch("/api/doctor/verification", {"reg_year": 1900}).status_code == 422

    def test_suspension_takes_effect_on_the_very_next_request_of_an_active_session(self, admin):
        doctor = verified_doctor(admin)
        assert doctor.get("/api/doctor/patients").status_code == 200
        assert (
            admin.post(
                f"/api/admin/doctors/{doctor.id}/suspend", {"reason": "Pending an investigation"}
            ).status_code
            == 200
        )
        response = doctor.get("/api/doctor/patients")
        assert (
            response.status_code == 403
            and response.json()["detail"] == "Your account is not verified yet"
        )
        assert (
            admin.post(
                f"/api/admin/doctors/{doctor.id}/reinstate", {"reason": "Investigation closed"}
            ).status_code
            == 200
        )
        assert doctor.get("/api/doctor/patients").status_code == 200

    def test_license_certificate_is_visible_to_admin_and_owner_only(self, admin):
        doctor = new_doctor()
        file_id = doctor.upload(
            "/api/doctor/license", "l.pdf", PDF_BYTES, "application/pdf"
        ).json()["file_id"]
        assert admin.get(f"/api/files/{file_id}").status_code == 200
        assert doctor.get(f"/api/files/{file_id}").status_code == 200
        assert new_patient().get(f"/api/files/{file_id}").status_code == 403
        assert new_doctor().get(f"/api/files/{file_id}").status_code == 403

    def test_verified_badge_only_for_verified_doctors_in_the_directory(self, admin):
        tag = uuid.uuid4().hex[:8]
        pending = new_doctor(name=f"Dr. Pending {tag}")
        verified = verified_doctor(admin, name=f"Dr. Verified {tag}")
        patient = new_patient()
        listing = patient.get(f"/api/doctors?q={tag}&limit=100").json()["items"]
        assert [d["id"] for d in listing] == [verified.id]  # the pending doctor is not listed
        assert pending.id not in [d["id"] for d in listing] and all(
            d["is_verified"] for d in listing
        )
        entry = listing[0]
        assert set(entry) == {
            "id",
            "full_name",
            "specialization",
            "clinic_name",
            "clinic_address",
            "profile_photo_url",
            "is_verified",
        }  # public fields only

    def test_directory_requires_login_and_excludes_unverified_doctors(self, admin):
        assert Api().get("/api/doctors").status_code == 401
        assert new_doctor().get("/api/doctors").status_code == 403
        assert verified_doctor(admin).get("/api/doctors").status_code == 200
        assert admin.get("/api/doctors").status_code == 200

    def test_directory_search_and_wildcards_are_literal(self, admin):
        verified_doctor(admin, name="Dr. Zelda Zebra")
        patient = new_patient()
        assert [d["full_name"] for d in patient.get("/api/doctors?q=zebra").json()["items"]] == [
            "Dr. Zelda Zebra"
        ]
        assert patient.get("/api/doctors?q=%25").json()["total"] == 0  # "%" is not a wildcard


CLINICAL_ROUTES = [
    ("get", "/api/doctor/patients"),
    ("get", f"/api/doctor/patients/{uuid.uuid4()}"),
    ("get", "/api/doctor/dashboard"),
    ("post", "/api/consults"),
    ("get", "/api/consults"),
    ("get", f"/api/consults/{uuid.uuid4()}"),
    ("post", f"/api/consults/{uuid.uuid4()}/audio"),
    ("put", f"/api/consults/{uuid.uuid4()}/transcript"),
    ("post", f"/api/consults/{uuid.uuid4()}/soap/generate"),
    ("put", f"/api/consults/{uuid.uuid4()}/soap"),
    ("post", f"/api/consults/{uuid.uuid4()}/soap/approve"),
    ("post", f"/api/consults/{uuid.uuid4()}/retry"),
    ("post", f"/api/consults/{uuid.uuid4()}/prescriptions/regenerate"),
    ("get", f"/api/consults/{uuid.uuid4()}/prescriptions"),
    ("get", "/api/prescriptions"),
    ("get", f"/api/prescriptions/{uuid.uuid4()}"),
    ("put", f"/api/prescriptions/{uuid.uuid4()}"),
    ("post", f"/api/prescriptions/{uuid.uuid4()}/approve"),
    ("post", f"/api/prescriptions/{uuid.uuid4()}/reject"),
    ("get", "/api/appointments"),
    ("patch", f"/api/appointments/{uuid.uuid4()}"),
    ("get", "/api/consents"),
    ("get", "/api/doctors"),
    ("post", f"/api/prescriptions/{uuid.uuid4()}/speak"),
]


def drive(api: Api, method: str, url: str):
    kwargs = {}
    if url.endswith("/audio"):
        return api.upload(url, "a.webm", b"x", "audio/webm")
    if method in ("post", "put", "patch"):
        kwargs["json"] = {}
    return getattr(api, method)(url, **kwargs)


@pytest.mark.parametrize("state", ["pending", "rejected", "suspended"])
@pytest.mark.parametrize("method,url", CLINICAL_ROUTES)
def test_unverified_doctors_get_403_on_every_clinical_route(admin, state, method, url):
    doctor = new_doctor()
    doctor.upload("/api/doctor/license", "l.pdf", PDF_BYTES, "application/pdf")
    if state == "rejected":
        admin.post(f"/api/admin/doctors/{doctor.id}/reject", {"reason": "Rejected for this test"})
    if state == "suspended":
        admin.post(f"/api/admin/doctors/{doctor.id}/approve")
        admin.post(f"/api/admin/doctors/{doctor.id}/suspend", {"reason": "Suspended for this test"})
    response = drive(doctor, method, url)
    assert response.status_code == 403, (state, method, url, response.text)
    assert (
        response.json()["detail"] == "Your account is not verified yet"
        or "verified" in response.json()["detail"]
    )


ADMIN_ROUTES = [
    ("get", "/api/admin/stats"),
    ("get", "/api/admin/doctors"),
    ("get", f"/api/admin/doctors/{uuid.uuid4()}"),
    ("post", f"/api/admin/doctors/{uuid.uuid4()}/approve"),
    ("post", f"/api/admin/doctors/{uuid.uuid4()}/reject"),
    ("post", f"/api/admin/doctors/{uuid.uuid4()}/suspend"),
    ("post", f"/api/admin/doctors/{uuid.uuid4()}/reinstate"),
    ("get", "/api/admin/reports"),
    ("get", f"/api/admin/reports/{uuid.uuid4()}"),
    ("get", f"/api/admin/reports/{uuid.uuid4()}/consult"),
    ("post", f"/api/admin/reports/{uuid.uuid4()}/resolve"),
    ("get", "/api/admin/audit-logs"),
]


@pytest.mark.parametrize("method,url", ADMIN_ROUTES)
def test_non_admins_get_403_and_anonymous_401_on_admin_routes(admin, method, url):
    for api in (new_patient(), verified_doctor(admin), new_doctor()):
        assert drive(api, method, url).status_code == 403, (method, url)
    assert drive(Api(), method, url).status_code in (401, 403)
