"""Shared fixtures: an isolated database, local storage, CSRF-aware API client and helpers.

By default the suite runs on a throw-away SQLite file. Set TEST_DATABASE_URL to a PostgreSQL URL
to run the same tests against Postgres (native enums, JSONB, partial indexes, append-only
triggers); the schema in that database is dropped and rebuilt from the Alembic migrations.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

_workdir = tempfile.TemporaryDirectory(prefix="clinicalscribe-tests-")
_database_url = (
    os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{Path(_workdir.name, 'test.db').as_posix()}"
)
os.environ.update(
    {
        "DATABASE_URL": _database_url,
        "ENV": "development",
        "SECRET_KEY": "test-secret-key-0123456789-abcdefghijklmnop",
        "STT_PROVIDER": "mock",
        "VOICE_AGENT_PROVIDER": "none",
        "STORAGE_BACKEND": "local",
        "LOCAL_STORAGE_DIR": str(Path(_workdir.name, "storage")),
        "LLM_PROVIDER": "mock",
        "LLM_API_KEY": "",
        "ELEVENLABS_API_KEY": "",
        "MAX_AUDIO_MB": "25",
        "DELETE_AUDIO_AFTER_TRANSCRIPTION": "false",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_REGION": "ap-south-1",
        "S3_BUCKET_NAME": "clinicalscribe-test-bucket",
    }
)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.models.registry import RegistryRecord  # noqa: E402
from app.models.user import User  # noqa: E402
from app.rate_limit import limiter, login_email_limiter  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services.jobs import set_inline_mode  # noqa: E402
from app.services.speech import MockSTT  # noqa: E402

PASSWORD = "Passw0rdTest"
ADMIN_PASSWORD = "AdminPassw0rd123"

# Smallest valid files per type (magic bytes are what the validator sniffs).
PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)
JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    + b"\x00" * 32
    + b"\xff\xd9"
)
WEBM_BYTES = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
MP3_BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    """Build the schema from the real Alembic migrations on a fresh database."""
    if _database_url.startswith("postgresql"):
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    with SessionLocal() as db:
        for number, council, name in (
            ("MH-2015-12345", "Maharashtra Medical Council", "Dr. Priya Sharma"),
            ("GJ-2018-67890", "Gujarat Medical Council", "Dr. Arjun Patel"),
        ):
            db.add(
                RegistryRecord(
                    reg_number=number,
                    council=council,
                    full_name=name,
                    reg_year=2015,
                    is_active=True,
                )
            )
        db.commit()
    yield
    engine.dispose()
    _workdir.cleanup()


@pytest.fixture(autouse=True)
def isolate_runtime():
    """Per-test defaults: no rate limiting, background jobs run inline."""
    limiter.enabled = False
    login_email_limiter.enabled = False
    login_email_limiter.reset()
    set_inline_mode(True)
    yield
    set_inline_mode(False)
    limiter.enabled = True
    login_email_limiter.enabled = True


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


class Api:
    """TestClient wrapper that behaves like the browser: keeps cookies and sends the CSRF header."""

    def __init__(self):
        self.http = TestClient(app, raise_server_exceptions=True)
        self.user: dict | None = None
        self.email: str | None = None

    def _csrf(self) -> dict:
        if not self.http.cookies.get("csrf_token"):
            self.http.get("/")
        return {"X-CSRF-Token": self.http.cookies.get("csrf_token", "")}

    def get(self, url, **kw):
        return self.http.get(url, **kw)

    def post(self, url, json=None, **kw):
        return self.http.post(url, json=json, headers=kw.pop("headers", None) or self._csrf(), **kw)

    def put(self, url, json=None, **kw):
        return self.http.put(url, json=json, headers=self._csrf(), **kw)

    def patch(self, url, json=None, **kw):
        return self.http.patch(url, json=json, headers=self._csrf(), **kw)

    def delete(self, url, **kw):
        return self.http.delete(url, headers=self._csrf(), **kw)

    def upload(self, url, filename, data, content_type, field="file"):
        return self.http.post(
            url, files={field: (filename, data, content_type)}, headers=self._csrf()
        )

    def login(self, email, password=PASSWORD):
        response = self.post("/api/auth/login", {"email": email, "password": password})
        assert response.status_code == 200, response.text
        self.email, self.user = email, response.json()
        return response

    @property
    def id(self) -> str:
        return self.user["id"]


def unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}@example.com"


def new_patient(allergies: str | None = None, name: str = "Test Patient") -> Api:
    api = Api()
    email = unique_email("patient")
    body = {
        "email": email,
        "password": PASSWORD,
        "full_name": name,
        "role": "patient",
        "dob": "1990-05-15",
        "gender": "male",
    }
    if allergies is not None:
        body["allergies"] = allergies
    response = api.post("/api/auth/register", body)
    assert response.status_code == 201, response.text
    api.email, api.user = email, response.json()
    return api


def new_doctor(
    name: str = "Dr. Test Doctor",
    reg_number: str | None = None,
    council: str = "Maharashtra Medical Council",
) -> Api:
    api = Api()
    email = unique_email("doctor")
    response = api.post(
        "/api/auth/register",
        {
            "email": email,
            "password": PASSWORD,
            "full_name": name,
            "role": "doctor",
            "reg_number": reg_number or f"REG-{uuid.uuid4().hex[:8].upper()}",
            "council": council,
            "reg_year": 2015,
            "specialization": "General Medicine",
            "clinic_name": "Test Clinic",
            "clinic_address": "1 Test Road, Mumbai",
            "clinic_phone": "+91-22-0000000",
        },
    )
    assert response.status_code == 201, response.text
    api.email, api.user = email, response.json()
    return api


def registry_record(name: str, council: str = "Maharashtra Medical Council") -> str:
    """Add a fresh fake registry record and return its (unique) registration number."""
    number = f"REG-{uuid.uuid4().hex[:8].upper()}"
    with SessionLocal() as db:
        db.add(
            RegistryRecord(
                reg_number=number, council=council, full_name=name, reg_year=2015, is_active=True
            )
        )
        db.commit()
    return number


def new_admin() -> Api:
    email = unique_email("admin")
    with SessionLocal() as db:
        db.add(
            User(
                email=email,
                password_hash=hash_password(ADMIN_PASSWORD),
                role=UserRole.admin,
                full_name="Test Admin",
            )
        )
        db.commit()
    api = Api()
    api.login(email, ADMIN_PASSWORD)
    return api


def verified_doctor(admin: Api, **kwargs) -> Api:
    """Register a doctor, upload a license and have the admin approve (the real flow)."""
    doctor = new_doctor(**kwargs)
    assert (
        doctor.upload(
            "/api/doctor/license", "license.pdf", PDF_BYTES, "application/pdf"
        ).status_code
        == 200
    )
    assert admin.post(f"/api/admin/doctors/{doctor.id}/approve").status_code == 200
    return doctor


def grant_consent(patient: Api, doctor: Api) -> str:
    response = patient.post("/api/consents", {"doctor_id": doctor.id})
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture
def admin() -> Api:
    return new_admin()


@pytest.fixture
def doctor(admin) -> Api:
    return verified_doctor(admin)


@pytest.fixture
def patient() -> Api:
    return new_patient()


@pytest.fixture
def consulting(admin):
    """A verified doctor, a consenting patient and an empty consult: the start of the workflow."""
    doc = verified_doctor(admin)
    pat = new_patient()
    consent_id = grant_consent(pat, doc)
    response = doc.post("/api/consults", {"patient_id": pat.id})
    assert response.status_code == 201, response.text
    return type(
        "Setup",
        (),
        {
            "doctor": doc,
            "patient": pat,
            "admin": admin,
            "consult_id": response.json()["id"],
            "consent_id": consent_id,
        },
    )


def run_to_prescription(setup, transcript: str | None = None) -> dict:
    """Drive a consult through transcript -> SOAP -> approve and return the draft prescription.

    The default transcript is the mock STT sample the mock LLM's canned prescription matches.
    """
    doc, cid = setup.doctor, setup.consult_id
    text_ = transcript or MockSTT.MOCK_TRANSCRIPT
    assert doc.put(f"/api/consults/{cid}/transcript", {"transcript_text": text_}).status_code == 200
    assert doc.post(f"/api/consults/{cid}/soap/generate").status_code == 200
    assert doc.get(f"/api/consults/{cid}").json()["status"] == "soap_ready"
    assert doc.post(f"/api/consults/{cid}/soap/approve").status_code == 200
    consult = doc.get(f"/api/consults/{cid}").json()
    assert consult["status"] == "prescription_ready", consult
    return doc.get(f"/api/prescriptions/{consult['latest_prescription']['id']}").json()
