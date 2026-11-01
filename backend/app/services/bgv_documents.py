"""A candidate's legacy verification documents: enumerate, withdraw, erase.

STATUS: RETIRED FOR UPLOAD (2026-11, production-fix pass). The candidate
profile no longer collects academic certificates or address proof, and
`POST /bgv/me/documents` no longer exists. What remains is only what the
existing rows need:

* `list_documents` and `delete_document`, so a candidate who uploaded a file
  while the feature existed can still see it and withdraw it (API only; there
  is no screen);
* `candidate_document_keys` and `delete_candidate_documents`, so profile
  erasure reaches the BYTES.

No file is deleted by this retirement. What happens to the legacy files and
rows is a retention decision that belongs to the owner:
`docs/operations/BGV_LEGACY_DOCUMENTS.md` is the plan.

There is still no recruiter route to these documents and there never was one:
the product has no consent record for sharing a degree certificate with a
customer (`bgv_share_consents` covers inquiry results only). Employer-driven
background verification is a separate workflow and is untouched.

ERASURE IS THE WHOLE REASON `candidate_document_keys` IS SEPARATE
--------------------------------------------------------------------
Deleting the ROW deletes nothing in the object store: no foreign key reaches
an object, which is the same trap `context_chunks` and the video recordings
already taught this product. `candidate_document_keys` must be called BEFORE
the rows are erased, because after the cascade there is nothing left that
names the objects and an enumeration run afterwards returns an honest empty
tuple that a caller would read as a complete deletion of nothing.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.services import object_storage

#: What erasure records this object WAS. One kind, because a certificate and an
#: address proof are deleted identically and a distinction that changes no
#: behaviour is a distinction an operator has to decode for nothing.
KIND_BGV_DOCUMENT = "bgv_document"


@dataclass(frozen=True)
class StoredBGVDocument:
    """One stored document, as the row records it."""

    id: uuid.UUID
    document_type: str
    original_filename: str
    mime_type: str
    size_bytes: int
    uploaded_at: datetime


async def list_documents(
    session: AsyncSession, *, candidate_id: uuid.UUID
) -> list[StoredBGVDocument]:
    """This candidate's documents. NO object key crosses this boundary."""
    rows = (
        (
            await session.execute(
                text(
                    "SELECT id, document_type, original_filename, mime_type, "
                    " size_bytes, uploaded_at FROM candidate_bgv_documents "
                    "WHERE candidate_id = :cid "
                    "ORDER BY document_type, uploaded_at DESC, id"
                ),
                {"cid": str(candidate_id)},
            )
        )
        .mappings()
        .all()
    )
    return [
        StoredBGVDocument(
            id=uuid.UUID(str(row["id"])),
            document_type=row["document_type"],
            original_filename=row["original_filename"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            uploaded_at=row["uploaded_at"],
        )
        for row in rows
    ]


async def delete_document(
    session: AsyncSession, *, candidate_id: uuid.UUID, document_id: uuid.UUID
) -> bool:
    """Remove one document the candidate uploaded, bytes included.

    Scoped by candidate id IN THE STATEMENT, not checked beforehand: a delete
    that reads then writes is a delete somebody can race, and this runs in the
    bypass scope where a candidate id in the WHERE clause is the only boundary
    there is.

    THE ROW GOES FIRST AND THE BYTES SECOND, deliberately. The object is shared
    by content address, so another row may still reference it; the delete is
    attempted only when no row does.
    """
    key = (
        await session.execute(
            text(
                "DELETE FROM candidate_bgv_documents "
                "WHERE id = :id AND candidate_id = :cid RETURNING object_key"
            ),
            {"id": str(document_id), "cid": str(candidate_id)},
        )
    ).scalar()
    if key is None:
        return False
    await _drop_object_if_unreferenced(session, str(key))
    return True


class BGVDocumentNotDeleted(RuntimeError):
    """The row is gone and the bytes are still readable.

    `object_storage.delete` is best-effort and never raises, which is right on
    a rollback path and wrong here: a candidate's certificate reported as
    removed and still sitting in the store is the one outcome this module must
    never produce quietly. So the delete is followed by a HEAD, the same
    contract the project-intake pipeline holds itself to, and this is what an
    unconfirmed deletion looks like from the outside.
    """


async def _drop_object_if_unreferenced(session: AsyncSession, key: str) -> None:
    """Remove one object once no row names it, and CONFIRM it is gone.

    The key is a content address, so two candidates who uploaded byte-identical
    files share one object: erasing one person must not remove the other's
    document, and the reference check is what keeps that true.
    """
    still_referenced = (
        await session.execute(
            text(
                "SELECT 1 FROM candidate_bgv_documents WHERE object_key = :key "
                "LIMIT 1"
            ),
            {"key": key},
        )
    ).scalar()
    if still_referenced is not None:
        return
    await run_in_threadpool(object_storage.delete, key)
    # "The delete call returned" is the same class of non-evidence as "the
    # pipeline was green" (Project Evidence brief section 27). Ask.
    if await run_in_threadpool(object_storage.exists, key):
        raise BGVDocumentNotDeleted(
            f"object {key} is still readable after a delete was issued"
        )


# ── The erasure hook ─────────────────────────────────────────────────────────


async def candidate_document_keys(
    session: AsyncSession, candidate_id: uuid.UUID | str
) -> tuple[dict[str, str], ...]:
    """Every object key this candidate's BGV documents own.

    Shaped exactly like `erasure.candidate_object_keys` entries,
    `{"key": ..., "kind": ...}`, so the two concatenate without a translation
    step that could drop a field.

    CALL THIS BEFORE THE ROWS ARE ERASED. Nothing else in the database names
    these objects, so an enumeration run after the cascade returns an empty
    tuple and a caller acting on it reports a complete deletion of nothing.

    THE SESSION MUST BE IN A BYPASS SCOPE, for the reason the resume
    enumeration gives: these rows are tenant-free and a tenant-scoped read
    would answer with whatever subset it could see.
    """
    identifier = uuid.UUID(str(candidate_id))
    rows = (
        await session.execute(
            text(
                "SELECT object_key FROM candidate_bgv_documents "
                "WHERE candidate_id = :cid "
                "AND object_key IS NOT NULL AND btrim(object_key) <> ''"
            ),
            {"cid": str(identifier)},
        )
    ).all()
    return tuple(
        {"key": str(key), "kind": KIND_BGV_DOCUMENT} for (key,) in rows
    )


async def delete_candidate_documents(
    session: AsyncSession, candidate_id: uuid.UUID | str
) -> int:
    """Delete this candidate's BGV documents, rows and bytes. Returns the count.

    The rows are deleted first and the objects second, and an object still
    referenced by ANOTHER candidate's row is left alone: the key is a content
    address, so two people who uploaded byte-identical files share one object
    and erasing one person must not remove the other's document.

    An UNCONFIRMED deletion raises `BGVDocumentNotDeleted`. An erasure that
    reported success while the bytes stayed readable is exactly the failure
    `erasure.LegacyObjectNotDeletable` exists to make visible, and swallowing
    it here would reintroduce it one table over.
    """
    identifier = uuid.UUID(str(candidate_id))
    keys = [entry["key"] for entry in await candidate_document_keys(session, identifier)]
    if not keys:
        return 0
    await session.execute(
        text("DELETE FROM candidate_bgv_documents WHERE candidate_id = :cid"),
        {"cid": str(identifier)},
    )
    for key in dict.fromkeys(keys):
        await _drop_object_if_unreferenced(session, key)
    return len(keys)
