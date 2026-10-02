"""Upload validation, S3 key scheme, bounded reads, body limits and presigned access (moto)."""

from __future__ import annotations

import re
import uuid
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from fastapi import HTTPException
from moto import mock_aws
from sqlalchemy import select

from app.config import get_settings
from app.models.file import File
from app.models.report import AuditLog
from app.services.storage import (
    LocalStorageService,
    StorageService,
    build_prescription_audio_key,
    build_prescription_key,
    build_s3_key,
    sniff_kind,
    validate_upload,
)
from app.services.uploads import read_validated_upload
from tests.conftest import (
    JPEG_BYTES,
    MP3_BYTES,
    PDF_BYTES,
    PNG_BYTES,
    WEBM_BYTES,
    new_doctor,
    run_to_prescription,
)

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 20
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 20
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 24
OGG = b"OggS\x00\x02" + b"\x00" * 30


class TestValidation:
    @pytest.mark.parametrize(
        "category,name,mime,data",
        [
            ("profile_photo", "a.jpg", "image/jpeg", JPEG_BYTES),
            ("profile_photo", "a.JPEG", "image/jpeg", JPEG_BYTES),
            ("profile_photo", "a.png", "image/png", PNG_BYTES),
            ("profile_photo", "a.webp", "image/webp", WEBP),
            ("license_certificate", "l.pdf", "application/pdf", PDF_BYTES),
            ("license_certificate", "l.png", "image/png", PNG_BYTES),
            ("license_certificate", "l.jpg", "image/jpeg", JPEG_BYTES),
            ("consult_audio", "c.webm", "audio/webm", WEBM_BYTES),
            ("consult_audio", "c.webm", "audio/webm;codecs=opus", WEBM_BYTES),
            ("consult_audio", "c.mp3", "audio/mpeg", MP3_BYTES),
            ("consult_audio", "c.mp3", "audio/mpeg", b"\xff\xfb\x90\x00" + b"\x00" * 30),
            ("consult_audio", "c.wav", "audio/wav", WAV),
            ("consult_audio", "c.m4a", "audio/mp4", M4A),
            ("consult_audio", "c.ogg", "audio/ogg", OGG),
        ],
    )
    def test_valid_files_are_accepted(self, category, name, mime, data):
        assert validate_upload(data, name, mime, category) == (True, "")

    @pytest.mark.parametrize(
        "category,name,mime,data,why",
        [
            ("license_certificate", "l.pdf", "image/png", PDF_BYTES, "wrong declared MIME"),
            ("license_certificate", "l.pdf", "application/pdf", PNG_BYTES, "PNG bytes in a .pdf"),
            (
                "license_certificate",
                "evil.pdf",
                "application/pdf",
                b"MZ\x90\x00" + b"\x00" * 30,
                "exe renamed to pdf",
            ),
            ("license_certificate", "l.exe", "application/pdf", PDF_BYTES, "disallowed extension"),
            ("license_certificate", "l", "application/pdf", PDF_BYTES, "no extension"),
            ("license_certificate", "l.pdf.exe", "application/pdf", PDF_BYTES, "double extension"),
            ("license_certificate", "l.pdf", "application/pdf", b"", "empty file"),
            ("profile_photo", "p.png", "image/png", JPEG_BYTES, "JPEG bytes in a .png"),
            ("profile_photo", "p.jpg", "image/jpeg", PNG_BYTES, "PNG bytes in a .jpg"),
            ("profile_photo", "p.webp", "image/webp", WAV, "WAV masquerading as WEBP"),
            ("profile_photo", "p.gif", "image/gif", b"GIF89a" + b"\x00" * 20, "unsupported type"),
            (
                "consult_audio",
                "c.webm",
                "audio/webm",
                b"\x00" * 40,
                "audio with no recognisable header",
            ),
            ("consult_audio", "c.mp3", "audio/mpeg", PDF_BYTES, "pdf bytes in an mp3"),
            ("consult_audio", "c.wav", "audio/wav", WEBP, "WEBP bytes in a wav"),
            ("consult_audio", "c.webm", "text/plain", WEBM_BYTES, "wrong MIME"),
            ("consult_audio", "c.avi", "video/avi", b"RIFF" + b"\x00" * 30, "unsupported video"),
        ],
    )
    def test_bad_files_are_rejected(self, category, name, mime, data, why):
        ok, error = validate_upload(data, name, mime, category)
        assert not ok and error, why

    def test_size_limits_per_category(self, monkeypatch):
        assert not validate_upload(
            PNG_BYTES + b"0" * (5 * 1024 * 1024), "p.png", "image/png", "profile_photo"
        )[0]
        assert validate_upload(
            PDF_BYTES + b"0" * (9 * 1024 * 1024), "l.pdf", "application/pdf", "license_certificate"
        )[0]
        assert not validate_upload(
            PDF_BYTES + b"0" * (10 * 1024 * 1024), "l.pdf", "application/pdf", "license_certificate"
        )[0]
        monkeypatch.setattr(get_settings(), "MAX_AUDIO_MB", 1)
        assert not validate_upload(
            WEBM_BYTES + b"0" * (1024 * 1024), "c.webm", "audio/webm", "consult_audio"
        )[0]
        assert validate_upload(WEBM_BYTES + b"0" * 1000, "c.webm", "audio/webm", "consult_audio")[0]

    def test_sniffing_recognises_formats(self):
        assert [
            sniff_kind(d)
            for d in (PDF_BYTES, PNG_BYTES, JPEG_BYTES, WEBP, WAV, MP3_BYTES, M4A, WEBM_BYTES, OGG)
        ] == ["pdf", "png", "jpeg", "webp", "wav", "mp3", "mp4", "webm", "ogg"]
        assert sniff_kind(b"hello world") is None

    def test_reads_are_bounded_and_stop_at_the_limit(self):
        class Endless:
            def __init__(self):
                self.read_bytes = 0

            def read(self, size=-1):
                self.read_bytes += size
                return b"A" * size

        source = Endless()
        upload = type("U", (), {"file": source, "filename": "p.png", "content_type": "image/png"})
        with pytest.raises(HTTPException) as caught:
            read_validated_upload(upload, "profile_photo")
        assert caught.value.status_code == 400 and "too large" in caught.value.detail
        assert source.read_bytes <= 6 * 1024 * 1024  # never loaded "unlimited" data into memory

    def test_request_bodies_over_the_limit_are_rejected_before_parsing(self, doctor):
        doctor.http.get("/")
        headers = {
            "X-CSRF-Token": doctor.http.cookies.get("csrf_token"),
            "Content-Type": "application/json",
        }
        too_big_json = doctor.http.post(
            "/api/auth/logout", content=b"{" + b" " * (3 * 1024 * 1024) + b"}", headers=headers
        )
        assert (
            too_big_json.status_code == 413
            and too_big_json.json()["detail"] == "Request body too large"
        )
        big_upload = doctor.http.post(
            "/api/doctor/license",
            headers={"X-CSRF-Token": headers["X-CSRF-Token"]},
            files={"file": ("l.pdf", b"%PDF-" + b"0" * (27 * 1024 * 1024), "application/pdf")},
        )
        assert big_upload.status_code == 413


