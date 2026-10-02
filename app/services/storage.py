"""S3 file storage service using boto3."""

from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings

logger = logging.getLogger(__name__)


class StorageUnavailable(RuntimeError):
    """Storage backend failed; safe to report to API clients as a 503."""


class LocalStorageService:
    """Private filesystem storage for local development and fake-data testing."""

    is_local = True

    def __init__(self):
        configured = get_settings().LOCAL_STORAGE_DIR
        self._root = (
            Path(configured)
            if configured
            else Path(__file__).resolve().parents[2] / ".local_storage"
        )

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ValueError("Invalid storage key")
        return path

    def upload_bytes(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            logger.exception("Local file storage write failed")
            raise StorageUnavailable("Local file storage is unavailable.") from exc
        logger.info("Stored %s (%d bytes) locally", key, len(data))

    def download_bytes(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except OSError as exc:
            logger.exception("Local file storage read failed")
            raise StorageUnavailable("File storage is unavailable.") from exc

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as exc:
            logger.exception("Local file storage delete failed")
            raise StorageUnavailable("File storage is unavailable.") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


# ── Upload validation: extension + declared MIME + sniffed magic bytes must agree ─────────

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def sniff_kind(data: bytes) -> str | None:
    """Identify a file by its leading bytes. Returns None when the content is not recognised."""
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    return None


# category -> extension -> (sniffed kind, accepted declared MIME types)
UPLOAD_RULES: dict[str, dict[str, tuple[str, frozenset[str]]]] = {
    "profile_photo": {
        ".jpg": ("jpeg", frozenset({"image/jpeg"})),
        ".jpeg": ("jpeg", frozenset({"image/jpeg"})),
        ".png": ("png", frozenset({"image/png"})),
        ".webp": ("webp", frozenset({"image/webp"})),
    },
    "license_certificate": {
        ".pdf": ("pdf", frozenset({"application/pdf"})),
        ".jpg": ("jpeg", frozenset({"image/jpeg"})),
        ".jpeg": ("jpeg", frozenset({"image/jpeg"})),
        ".png": ("png", frozenset({"image/png"})),
    },
    "consult_audio": {
        ".mp3": ("mp3", frozenset({"audio/mpeg", "audio/mp3"})),
        ".wav": ("wav", frozenset({"audio/wav", "audio/x-wav", "audio/wave"})),
        ".m4a": ("mp4", frozenset({"audio/mp4", "audio/x-m4a", "audio/m4a"})),
        ".webm": ("webm", frozenset({"audio/webm", "video/webm"})),
        ".ogg": ("ogg", frozenset({"audio/ogg", "application/ogg"})),
    },
    "prescription_docx": {".docx": ("zip", frozenset({DOCX_MIME}))},
    "prescription_audio": {".mp3": ("mp3", frozenset({"audio/mpeg"}))},
}

MAX_BYTES = {
    "profile_photo": 5 * 1024 * 1024,
    "license_certificate": 10 * 1024 * 1024,
    "prescription_docx": 50 * 1024 * 1024,
    "prescription_audio": 50 * 1024 * 1024,
}


def max_upload_bytes(category: str) -> int:
    """Size limit for a category; audio follows MAX_AUDIO_MB."""
    if category == "consult_audio":
        return get_settings().max_audio_bytes
    return MAX_BYTES[category]


def validate_upload(
    data: bytes,
    filename: str,
    content_type: str,
    category: str,
) -> tuple[bool, str]:
    """Validate an upload: size, extension, declared MIME type and magic bytes must all agree.

    Returns (is_valid, error_message).
    """
    import os

    rules = UPLOAD_RULES.get(category)
    if not rules:
        return False, f"Unknown file category: {category}"

    limit = max_upload_bytes(category)
    if len(data) > limit:
        return False, f"File too large. Maximum size: {limit // (1024 * 1024)} MB"
    if not data:
        return False, "File is empty"

    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in rules:
        return False, f"File type not allowed. Accepted: {', '.join(sorted(rules))}"

    expected_kind, mime_types = rules[ext]
    declared = (content_type or "").lower().split(";")[0].strip()
    if declared not in mime_types:
        return False, f"Content type '{declared or 'unknown'}' does not match a {ext} file"

    if sniff_kind(data) != expected_kind:
        return False, "File content does not match its declared type"

    return True, ""


def compute_sha256(data: bytes) -> str:
    """Compute SHA-256 hash of file data."""
    return hashlib.sha256(data).hexdigest()


def extension_of(filename: str | None) -> str:
    """Lower-cased extension of a client filename (only ever used after validate_upload)."""
    import os

    return os.path.splitext(filename or "")[1].lower()


def build_s3_key(role: str, user_id: str, category: str, file_ext: str, extra: str = "") -> str:
    """Build S3 key following the scheme: {role}/{user_id}/{category}/{uuid4}.{ext}.

    The client filename is never part of a key.
    """
    file_id = str(uuid.uuid4())
    cat_slug = category.replace("_", "-")
    if extra:
        return f"{role}/{user_id}/{cat_slug}/{extra}-{file_id}{file_ext}"
    return f"{role}/{user_id}/{cat_slug}/{file_id}{file_ext}"


def build_prescription_key(
    doctor_id: str, consult_id: str, version: int, approved: bool = False
) -> str:
    """doctor/<id>/prescriptions/<consult_id>/v2-<uuid>.docx (approved copies get an 'a' marker)."""
    marker = f"v{version}a" if approved else f"v{version}"
    return f"doctor/{doctor_id}/prescriptions/{consult_id}/{marker}-{uuid.uuid4()}.docx"


def build_prescription_audio_key(patient_id: str, prescription_id: str, version: int) -> str:
    """Deterministic cache key for the TTS summary of one prescription version."""
    return f"patient/{patient_id}/prescription-audio/{prescription_id}-v{version}.mp3"


class StorageService:
    """Wrapper around boto3 S3 client."""

    def __init__(self):
        self.is_local = False
        settings = get_settings()
        self._client = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION,
            config=BotoConfig(signature_version="s3v4"),
        )
        self._bucket = settings.S3_BUCKET_NAME

    def upload_bytes(self, key: str, data: bytes, content_type: str) -> None:
        """Upload bytes to S3."""
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                ServerSideEncryption="AES256",
            )
        except (BotoCoreError, ClientError) as exc:
            self._raise_storage_error("upload", exc)
        logger.info("Uploaded %s (%d bytes) to S3", key, len(data))

    def download_bytes(self, key: str) -> bytes:
        """Download bytes from S3."""
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            self._raise_storage_error("download", exc)

    def delete(self, key: str) -> None:
        """Delete an object from S3."""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            self._raise_storage_error("delete", exc)
        logger.info("Deleted %s from S3", key)

    def exists(self, key: str) -> bool:
        """Check if an object exists in S3."""
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError:
            return False

    def presigned_get_url(
        self,
        key: str,
        expires_seconds: int | None = None,
        download_filename: str | None = None,
    ) -> str:
        """Generate a presigned GET URL for an S3 object."""
        settings = get_settings()
        if expires_seconds is None:
            expires_seconds = settings.PRESIGNED_URL_EXPIRY_SECONDS

        params: dict = {
            "Bucket": self._bucket,
            "Key": key,
        }
        if download_filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{download_filename}"'

        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=expires_seconds,
            )
        except (BotoCoreError, ClientError) as exc:
            self._raise_storage_error("create download link", exc)

    @staticmethod
    def _raise_storage_error(operation: str, exc: Exception) -> None:
        code = (
            exc.response.get("Error", {}).get("Code", "unknown")
            if isinstance(exc, ClientError)
            else type(exc).__name__
        )
        logger.error("S3 %s failed (code=%s)", operation, code)
        raise StorageUnavailable(
            "File storage is unavailable. Check the S3 credentials, bucket, and region."
        ) from exc


def get_storage_service() -> StorageService | LocalStorageService:
    """Factory for storage service."""
    if get_settings().STORAGE_BACKEND == "local":
        return LocalStorageService()
    return StorageService()
