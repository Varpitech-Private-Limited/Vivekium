# Legacy candidate verification documents: retirement, retention and erasure plan

**Status:** current for the code change (upload retired); the purge itself is **decided, pending** an owner authorisation (open question Q-34 in [../decisions/OPEN.md](../decisions/OPEN.md)). Nothing is deleted by the retirement.

## What changed

The My Profile page no longer shows the "verification documents" card (academic certificate and address proof upload). `POST /api/v1/bgv/me/documents` was removed together with its validation, storage and limit code and the two settings that bounded it (`bgv_document_max_bytes`, `bgv_documents_max_per_type`). The "document readiness" answer (all ready, some pending, and so on) is a question on the application form and is untouched. Employer-driven background verification (employment history, HR verification emails, the recruiter `/candidates/{id}/bgv` view) is a separate workflow and is untouched.

What remains, deliberately:

| Surface | Purpose |
|---|---|
| `GET /bgv/me/documents` | A candidate can list files they uploaded earlier (no object key is returned). No screen uses it. |
| `DELETE /bgv/me/documents/{id}` | A candidate can withdraw one of their own files; the row goes, then the object once no other row names it, confirmed by a HEAD. |
| `bgv_documents.candidate_document_keys` and `delete_candidate_documents` | Profile deletion (the erasure cascade) reaches the stored bytes. |

There is no recruiter or company route to these files, and there never was one: no consent record exists for sharing a degree certificate with a customer. Companies see the readiness answer only, never document bytes.

## What exists in storage

- Table `candidate_bgv_documents` (tenant-free, one row per file, `object_key`, `sha256`, filename, size, `uploaded_at`), created by migration 0114.
- Objects under the private bucket prefix `bgv-documents/<sha256>`, content addressed (two identical uploads share one object).

## Retention and erasure plan (decided, pending owner authorisation)

1. **Now (done):** stop collecting. Files already stored are kept; no sweep, no migration, no deletion.
2. **Inventory (operator, read only):** count rows and distinct objects, and group by `uploaded_at` month. A SQL count against `candidate_bgv_documents` and an S3 list of the prefix are enough. Record the totals in `docs/verification/` with no filenames or candidate identities.
3. **Owner decision (Q-34):** choose one of
   - (a) keep until each candidate deletes their profile (today's behaviour: erasure already reaches the bytes);
   - (b) purge all legacy files after a notice period, with an e-mail to each affected candidate first;
   - (c) purge files older than a fixed age, keep newer ones.
   Recommendation: (b), because the product no longer has a purpose or a consent record for holding these documents, and holding identity-adjacent files without a purpose is the larger risk.
4. **If a purge is authorised:** run it as a reviewed one-off script that calls `delete_candidate_documents` per candidate (rows first, bytes second, HEAD-confirmed, any unconfirmed object raises), writes one `audit_log` row per candidate (count only, no filenames), and is idempotent. A schema drop of `candidate_bgv_documents` is a later, separate migration, and must be a guarded drop that counts first and raises if rows remain (see [../engineering/database.md](../engineering/database.md)).
5. **Backups:** erased rows and objects persist in database snapshots and object versions until their own lifecycle expires them; the notice to candidates must say so.

## Verification

- `backend/tests/test_bgv_documents.py`: no upload method on the path (405 and absent from OpenAPI), listing carries no object key, withdrawal removes the bytes, erasure removes the bytes (HEAD), documents gate nothing.
- `backend/tests/test_bgv_documents_retired.py`: no live reference to the removed upload code, settings or card.
- `backend/tests/test_candidate_audience_consistency.py`: both remaining routes are candidate-audience only.