class TestUploadEndpoints:
    def test_invalid_uploads_get_400_and_valid_ones_200(self, doctor):
        assert (
            doctor.upload("/api/doctor/license", "l.pdf", PNG_BYTES, "application/pdf").status_code
            == 400
        )
        assert (
            doctor.upload(
                "/api/doctor/license", "l.pdf", b"MZ" + b"0" * 50, "application/pdf"
            ).status_code
            == 400
        )
        assert doctor.upload("/api/me/photo", "p.png", JPEG_BYTES, "image/png").status_code == 400
        assert (
            doctor.upload("/api/doctor/license", "l.pdf", PDF_BYTES, "application/pdf").status_code
            == 200
        )
        assert doctor.upload("/api/me/photo", "p.jpg", JPEG_BYTES, "image/jpeg").status_code == 200

    def test_oversize_photo_is_rejected(self, patient):
        big = PNG_BYTES + b"0" * (5 * 1024 * 1024 + 10)
        response = patient.upload("/api/me/photo", "big.png", big, "image/png")
        assert response.status_code == 400 and "too large" in response.json()["detail"]

    def test_bad_audio_magic_bytes_are_rejected_at_the_endpoint(self, consulting):
        response = consulting.doctor.upload(
            f"/api/consults/{consulting.consult_id}/audio", "x.webm", b"\x00" * 50, "audio/webm"
        )
        assert response.status_code == 400

    def test_keys_follow_the_scheme_and_never_contain_the_client_filename(
        self, doctor, patient, db
    ):
        license_id = doctor.upload(
            "/api/doctor/license", "my secret ../name.pdf", PDF_BYTES, "application/pdf"
        ).json()["file_id"]
        photo_id = patient.upload(
            "/api/me/photo", "selfie-of-me.PNG", PNG_BYTES, "image/png"
        ).json()["file_id"]
        license_row, photo_row = db.get(File, uuid.UUID(license_id)), db.get(
            File, uuid.UUID(photo_id)
        )
        assert re.fullmatch(
            rf"doctor/{doctor.id}/license-certificate/[0-9a-f-]{{36}}\.pdf", license_row.s3_key
        )
        assert re.fullmatch(
            rf"patient/{patient.id}/profile-photo/[0-9a-f-]{{36}}\.png", photo_row.s3_key
        )
        assert "secret" not in license_row.s3_key and "selfie" not in photo_row.s3_key
        assert license_row.original_filename == "my secret ../name.pdf"  # display only
        import hashlib

        assert license_row.sha256 == hashlib.sha256(
            PDF_BYTES
        ).hexdigest() and license_row.size_bytes == len(PDF_BYTES)
        assert license_row.content_type == "application/pdf"

    def test_prescription_keys_follow_the_scheme(self, consulting, db):
        draft = run_to_prescription(consulting)
        row = db.get(File, uuid.UUID(draft["docx_file_id"]))
        assert re.fullmatch(
            rf"doctor/{consulting.doctor.id}/prescriptions/{consulting.consult_id}/v1-[0-9a-f-]{{36}}\.docx",
            row.s3_key,
        )
        approved = consulting.doctor.post(
            f"/api/prescriptions/{draft['id']}/approve",
            {
                "acknowledged_flag_ids": [
                    f["id"] for f in draft["safety_flags"] if f["severity"] == "high"
                ]
            },
        ).json()
        db.expire_all()
        assert re.search(
            r"/v1a-[0-9a-f-]{36}\.docx$", db.get(File, uuid.UUID(approved["docx_file_id"])).s3_key
        )

    def test_key_builders(self):
        assert build_s3_key("doctor", "u1", "consult_audio", ".webm").startswith(
            "doctor/u1/consult-audio/"
        )
        assert build_prescription_key("d", "c", 2).startswith("doctor/d/prescriptions/c/v2-")
        assert (
            build_prescription_audio_key("p", "rx", 3) == "patient/p/prescription-audio/rx-v3.mp3"
        )


