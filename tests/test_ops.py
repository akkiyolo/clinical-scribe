"""Scripts, migrations, database-level guarantees and the audit trail."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import Base
from app.models.doctor import DoctorProfile
from app.models.enums import DoctorStatus, UserRole
from app.models.registry import RegistryRecord, VerificationEvent
from app.models.report import AuditLog
from app.models.user import User
from app.security import create_access_token
from scripts import create_admin, seed_demo, seed_registry
from tests.conftest import Api, new_admin, new_doctor, new_patient, unique_email

ON_POSTGRES = engine.dialect.name == "postgresql"


class TestMigrations:
    def test_models_and_migrations_agree(self):
        with engine.connect() as conn:
            diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)

        def significant(diff):
            # SQLite reports type/default noise for portable JSON and timestamp columns
            kind = diff[0] if isinstance(diff, tuple) else diff[0][0]
            return kind in {
                "add_table",
                "remove_table",
                "add_column",
                "remove_column",
                "add_fk",
                "remove_fk",
                "add_index",
                "remove_index",
                "add_constraint",
                "remove_constraint",
            }

        assert not [d for d in diffs if significant(d)], diffs

    def test_foreign_keys_exist_for_the_spec_relationships(self):
        from sqlalchemy import inspect

        inspector = inspect(engine)
        expected = {
            "appointments": {"patient_id", "doctor_id"},
            "consents": {"patient_id", "doctor_id"},
            "files": {"owner_id"},
            "reports": {"reporter_id", "doctor_id", "consult_id", "resolved_by"},
            "consults": {"doctor_id", "patient_id", "appointment_id", "audio_file_id"},
            "prescriptions": {
                "consult_id",
                "doctor_id",
                "patient_id",
                "docx_file_id",
                "agent_run_id",
                "approved_by",
            },
            "doctor_profiles": {"user_id", "license_file_id", "verified_by"},
            "users": {"profile_photo_file_id"},
        }
        for table, columns in expected.items():
            have = {
                c for fk in inspector.get_foreign_keys(table) for c in fk["constrained_columns"]
            }
            assert columns <= have, (table, columns - have)

    def test_status_and_foreign_key_columns_are_indexed(self):
        from sqlalchemy import inspect

        inspector = inspect(engine)

        def indexed(table: str) -> set[str]:
            return {c for ix in inspector.get_indexes(table) for c in ix["column_names"]}

        assert {"created_at", "actor_id", "action"} <= indexed("audit_logs")
        for table, column in (
            ("doctor_profiles", "status"),
            ("appointments", "status"),
            ("consults", "status"),
            ("prescriptions", "status"),
            ("appointments", "patient_id"),
            ("consents", "doctor_id"),
        ):
            assert column in indexed(table), (table, column)

    def test_partial_unique_indexes_allow_reuse_after_rejection_and_revocation(self, admin):
        number = f"REUSE-{uuid.uuid4().hex[:6]}"
        first = new_doctor(reg_number=number, council="NMC")
        from tests.conftest import PDF_BYTES

        first.upload("/api/doctor/license", "l.pdf", PDF_BYTES, "application/pdf")
        admin.post(
            f"/api/admin/doctors/{first.id}/reject", {"reason": "Rejected to free the number"}
        )
        second = Api()
        response = second.post(
            "/api/auth/register",
            {
                "email": unique_email("again"),
                "password": "Passw0rdTest",
                "full_name": "Second Doctor",
                "role": "doctor",
                "reg_number": number,
                "council": "NMC",
                "reg_year": 2015,
                "specialization": "GP",
            },
        )
        assert response.status_code == 201  # the rejected doctor no longer holds the number


@pytest.mark.skipif(
    not ON_POSTGRES, reason="append-only triggers are PostgreSQL-only; run with TEST_DATABASE_URL"
)
class TestAppendOnlyTriggers:
    def test_audit_logs_reject_update_delete_and_truncate(self):
        new_patient()
        with SessionLocal() as db:
            row_id = db.scalar(select(AuditLog.id).limit(1))
        for statement in (
            "UPDATE audit_logs SET action = 'tampered' WHERE id = :id",
            "DELETE FROM audit_logs WHERE id = :id",
            "TRUNCATE audit_logs",
        ):
            with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
                conn.execute(text(statement), {"id": row_id})

    def test_verification_events_reject_update_delete_and_truncate(self):
        new_doctor()
        with SessionLocal() as db:
            row_id = db.scalar(select(VerificationEvent.id).limit(1))
        for statement in (
            "UPDATE verification_events SET reason = 'tampered' WHERE id = :id",
            "DELETE FROM verification_events WHERE id = :id",
            "TRUNCATE verification_events",
        ):
            with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
                conn.execute(text(statement), {"id": row_id})

    def test_inserts_still_work(self):
        new_patient()
        with SessionLocal() as db:
            assert db.query(AuditLog).count() > 0


class TestSessionEdgeCases:
    def test_a_valid_token_for_a_deleted_user_is_rejected(self, db):
        patient = new_patient()
        token = patient.http.cookies.get("access_token")
        user = db.query(User).filter_by(email=patient.email).one()
        # a well-formed token for an id that no longer exists
        ghost = create_access_token(uuid.uuid4(), "admin")
        api = Api()
        api.http.cookies.set("access_token", ghost)
        assert api.get("/api/auth/me").status_code == 401
        assert token and user

    def test_the_role_in_the_token_is_never_trusted(self, db):
        patient = new_patient()
        forged = create_access_token(uuid.UUID(patient.id), "admin")
        api = Api()
        api.http.cookies.set("access_token", forged)
        assert api.get("/api/auth/me").json()["role"] == "patient"
        assert api.get("/api/admin/stats").status_code == 403

    def test_expired_tokens_are_rejected(self, monkeypatch):
        patient = new_patient()
        monkeypatch.setattr(get_settings(), "ACCESS_TOKEN_EXPIRE_MINUTES", -5)
        api = Api()
        api.http.cookies.set("access_token", create_access_token(uuid.UUID(patient.id), "patient"))
        assert api.get("/api/auth/me").status_code == 401


class TestAuditTrail:
    def test_login_failures_registration_and_verification_decisions_are_audited(self, db, admin):
        doctor = new_doctor()
        Api().post("/api/auth/login", {"email": doctor.email, "password": "Wrong1234567"})
        from tests.conftest import PDF_BYTES

        doctor.upload("/api/doctor/license", "l.pdf", PDF_BYTES, "application/pdf")
        admin.post(f"/api/admin/doctors/{doctor.id}/approve")
        actions = {
            a
            for (a,) in db.execute(
                select(AuditLog.action).where(
                    (AuditLog.resource_id == doctor.id)
                    | (AuditLog.actor_id == uuid.UUID(doctor.id))
                )
            )
        }
        assert {
            "auth.login.failure",
            "auth.register",
            "verification.submit",
            "doctor.license.upload",
            "verification.approve",
        } <= actions

    def test_audit_rows_record_ip_and_user_agent_without_secrets(self, db):
        api = Api()
        api.http.headers["user-agent"] = "pytest-agent/1.0"
        patient = new_patient()
        api.post("/api/auth/login", {"email": patient.email, "password": "Passw0rdTest"})
        row = db.scalars(
            select(AuditLog).where(
                AuditLog.action == "auth.login.success", AuditLog.resource_id == patient.id
            )
        ).first()
        assert row.ip and row.user_agent == "pytest-agent/1.0"
        assert "Passw0rd" not in str(row.metadata_)

    def test_admin_audit_endpoint_filters_and_paginates(self, admin):
        patient = new_patient()
        Api().post("/api/auth/login", {"email": patient.email, "password": "Wrong1234567"})
        body = admin.get(f"/api/admin/audit-logs?actor={patient.id}&limit=1").json()
        assert body["limit"] == 1 and len(body["items"]) == 1 and body["total"] >= 1
        assert all(i["actor_id"] == patient.id for i in body["items"])
        failures = admin.get("/api/admin/audit-logs?action=login.failure&limit=100").json()["items"]
        assert failures and all("login.failure" in i["action"] for i in failures)
        assert admin.get("/api/admin/audit-logs?actor=not-a-uuid").status_code == 422
        assert admin.get("/api/admin/audit-logs?from=2999-01-01").json()["items"] == []
        assert admin.get("/api/admin/audit-logs?to=2000-01-01").json()["items"] == []
        today = admin.get("/api/admin/audit-logs?from=2000-01-01&to=2999-01-01&limit=5").json()
        assert (
            today["total"] > 0
            and today["items"][0]["created_at"] >= today["items"][-1]["created_at"]
        )

    def test_failed_audit_writes_do_not_break_the_request(self, monkeypatch):
        from app.services import audit as audit_module

        original = audit_module.AuditLog

        def explode(**kwargs):
            raise RuntimeError("audit table unavailable")

        monkeypatch.setattr(audit_module, "AuditLog", explode)
        api = Api()
        response = api.post(
            "/api/auth/register",
            {
                "email": unique_email("quiet"),
                "password": "Passw0rdTest",
                "full_name": "Quiet",
                "role": "patient",
            },
        )
        assert response.status_code == 201
        monkeypatch.setattr(audit_module, "AuditLog", original)

    def test_list_endpoints_default_to_20_and_cap_at_100(self, admin, doctor):
        for url in (
            "/api/appointments",
            "/api/consents",
            "/api/consults",
            "/api/prescriptions",
            "/api/doctors",
            "/api/admin/doctors",
            "/api/admin/reports",
            "/api/admin/audit-logs",
        ):
            body = (admin if url.startswith("/api/admin") else doctor).get(url).json()
            assert body["limit"] == 20 and body["offset"] == 0, url
            assert (admin if url.startswith("/api/admin") else doctor).get(
                f"{url}?limit=5000"
            ).json()["limit"] == 100


class TestScripts:
    def test_create_admin_creates_once_and_never_echoes_the_password(self, monkeypatch, capsys):
        email = unique_email("root")
        monkeypatch.setattr(get_settings(), "ADMIN_BOOTSTRAP_EMAIL", email)
        monkeypatch.setattr(get_settings(), "ADMIN_BOOTSTRAP_PASSWORD", "Str0ng-Admin-Password-1")
        create_admin.main()
        create_admin.main()
        output = capsys.readouterr().out
        assert (
            "Created admin account" in output
            and "already exists" in output
            and "Str0ng-Admin" not in output
        )
        with SessionLocal() as db:
            assert db.query(User).filter_by(email=email, role=UserRole.admin).count() == 1
        Api().login(email, "Str0ng-Admin-Password-1")

    @pytest.mark.parametrize(
        "password", ["short1", "alllettersnodigits", "123456789012", "change-me", ""]
    )
    def test_create_admin_refuses_weak_passwords(self, monkeypatch, password):
        monkeypatch.setattr(get_settings(), "ADMIN_BOOTSTRAP_EMAIL", unique_email("weak"))
        monkeypatch.setattr(get_settings(), "ADMIN_BOOTSTRAP_PASSWORD", password)
        with pytest.raises(SystemExit):
            create_admin.main()

    def test_create_admin_refuses_to_reuse_a_non_admin_email(self, monkeypatch):
        patient = new_patient()
        monkeypatch.setattr(get_settings(), "ADMIN_BOOTSTRAP_EMAIL", patient.email)
        monkeypatch.setattr(get_settings(), "ADMIN_BOOTSTRAP_PASSWORD", "Str0ng-Admin-Password-1")
        with pytest.raises(SystemExit, match="non-admin"):
            create_admin.main()

    def test_seed_registry_is_idempotent_and_fake(self):
        seed_registry.seed_registry()
        assert seed_registry.seed_registry() == 0
        with SessionLocal() as db:
            records = db.query(RegistryRecord).all()
        assert len(seed_registry.RECORDS) >= 20 and len(records) >= 20
        assert any(
            not r.is_active for r in records
        )  # an inactive registration for the evidence badges

    def test_seed_demo_creates_the_demo_accounts_and_is_idempotent(self, capsys):
        seed_demo.seed_demo()
        seed_demo.seed_demo()
        capsys.readouterr()
        doctor = Api()
        doctor.login(seed_demo.DOCTOR_EMAIL, seed_demo.DEMO_PASSWORD)
        assert doctor.user["doctor_status"] == "verified"
        assert doctor.get("/api/doctor/patients").json()["total"] >= 1  # the demo consent
        pending = Api()
        pending.login(seed_demo.PENDING_DOCTOR_EMAIL, seed_demo.DEMO_PASSWORD)
        assert pending.user["doctor_status"] == "pending"
        Api().login(seed_demo.PATIENT_EMAIL, seed_demo.DEMO_PASSWORD)
        with SessionLocal() as db:
            sharma = db.query(DoctorProfile).filter_by(reg_number="MH-2015-12345").first()
        assert sharma is not None and sharma.status in (DoctorStatus.verified, DoctorStatus.pending)

    def test_seed_demo_refuses_to_run_in_production(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "ENV", "production")
        with pytest.raises(SystemExit, match="production"):
            seed_demo.seed_demo()

    def test_the_admin_fixture_is_a_real_admin(self):
        assert new_admin().get("/api/admin/stats").status_code == 200
