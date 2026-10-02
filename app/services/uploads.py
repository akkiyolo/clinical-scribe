"""Shared upload handling: bounded read, validation, storage write and files-row creation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.models.enums import FileCategory
from app.models.file import File
from app.services.storage import (
    build_s3_key,
    compute_sha256,
    extension_of,
    get_storage_service,
    max_upload_bytes,
    validate_upload,
)

READ_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ValidatedUpload:
    data: bytes
    filename: str
    content_type: str


def read_validated_upload(file: UploadFile, category: str) -> ValidatedUpload:
    """Read an upload in bounded chunks and validate extension, MIME type and magic bytes.

    Reading stops as soon as the category limit is exceeded, so an oversized upload is never
    fully loaded into memory. Raises HTTP 400 on any rejection.
    """
    limit = max_upload_bytes(category)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = file.file.read(READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=400,
                detail=f"File too large. Maximum size: {limit // (1024 * 1024)} MB",
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    filename = file.filename or ""
    content_type = file.content_type or ""
    valid, error = validate_upload(data, filename, content_type, category)
    if not valid:
        raise HTTPException(status_code=400, detail=error)
    return ValidatedUpload(data=data, filename=filename, content_type=content_type)


def store_upload(
    db: Session,
    owner_id: uuid.UUID,
    role: str,
    category: FileCategory,
    upload: ValidatedUpload,
) -> File:
    """Write a validated upload to storage under a random key and add its files row."""
    key = build_s3_key(role, str(owner_id), category.value, extension_of(upload.filename))
    get_storage_service().upload_bytes(key, upload.data, upload.content_type)
    record = File(
        owner_id=owner_id,
        category=category,
        s3_key=key,
        original_filename=(upload.filename or "")[:255] or None,
        content_type=upload.content_type.split(";")[0].strip().lower() or None,
        size_bytes=len(upload.data),
        sha256=compute_sha256(upload.data),
    )
    db.add(record)
    db.flush()
    return record