class TestLocalStorage:
    def test_round_trip_and_traversal_is_blocked(self):
        storage = LocalStorageService()
        key = f"test/{uuid.uuid4()}/file.bin"
        storage.upload_bytes(key, b"data", "application/octet-stream")
        assert storage.exists(key) and storage.download_bytes(key) == b"data"
        storage.delete(key)
        assert not storage.exists(key)
        with pytest.raises(ValueError):
            storage.exists("../../etc/passwd")


@pytest.fixture
def s3(monkeypatch):
    """moto S3 with a private bucket; the app is switched to the real boto3 storage service."""
    settings = get_settings()
    monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
    with mock_aws():
        client = boto3.client("s3", region_name=settings.AWS_REGION)
        client.create_bucket(
            Bucket=settings.S3_BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": settings.AWS_REGION},
        )
        yield client


class TestS3:
    def test_storage_service_operations(self, s3):
        storage = StorageService()
        key = "doctor/x/license-certificate/abc.pdf"
        storage.upload_bytes(key, PDF_BYTES, "application/pdf")
        assert storage.exists(key) and storage.download_bytes(key) == PDF_BYTES
        head = s3.head_object(Bucket=get_settings().S3_BUCKET_NAME, Key=key)
        assert head["ServerSideEncryption"] == "AES256" and head["ContentType"] == "application/pdf"
        url = storage.presigned_get_url(
            key, expires_seconds=120, download_filename="Prescription-X-2026.docx"
        )
        query = parse_qs(urlparse(url).query)
        assert query["X-Amz-Expires"] == ["120"] and "X-Amz-Signature" in query
        assert "Prescription-X-2026.docx" in query["response-content-disposition"][0]
        storage.delete(key)
        assert not storage.exists(key)

    def test_signature_version_and_region_are_configured(self, s3):
        storage = StorageService()
        assert storage._client.meta.config.signature_version == "s3v4"
        assert storage._client.meta.region_name == get_settings().AWS_REGION

    def test_files_endpoint_redirects_to_a_presigned_url_only_after_authorization(
        self, s3, doctor, db
    ):
        file_id = doctor.upload(
            "/api/doctor/license", "l.pdf", PDF_BYTES, "application/pdf"
        ).json()["file_id"]
        stranger = new_doctor()
        denied = stranger.get(f"/api/files/{file_id}", follow_redirects=False)
        assert denied.status_code == 403 and "location" not in denied.headers
        ok = doctor.get(f"/api/files/{file_id}", follow_redirects=False)
        assert ok.status_code == 302
        location = ok.headers["location"]
        expires = int(parse_qs(urlparse(location).query)["X-Amz-Expires"][0])
        assert expires == get_settings().PRESIGNED_URL_EXPIRY_SECONDS
        assert ".amazonaws.com" in location or "s3" in location
        rows = db.scalars(
            select(AuditLog).where(
                AuditLog.action == "file.access", AuditLog.resource_id == file_id
            )
        ).all()
        assert len(rows) == 1 and rows[0].actor_id == uuid.UUID(
            doctor.id
        )  # only the allowed access is logged

    def test_prescription_download_filename_and_patient_rules_over_s3(self, s3, consulting):
        draft = run_to_prescription(consulting)
        assert (
            consulting.patient.get(
                f"/api/files/{draft['docx_file_id']}", follow_redirects=False
            ).status_code
            == 403
        )
        approved = consulting.doctor.post(
            f"/api/prescriptions/{draft['id']}/approve",
            {
                "acknowledged_flag_ids": [
                    f["id"] for f in draft["safety_flags"] if f["severity"] == "high"
                ]
            },
        ).json()
        response = consulting.patient.get(
            f"/api/files/{approved['docx_file_id']}", follow_redirects=False
        )
        assert response.status_code == 302
        disposition = parse_qs(urlparse(response.headers["location"]).query)[
            "response-content-disposition"
        ][0]
        assert re.search(r"Prescription-Test-Patient-\d{4}-\d{2}-\d{2}\.docx", disposition)
        stored = s3.get_object(
            Bucket=get_settings().S3_BUCKET_NAME, Key=_key_for(approved["docx_file_id"])
        )
        assert stored["ServerSideEncryption"] == "AES256"

    def test_storage_outage_gives_a_clean_503(self, monkeypatch, doctor):
        monkeypatch.setattr(get_settings(), "STORAGE_BACKEND", "s3")
        monkeypatch.setattr(get_settings(), "S3_BUCKET_NAME", "bucket-that-does-not-exist")
        with mock_aws():
            response = doctor.upload("/api/doctor/license", "l.pdf", PDF_BYTES, "application/pdf")
        assert response.status_code == 503 and "unavailable" in response.json()["detail"]
        assert (
            "bucket-that-does-not-exist" not in response.text and "Traceback" not in response.text
        )


def _key_for(file_id: str) -> str:
    from app.db import SessionLocal

    with SessionLocal() as session:
        return session.get(File, uuid.UUID(file_id)).s3_key
