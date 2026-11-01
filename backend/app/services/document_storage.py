"""Private S3-backed storage for customer compliance documents.

The sibling of `services/resume_storage`, and deliberately a separate module
rather than a parameter on it: a resume is a candidate artefact with its own
PDF/DOCX-only rule and its own folder, while a compliance record is a scan a
finance team produces — a photographed PAN card is a JPEG, and accepting one
must never widen what a resume upload will take.

Everything else mirrors resume_storage on purpose, because those behaviours
were chosen for good reasons and diverging would be a silent regression:

  * bytes never touch the application filesystem;
  * the object key is CONTENT-ADDRESSED (sha256), so a retry after a
    lost response resolves to the asset already stored instead of creating a
    second one;
  * a lost response is re-checked against the deterministic id before the
    upload is declared failed.

Never name the vendor in user-facing copy (claude.md, 2026-07-26): every
message below states the limits, not where the bytes land.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.services import object_storage

#: Scans and exports a finance team actually produces. DOCX is deliberately
#: absent — a compliance record is a signed/issued artefact, not an editable
#: document, and accepting one would invite an unsigned draft being filed as
#: the agreement.
ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
ALLOWED_DOCUMENT_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    # Some browsers send this for a file picked from a file manager.
    "application/octet-stream",
}
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
OBJECT_PREFIX = "compliance"

#: Extension to the content type this store records.
MIME_BY_EXTENSION = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

#: Human-readable limits, used in the API description and the UI hint so the
#: two cannot drift apart.
UPLOAD_LIMITS_HINT = "PDF, JPG or PNG, up to 10 MB."


@dataclass(frozen=True)
class StoredDocument:
    public_id: str
    secure_url: str
    original_filename: str
    mime_type: str
    size_bytes: int
    uploaded_at: datetime
    sha256: str


def _error(code: int, message: str, *, retryable: bool = False) -> HTTPException:
    return HTTPException(
        status_code=code, detail={"message": message, "retryable": retryable}
    )


def _normalise_filename(filename: str | None) -> str:
    name = os.path.basename((filename or "").strip())
    if not name or len(name) > 255:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Choose a document file with a valid filename.",
        )
    return name


def assert_signature(data: bytes, extension: str) -> None:
    """Check the magic bytes, not just the extension.

    PUBLIC because a candidate's certificate upload needs exactly this check
    and must not carry a second implementation of it: a renamed executable
    given a .pdf extension is the whole reason the check exists, and two
    copies means one of them eventually loses a format.

    A renamed file is the ordinary case here (someone exports a scan and types
    a filename), and filing a corrupt PAN card that only reveals itself when
    the Provider clicks View is worse than refusing it at upload.
    """
    if extension == ".pdf" and not data.startswith(b"%PDF-"):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The selected file is not a valid PDF.",
        )
    if extension in {".jpg", ".jpeg"} and not data.startswith(b"\xff\xd8\xff"):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The selected file is not a valid JPEG image.",
        )
    if extension == ".png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The selected file is not a valid PNG image.",
        )


async def read_validated_document(file: UploadFile) -> tuple[bytes, str, str]:
    """Read one upload into memory after strict format and size validation."""
    filename = _normalise_filename(file.filename)
    extension = os.path.splitext(filename.lower())[1]
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Compliance documents must be {UPLOAD_LIMITS_HINT}",
        )
    if content_type and content_type not in ALLOWED_DOCUMENT_CONTENT_TYPES:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The selected file type does not match a PDF, JPG or PNG document.",
        )

    data = await file.read(MAX_DOCUMENT_BYTES + 1)
    if not data:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The selected document is empty."
        )
    if len(data) > MAX_DOCUMENT_BYTES:
        raise _error(
            status.HTTP_413_CONTENT_TOO_LARGE,
            "Compliance documents must be 10 MB or smaller.",
        )
    assert_signature(data, extension)
    return data, filename, MIME_BY_EXTENSION[extension]


def _upload_or_get_existing(
    data: bytes, sha256: str, filename: str, mime_type: str
) -> dict[str, Any]:
    public_id = f"{OBJECT_PREFIX}/{sha256}"
    try:
        stored = object_storage.put_if_absent(
            key=public_id,
            data=data,
            content_type=mime_type,
            metadata={
                "original_filename": filename,
                "mime_type": mime_type,
                "sha256": sha256,
            },
        )
    except object_storage.ObjectStorageNotConfigured as exc:
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Document storage is not configured. Please try again shortly or contact support.",
            retryable=True,
        ) from exc
    except Exception as exc:
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "We could not securely store this document. Nothing was saved; "
            "please retry.",
            retryable=True,
        ) from exc
    return {
        "public_id": stored.key,
        "secure_url": stored.uri,
        "bytes": stored.size_bytes,
        "created_at": stored.created_at,
    }


def _download(public_id: str) -> bytes:
    try:
        return object_storage.get_bytes(public_id)
    except object_storage.ObjectStorageError as exc:
        raise _error(
            status.HTTP_502_BAD_GATEWAY,
            "The document could not be loaded.",
        ) from exc


async def fetch_document_bytes(public_id: str) -> bytes:
    return await run_in_threadpool(_download, public_id)


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(
            tzinfo=timezone.utc
        )
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    return datetime.now(timezone.utc)


async def store_document(file: UploadFile) -> StoredDocument:
    """Validate and persist a compliance document, returning DB-ready metadata."""
    data, filename, mime_type = await read_validated_document(file)
    sha256 = hashlib.sha256(data).hexdigest()
    result = await run_in_threadpool(
        _upload_or_get_existing, data, sha256, filename, mime_type
    )
    secure_url = result.get("secure_url")
    public_id = result.get("public_id")
    if not secure_url or not public_id:
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Document storage returned an incomplete response. Please retry.",
            retryable=True,
        )
    return StoredDocument(
        public_id=str(public_id),
        secure_url=str(secure_url),
        original_filename=filename,
        mime_type=mime_type,
        size_bytes=int(result.get("bytes") or len(data)),
        uploaded_at=_as_utc(result.get("created_at")),
        sha256=sha256,
    )
