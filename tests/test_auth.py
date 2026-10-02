"""Registration, login, logout, sessions, CSRF and rate limits."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.models.doctor import DoctorProfile
from app.models.enums import DoctorStatus
from app.models.report import AuditLog
from app.models.user import User
from app.rate_limit import EmailRateLimiter, limiter, login_email_limiter
from tests.conftest import PASSWORD, Api, new_admin, new_doctor, new_patient, unique_email


def register_body(**overrides):
    body = {
        "email": unique_email("reg"),
        "password": PASSWORD,
        "full_name": "Reg Tester",
        "role": "patient",
    }
    body.update(overrides)
    return body


class TestRegister:
    def test_patient_registers_and_is_logged_in(self):
        api = Api()
        response = api.post("/api/auth/register", register_body(dob="1991-01-01", gender="female"))
        assert response.status_code == 201
        assert response.json()["role"] == "patient"
        assert api.get("/api/auth/me").json()["role"] == "patient"

    def test_doctor_starts_pending_and_gets_submitted_and_auto_check_events(self, db):
        doctor = new_doctor()
        assert doctor.user["doctor_status"] == "pending"
        profile = db.get(DoctorProfile, uuid.UUID(doctor.id))
        assert profile.status == DoctorStatus.pending and profile.submitted_at is not None
        admin = new_admin()
        events = admin.get(f"/api/admin/doctors/{doctor.id}").json()["events"]
        assert [e["action"] for e in events] == ["submitted", "auto_checked"]

    def test_duplicate_email_returns_generic_error(self):
        first = new_patient()
        response = Api().post("/api/auth/register", register_body(email=first.email))
        assert response.status_code == 400
        assert response.json()["detail"] == "Could not create account"

    def test_duplicate_email_differs_only_by_case_and_spaces(self):
        first = new_patient()
        response = Api().post(
            "/api/auth/register", register_body(email=f"  {first.email.upper()} ")
        )
        assert response.status_code == 400

    def test_admin_role_cannot_be_registered(self):
        response = Api().post("/api/auth/register", register_body(role="admin"))
        assert response.status_code == 422

    def test_password_rules(self):
        api = Api()
        for bad in ("short1", "alllettersonly", "12345678901"):
            response = api.post("/api/auth/register", register_body(password=bad))
            assert response.status_code == 422, bad
            assert bad not in response.text  # submitted values are never echoed back
        assert api.post("/api/auth/register", register_body(password="a1" * 70)).status_code == 422

    def test_invalid_email_and_name_rejected(self):
        api = Api()
        assert (
            api.post("/api/auth/register", register_body(email="not-an-email")).status_code == 422
        )
        assert api.post("/api/auth/register", register_body(full_name="   ")).status_code == 422

    def test_doctor_fields_required_and_year_validated(self):
        api = Api()
        assert api.post("/api/auth/register", register_body(role="doctor")).status_code == 422
        doctor = dict(role="doctor", reg_number="X1", council="NMC", specialization="GP")
        assert (
            api.post("/api/auth/register", register_body(reg_year=1949, **doctor)).status_code
            == 422
        )
        assert (
            api.post("/api/auth/register", register_body(reg_year=2999, **doctor)).status_code
            == 422
        )

    def test_future_date_of_birth_rejected(self):
        assert Api().post("/api/auth/register", register_body(dob="2999-01-01")).status_code == 422

    def test_password_is_stored_hashed(self, db):
        patient = new_patient()
        stored = db.scalar(select(User.password_hash).where(User.email == patient.email))
        assert stored != PASSWORD and stored.startswith("$argon2")

    def test_duplicate_council_and_registration_number_is_refused(self):
        first = new_doctor(reg_number="DUP-100", council="NMC")
        second = Api().post(
            "/api/auth/register",
            register_body(
                role="doctor",
                reg_number="DUP-100",
                council="NMC",
                reg_year=2016,
                specialization="GP",
            ),
        )
        assert second.status_code == 400 and second.json()["detail"] == "Could not create account"
        assert first.get("/api/doctor/verification").status_code == 200

    def test_registration_number_variant_is_flagged_as_duplicate_for_admin(self):
        new_doctor(reg_number="VAR-200-A", council="NMC")
        variant = new_doctor(reg_number="var 200 a", council="nmc")
        detail = new_admin().get(f"/api/admin/doctors/{variant.id}").json()
        assert detail["checks"][0]["duplicate_reg_flag"] is True


class TestLogin:
    def test_success_sets_hardened_cookie(self):
        patient = new_patient()
        response = Api().post("/api/auth/login", {"email": patient.email, "password": PASSWORD})
        assert response.status_code == 200
        cookie = next(
            h for h in response.headers.get_list("set-cookie") if h.startswith("access_token=")
        )
        lowered = cookie.lower()
        assert (
            "httponly" in lowered
            and "samesite=lax" in lowered
            and "path=/" in lowered
            and "max-age=" in lowered
        )

    def test_wrong_password_and_unknown_email_are_indistinguishable(self):
        patient = new_patient()
        wrong = Api().post("/api/auth/login", {"email": patient.email, "password": "Wrong1234567"})
        unknown = Api().post(
            "/api/auth/login", {"email": unique_email("nobody"), "password": "Wrong1234567"}
        )
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json() == {"detail": "Invalid email or password"}

    def test_email_login_is_case_insensitive(self):
        patient = new_patient()
        assert (
            Api()
            .post("/api/auth/login", {"email": patient.email.upper(), "password": PASSWORD})
            .status_code
            == 200
        )

    def test_disabled_account_cannot_log_in_or_use_existing_session(self, db):
        patient = new_patient()
        assert patient.get("/api/auth/me").status_code == 200
        db.query(User).filter(User.email == patient.email).update({"is_active": False})
        db.commit()
        assert patient.get("/api/auth/me").status_code == 403  # the live session stops immediately
        response = Api().post("/api/auth/login", {"email": patient.email, "password": PASSWORD})
        assert response.status_code == 403 and response.json()["detail"] == "Account disabled"

    def test_failed_and_successful_logins_are_audited_without_the_password(self, db):
        patient = new_patient()
        Api().post("/api/auth/login", {"email": patient.email, "password": "Wrong1234567"})
        Api().post("/api/auth/login", {"email": patient.email, "password": PASSWORD})
        rows = db.scalars(select(AuditLog).where(AuditLog.resource_id == patient.id)).all()
        actions = {r.action for r in rows}
        assert {"auth.login.failure", "auth.login.success"} <= actions
        assert "Wrong1234567" not in str([r.metadata_ for r in rows]) and PASSWORD not in str(
            [r.metadata_ for r in rows]
        )

    def test_login_rate_limit_per_ip(self):
        limiter.enabled = True
        limiter.reset()
        api = Api()
        codes = [
            api.post(
                "/api/auth/login", {"email": unique_email("x"), "password": "Wrong1234567"}
            ).status_code
            for _ in range(7)
        ]
        assert codes[:5] == [401] * 5 and codes[5] == 429
        assert (
            api.post("/api/auth/login", {"email": "a@b.co", "password": "x"})
            .json()["detail"]
            .startswith("Too many")
        )

    def test_login_rate_limit_per_email(self):
        login_email_limiter.enabled = True
        login_email_limiter.reset()
        target = unique_email("victim")
        codes = [
            Api().post("/api/auth/login", {"email": target, "password": "Wrong1234567"}).status_code
            for _ in range(7)
        ]
        assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]

    def test_register_rate_limit(self):
        limiter.enabled = True
        limiter.reset()
        api = Api()
        codes = [api.post("/api/auth/register", register_body()).status_code for _ in range(6)]
        assert codes[:5] == [201] * 5 and codes[5] == 429

    def test_email_limiter_window_expires(self):
        now = [0.0]
        limiter_ = EmailRateLimiter(limit=2, window_seconds=60, clock=lambda: now[0])
        assert limiter_.allow("a") and limiter_.allow("a") and not limiter_.allow("a")
        assert limiter_.allow("b")
        now[0] = 61
        assert limiter_.allow("a")


class TestSessionAndCsrf:
    def test_me_requires_authentication(self):
        assert Api().get("/api/auth/me").status_code == 401

    def test_me_reports_doctor_status(self):
        doctor = new_doctor()
        body = doctor.get("/api/auth/me").json()
        assert body["role"] == "doctor" and body["doctor_status"] == "pending"
        assert "password" not in str(body).lower()

    def test_logout_clears_cookie_and_session_is_gone(self):
        patient = new_patient()
        response = patient.post("/api/auth/logout")
        assert response.status_code == 200
        cookie = next(
            h for h in response.headers.get_list("set-cookie") if h.startswith("access_token=")
        )
        assert 'access_token=""' in cookie or "max-age=0" in cookie.lower()
        assert patient.get("/api/auth/me").status_code == 401

    def test_tampered_token_is_rejected(self):
        patient = new_patient()
        patient.http.cookies.set("access_token", patient.http.cookies.get("access_token") + "x")
        assert patient.get("/api/auth/me").status_code == 401

    def test_csrf_missing_or_wrong_is_rejected_on_mutating_routes(self):
        patient = new_patient()
        patient.http.get("/")
        no_header = patient.http.post("/api/auth/logout")
        assert no_header.status_code == 403
        wrong = patient.http.post("/api/auth/logout", headers={"X-CSRF-Token": "forged.value"})
        assert wrong.status_code == 403
        cookie = patient.http.cookies.get("csrf_token")
        altered = cookie[:-1] + (
            "a" if cookie[-1] != "a" else "b"
        )  # always differs from the cookie
        mismatch = patient.http.post("/api/auth/logout", headers={"X-CSRF-Token": altered})
        assert mismatch.status_code == 403

    def test_csrf_header_must_match_a_server_signed_token(self):
        patient = new_patient()
        forged = "a" * 64 + "." + "b" * 64
        patient.http.cookies.set("csrf_token", forged)
        assert (
            patient.http.post("/api/auth/logout", headers={"X-CSRF-Token": forged}).status_code
            == 403
        )

    def test_every_mutating_route_requires_csrf(self):
        patient = new_patient()
        patient.http.get("/")
        for method, url in (
            ("post", "/api/consents"),
            ("patch", "/api/me/profile"),
            ("post", "/api/appointments"),
            ("delete", "/api/consents/" + str(uuid.uuid4())),
            ("post", "/api/reports"),
        ):
            response = patient.http.request(method.upper(), url, json={})
            assert response.status_code == 403, (method, url)
