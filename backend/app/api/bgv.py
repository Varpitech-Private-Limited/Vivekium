"""Background verification: the candidate's declaration, and the recruiter's diligence.

TWO AUDIENCES, ONE ROUTER, AND THE SESSION DEPENDENCY IS THE BOUNDARY
-----------------------------------------------------------------------
`/bgv/me` is the CANDIDATE's own employment history and runs on
`get_candidate_db`. Everything else is the recruiter's and runs on
`get_tenant_db` behind `require_capability`. A candidate can never reach a
verification record and a recruiter can never rewrite an employment claim,
because neither route exists for them.

THE PRINCIPAL AND THE SESSION MUST NAME THE SAME AUDIENCE (2026-09-24)
------------------------------------------------------------------------
Until this date every `/bgv/me*` route declared `get_current_user` (staff
audiences only) beside `get_candidate_db` (candidate audience only). No token
satisfies both, so every real candidate got a 401 and Employment History was
unreachable, while every test passed because every test overrode BOTH
dependencies. The candidate routes now take `get_current_candidate`;
`tests/test_candidate_audience_consistency.py` refuses the mixed shape on any
route in the app, and `tests/test_bgv_real_candidate_token.py` calls these
routes with a real signed-in session and no overrides at all.

WHAT IS DELIBERATELY ABSENT
-----------------------------
There is no route that sets a verification status from email text, and no
parameter anywhere that infers one. `POST /verifications/{id}/decision` takes a
BOOLEAN A PERSON SET. The brief is explicit that HR responses are reviewed
manually, and the deeper reason is `hiring/layers.INVARIANTS`: an automated
decision here would be the product deciding a hiring outcome.

There is also no DELETE for an employment row and no PUT after finalisation.
The immutability is enforced three times over, on purpose: the database refuses
the write, the service refuses it with a sentence the candidate can act on, and
the route never offers it.

NARROWED 2026-09-18 (vivekium feature 5, owner-ruled final): after
finalisation the candidate may APPEND their newest employer through
`POST /bgv/me/employers`, and the last-two cap auto-drops the oldest row
under the one sanctioned escape (`services/bgv_maintenance`, the 0103
trigger). No edit of an existing row and no chosen deletion exists anywhere,
which is the half of the finality that survives.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    get_candidate_db,
    get_current_candidate,
    get_current_user,
    get_public_db,
    get_tenant_db,
    require_organisation_wide,
)
from app.core.config import get_settings
from app.models.bgv_documents import (
    CORRECTION_REASON_BOUNCE,
    DOCUMENT_TYPE_LABELS,
)
from app.models.bgv_verification import (
    DELIVERY_BOUNCED,
    DELIVERY_NOT_SENT,
    VERIFICATION_NOT_STARTED,
    VERIFICATION_NOT_VERIFIED,
    VERIFICATION_PENDING,
    VERIFICATION_VERIFIED,
)
from app.models.conversation import CHANNEL_EMAIL, DELIVERY_SENT, PARTY_RECRUITER
from app.models.employment import BACKGROUND_EXPERIENCED, EMPLOYMENT_BACKGROUNDS
from app.schemas.bgv_workflow import (
    BGVDocumentOut,
    BGVDocumentsOut,
    CandidateBGVOut,
    EmploymentIn,
    DecisionIn,
    DraftOut,
    EmploymentHistoryIn,
    EmploymentHistoryOut,
    EmploymentOut,
    HREmailCorrectionIn,
    OwnEmploymentOut,
    SendIn,
    VerificationOut,
)
from app.services import (
    bgv,
    bgv_agent,
    bgv_delivery,
    bgv_documents,
    bgv_form,
    bgv_maintenance,
    bgv_workflow,
    candidate_identity,
    conversations,
)
from app.services import capabilities as caps
from app.services.audit import audit
from app.workers.dispatch import dispatch_after_commit

router = APIRouter()

#: Shown to the candidate BEFORE they submit, served from the server so the
#: warning and the rule cannot drift apart. A one-way door that is not
#: announced is a trap rather than a guarantee.
SUBMISSION_WARNING = (
    "These details are used to contact each previous employer and verify your "
    "employment. Once you submit, they are FINAL and cannot be edited or "
    "removed, including by our support team. Please check every company name, "
    "job title, date and HR email address carefully before you submit."
)


# ── The candidate's own history ──────────────────────────────────────────────


async def _candidate_id_for(session: AsyncSession, user: CurrentUser) -> uuid.UUID:
    """The signed-in candidate's record, through the ONE resolver.

    `candidate_identity.require_candidate` matches `candidates.user_id` and
    nothing else, and 404s with the portal's own sentence when there is no
    record. The raw SQL this replaced also matched the candidate row by the
    signed-in user's email, an address Firebase may never have verified, so
    a password sign-up carrying somebody else's address could read and write
    THEIR employment history. Never trusts an id from the client.
    """
    return (await candidate_identity.require_candidate(session, user.user_id)).id


async def _history_out(
    session: AsyncSession, candidate_id: uuid.UUID
) -> EmploymentHistoryOut:
    row = (
        (
            await session.execute(
                text(
                    "SELECT employment_background, employment_history_finalized_at "
                    "FROM candidates WHERE id = :cid"
                ),
                {"cid": str(candidate_id)},
            )
        )
        .mappings()
        .first()
    )
    rows = (
        (
            await session.execute(
                text(
                    "SELECT id, employer_name, designation, started_on, ended_on, "
                    " hr_name, hr_email FROM candidate_employments "
                    "WHERE candidate_id = :cid ORDER BY started_on DESC, id"
                ),
                {"cid": str(candidate_id)},
            )
        )
        .mappings()
        .all()
    )
    needs_correction = await bgv_delivery.employments_needing_correction(
        session, candidate_id
    )
    return EmploymentHistoryOut(
        background=row["employment_background"] if row else None,
        finalized=bool(row and row["employment_history_finalized_at"]),
        finalized_at=row["employment_history_finalized_at"] if row else None,
        employments=[
            OwnEmploymentOut(
                **dict(item),
                correction_needed=uuid.UUID(str(item["id"])) in needs_correction,
            )
            for item in rows
        ],
        submission_warning=SUBMISSION_WARNING,
    )


@router.get("/me", response_model=EmploymentHistoryOut)
async def my_employment_history(
    user: CurrentUser = Depends(get_current_candidate),
    session: AsyncSession = Depends(get_candidate_db),
) -> EmploymentHistoryOut:
    """The candidate's own declaration and its employers."""
    return await _history_out(session, await _candidate_id_for(session, user))


@router.put("/me", response_model=EmploymentHistoryOut)
async def save_employment_history(
    body: EmploymentHistoryIn,
    user: CurrentUser = Depends(get_current_candidate),
    session: AsyncSession = Depends(get_candidate_db),
) -> EmploymentHistoryOut:
    """Save the declaration, and optionally close it forever.

    REPLACE, not merge. The candidate edits a list on one screen and submits
    the list they see; a merge would leave a row they had deleted from the form
    still in the database and still about to be emailed.

    The replace is only legal BEFORE finalisation. After it, this refuses with
    409 and the database refuses independently, so the guarantee does not rest
    on this function being the only writer.
    """
    candidate_id = await _candidate_id_for(session, user)
    finalized_at = (
        await session.execute(
            text(
                "SELECT employment_history_finalized_at FROM candidates WHERE id = :cid"
            ),
            {"cid": str(candidate_id)},
        )
    ).scalar()
    if finalized_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Your employment details were submitted on "
                f"{finalized_at:%d %B %Y} and are final. They cannot be edited."
            ),
        )

    if body.background not in EMPLOYMENT_BACKGROUNDS:  # pragma: no cover - schema guards
        raise HTTPException(status_code=422, detail="Choose fresher or experienced.")

    await session.execute(
        text("DELETE FROM candidate_employments WHERE candidate_id = :cid"),
        {"cid": str(candidate_id)},
    )
    for item in body.employments:
        await session.execute(
            text(
                "INSERT INTO candidate_employments (id, candidate_id, employer_name, "
                " designation, started_on, ended_on, hr_name, hr_email, created_at) "
                "VALUES (gen_random_uuid(), :cid, :name, :role, :start, :end, "
                " :hr, :email, now())"
            ),
            {
                "cid": str(candidate_id),
                "name": item.employer_name.strip(),
                "role": item.designation.strip(),
                "start": item.started_on,
                "end": item.ended_on,
                "hr": item.hr_name.strip(),
                "email": str(item.hr_email).strip(),
            },
        )

    now = datetime.now(timezone.utc)
    await session.execute(
        text(
            "UPDATE candidates SET employment_background = :bg"
            + (", employment_history_finalized_at = :at" if body.finalize else "")
            + " WHERE id = :cid"
        ),
        (
            {"bg": body.background, "cid": str(candidate_id), "at": now}
            if body.finalize
            else {"bg": body.background, "cid": str(candidate_id)}
        ),
    )
    if body.finalize:
        await audit(
            session,
            tenant_id=None,
            actor_user_id=user.user_id,
            action=bgv_workflow.AUDIT_HISTORY_SUBMITTED,
            target_type="candidate",
            target_id=candidate_id,
            metadata={
                "background": body.background,
                "employer_count": len(body.employments),
            },
        )
    await session.flush()
    return await _history_out(session, candidate_id)


@router.post("/me/employers", response_model=EmploymentHistoryOut)
async def append_employer_route(
    body: EmploymentIn,
    user: CurrentUser = Depends(get_current_candidate),
    session: AsyncSession = Depends(get_candidate_db),
) -> EmploymentHistoryOut:
    """Add the candidate's NEWEST employer to a finalised history.

    Vivekium feature 5, which narrows the 2026-09-12 finality: the BGV
    record is permanent and portable and holds the LAST TWO employers, so a
    candidate whose career moved on may APPEND, and only append. The cap
    auto-drops the oldest row (its verifications go with it, the consent
    item's own words), and auto-maintenance fires the new employer's
    verification for every tenant already verifying this candidate, with no
    manual trigger and no admin action.

    BEFORE finalisation this route refuses: the ordinary save screen owns
    the draft list, and two writers for one list is how a row the candidate
    deleted comes back.
    """
    candidate_id = await _candidate_id_for(session, user)
    finalized_at = (
        await session.execute(
            text(
                "SELECT employment_history_finalized_at FROM candidates "
                "WHERE id = :cid"
            ),
            {"cid": str(candidate_id)},
        )
    ).scalar()
    if finalized_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Your employment details have not been submitted yet. Edit "
                "and submit the list on this page instead."
            ),
        )
    result = await bgv_maintenance.append_employer(
        session,
        candidate_id=candidate_id,
        employer_name=body.employer_name,
        designation=body.designation,
        started_on=body.started_on,
        ended_on=body.ended_on,
        hr_name=body.hr_name,
        hr_email=str(body.hr_email),
    )
    await audit(
        session,
        tenant_id=None,
        actor_user_id=user.user_id,
        action=bgv_workflow.AUDIT_HISTORY_APPENDED,
        target_type="candidate",
        target_id=candidate_id,
        metadata={
            "employer": body.employer_name.strip(),
            "dropped": result["dropped"],
        },
    )
    await session.flush()
    # The auto-maintenance is DISPATCHED (rule 4): it emails employers, and
    # a candidate's save must not block on a mail merge. After the COMMIT,
    # because the task reads the employment row this request just wrote; a
    # lost invoke is logged at ERROR by the commit hook and the next history
    # save runs the same maintenance again.
    dispatch_after_commit(
        session,
        "pickready.bgv_auto_maintenance",
        args=[str(candidate_id), result["added_id"]],
    )
    return await _history_out(session, candidate_id)


# ── Correcting an HR address the provider refused ────────────────────────────
#
# THE IMMUTABILITY TRIGGER IS NOT WEAKENED, AND THIS ROUTE IS WHY IT DID NOT
# HAVE TO BE. `candidate_employments` holds the candidate's CLAIM: employer,
# title, dates, and the HR contact as first declared. An employer whose mailbox
# bounced has not made the claim wrong; the routing is wrong. So a correction
# is a NEW ROW beside the claim (`bgv_contact_corrections`, append-only) rather
# than an UPDATE of it, the 0103 trigger keeps refusing every UPDATE exactly as
# it did, and the declaration stays readable next to every address ever tried.
#
# The alternative considered and rejected was a second GUC admitting an UPDATE
# of `hr_email` alone. It is strictly weaker on two counts: a transaction-local
# escape hatch is reachable by anything that can set the GUC, and the
# overwritten address would be gone, so a candidate quietly redirecting a
# verification to a mailbox they control would leave nothing behind to notice.


@router.put("/me/employers/{employment_id}/hr-email", response_model=EmploymentHistoryOut)
async def correct_hr_email(
    employment_id: uuid.UUID,
    body: HREmailCorrectionIn,
    user: CurrentUser = Depends(get_current_candidate),
    session: AsyncSession = Depends(get_candidate_db),
) -> EmploymentHistoryOut:
    """Replace the HR address on an employer whose request could not be delivered.

    ONLY AFTER A BOUNCE, and only on the address. The brief asks the candidate
    to correct the address and the system to resend automatically; it does not
    ask for an employment editor, and a route that accepted a correction at any
    time would be one: a candidate could move a verification to a mailbox they
    control the day before an offer, and nothing would have refused it.

    Every bounced verification for this employer, across every tenant that
    opened one, is resent with a FRESH single-use token. All of them, because
    the address was wrong for all of them and asking the candidate to fix it
    once per customer would be the product exporting its own data model.
    """
    candidate_id = await _candidate_id_for(session, user)
    employment = (
        (
            await session.execute(
                text(
                    "SELECT id, employer_name, designation, started_on, ended_on, "
                    " hr_name, hr_email FROM candidate_employments "
                    "WHERE id = :eid AND candidate_id = :cid"
                ),
                {"eid": str(employment_id), "cid": str(candidate_id)},
            )
        )
        .mappings()
        .first()
    )
    if employment is None:
        # 404 rather than 403: confirming that somebody else's employment row
        # exists is the rule every cross-owner read here follows.
        raise HTTPException(status_code=404, detail="Employer not found")

    new_address = str(body.hr_email).strip()
    current = await bgv_delivery.effective_hr_email(session, employment_id)
    if new_address.lower() == (current or "").lower():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "That is the address we already tried. Please check it with "
                "the employer and enter the corrected one."
            ),
        )
    # The same refusal the declaration itself is held to: a reply from a
    # personal mailbox proves a person owns a mailbox, not that a company's HR
    # department answered.
    if bgv.is_free_provider_domain(new_address):
        raise HTTPException(
            status_code=422,
            detail=(
                "Use your previous employer's own company email address. A "
                "personal mailbox cannot confirm employment on their behalf."
            ),
        )

    bounced = (
        (
            await session.execute(
                text(
                    "SELECT v.id, v.tenant_id, v.conversation_id, t.name AS tenant_name "
                    "FROM bgv_verifications v "
                    "LEFT JOIN tenants t ON t.id = v.tenant_id "
                    "WHERE v.candidate_employment_id = :eid "
                    f"  AND {bgv_delivery.AWAITING_CORRECTION_SQL}"
                ),
                {"eid": str(employment_id), "bounced": DELIVERY_BOUNCED},
            )
        )
        .mappings()
        .all()
    )
    if not bounced:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "We have not had a delivery failure for this employer, so "
                "there is nothing to correct. Your employment details are "
                "final once submitted."
            ),
        )

    now = datetime.now(timezone.utc)
    await session.execute(
        text(
            "INSERT INTO bgv_contact_corrections (id, candidate_id, "
            " candidate_employment_id, previous_hr_email, hr_email, reason, "
            " created_at) "
            "VALUES (gen_random_uuid(), :cid, :eid, :old, :new, :reason, :at)"
        ),
        {
            "cid": str(candidate_id),
            "eid": str(employment_id),
            "old": current or employment["hr_email"],
            "new": new_address,
            "reason": CORRECTION_REASON_BOUNCE,
            "at": now,
        },
    )
    for verification in bounced:
        await _resend_after_correction(
            session,
            verification=verification,
            employment=dict(employment),
            candidate_id=candidate_id,
            candidate_name=None,
            recipient=new_address,
            now=now,
        )
    await audit(
        session,
        tenant_id=None,
        actor_user_id=user.user_id,
        action=bgv_workflow.AUDIT_HR_EMAIL_CORRECTED,
        target_type="candidate_employment",
        target_id=employment_id,
        metadata={
            "employer": employment["employer_name"],
            # MASKED IN BOTH DIRECTIONS. The audit log is read by staff and
            # exported; a third party's full address is not a fact it needs to
            # carry to answer "who changed what, and when".
            "previous": bgv_form.masked_email(current or employment["hr_email"]),
            "corrected": bgv_form.masked_email(new_address),
            "resent": len(bounced),
        },
    )
    await session.flush()
    return await _history_out(session, candidate_id)


async def _resend_after_correction(
    session: AsyncSession,
    *,
    verification: dict,
    employment: dict,
    candidate_id: uuid.UUID,
    candidate_name: str | None,
    recipient: str,
    now: datetime,
) -> None:
    """Send one verification again, to the corrected address, with a new token.

    THE TOKEN IS FRESH. The one already sitting in a mailbox that refused the
    message is worthless, and reusing it would leave two credentials live for
    one verification if the original ever did arrive somewhere.

    The DETERMINISTIC body, never the agent: nobody is reviewing this draft, so
    `generated_by_ai` is recorded False rather than claimed.
    """
    tenant_id = uuid.UUID(str(verification["tenant_id"]))
    name = candidate_name or (
        await session.execute(
            text("SELECT full_name FROM candidates WHERE id = :cid"),
            {"cid": str(candidate_id)},
        )
    ).scalar()
    facts = bgv_agent.FactBlock(
        candidate_name=name or "The candidate",
        employer_name=employment["employer_name"],
        designation=employment["designation"],
        started_on=employment["started_on"],
        ended_on=employment["ended_on"],
        hr_name=employment["hr_name"],
        recruiter_team=f"Recruitment team, {verification['tenant_name'] or 'our team'}",
    )
    subject = bgv_agent.subject_for(facts)
    body_text = bgv_agent.deterministic_body(facts)
    verification_id = uuid.UUID(str(verification["id"]))
    token = await bgv_delivery.issue_form_token(
        session, verification_id=verification_id, at=now
    )
    if token is not None:
        body_text = f"{body_text}\n\n{bgv_delivery.form_link_paragraph(token)}"

    reply_to = None
    if verification["conversation_id"] is not None:
        conversation = await conversations.authorize_participant(
            session,
            conversation_id=uuid.UUID(str(verification["conversation_id"])),
            tenant_id=tenant_id,
        )
        await conversations.post_message(
            session,
            conversation_id=uuid.UUID(str(verification["conversation_id"])),
            tenant_id=tenant_id,
            author_party=PARTY_RECRUITER,
            body=body_text,
            channel=CHANNEL_EMAIL,
            delivery_status=DELIVERY_SENT,
            client_token=f"bgv-resend-{verification_id}-{int(now.timestamp())}",
            now=now,
        )
        reply_to = conversations.reply_address(conversation["thread_token"])
    email_log_id = await bgv_delivery.record_outbound(
        session,
        tenant_id=tenant_id,
        verification_id=verification_id,
        candidate_id=candidate_id,
        recipient=recipient,
        subject=subject,
        body=body_text,
        generated_by_ai=False,
        edited_by_human=False,
    )
    # Clears `bounced_at` and `reminder_sent_at`, so the corrected request gets
    # its own three days instead of inheriting a clock that already ran out.
    await bgv_delivery.mark_sent(session, verification_id=verification_id, at=now)
    # After the COMMIT: the task resolves the `email_log` row written above,
    # which a pre-commit invoke could not see. A lost invoke is logged at
    # ERROR by the commit hook; the correction can be submitted again.
    dispatch_after_commit(
        session,
        "pickready.send_email",
        args=[
            str(tenant_id),
            recipient,
            "bgv_verification",
            {"subject": subject, "body": body_text},
            None,
            reply_to,
            str(email_log_id),
        ],
    )


# ── A candidate's legacy documents ───────────────────────────────────────────
#
# UPLOAD IS RETIRED (production-fix pass, 2026-11): the profile no longer
# collects academic certificates or address proof, so there is no POST. These
# two routes remain only so a candidate who uploaded a file earlier can see it
# and withdraw it; they have no screen. There is deliberately NO recruiter
# route: the product has no consent record for sharing a degree certificate.
# The plan for the stored files is `docs/operations/BGV_LEGACY_DOCUMENTS.md`.


def _documents_out(documents: list) -> BGVDocumentsOut:
    return BGVDocumentsOut(
        documents=[
            BGVDocumentOut(
                id=document.id,
                document_type=document.document_type,
                document_label=DOCUMENT_TYPE_LABELS[document.document_type],
                original_filename=document.original_filename,
                mime_type=document.mime_type,
                size_bytes=document.size_bytes,
                uploaded_at=document.uploaded_at,
            )
            for document in documents
        ],
    )


@router.get("/me/documents", response_model=BGVDocumentsOut)
async def my_bgv_documents(
    user: CurrentUser = Depends(get_current_candidate),
    session: AsyncSession = Depends(get_candidate_db),
) -> BGVDocumentsOut:
    """The candidate's own uploaded certificates and address proof."""
    candidate_id = await _candidate_id_for(session, user)
    return _documents_out(
        await bgv_documents.list_documents(session, candidate_id=candidate_id)
    )


@router.delete("/me/documents/{document_id}", response_model=BGVDocumentsOut)
async def delete_bgv_document(
    document_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_candidate),
    session: AsyncSession = Depends(get_candidate_db),
) -> BGVDocumentsOut:
    """Remove one of the candidate's own documents, bytes included.

    A document is the candidate's to withdraw. It confirms nothing and blocks
    nothing, so unlike the employment declaration there is no finality to
    protect here, and refusing the delete would only mean a file they no longer
    want is one they cannot remove.
    """
    candidate_id = await _candidate_id_for(session, user)
    removed = await bgv_documents.delete_document(
        session, candidate_id=candidate_id, document_id=document_id
    )
    if not removed:
        raise HTTPException(status_code=404, detail="Document not found")
    await session.flush()
    return _documents_out(
        await bgv_documents.list_documents(session, candidate_id=candidate_id)
    )


# ── The recruiter's view ─────────────────────────────────────────────────────


async def _assert_candidate_in_tenant(
    session: AsyncSession, candidate_id: uuid.UUID, tenant_id: uuid.UUID
) -> str | None:
    """A candidate this tenant has never linked to a job is NOT FOUND.

    404 rather than 403, the rule every cross-tenant read in this product
    follows: distinguishing them confirms that somebody else's candidate exists.
    """
    row = (
        (
            await session.execute(
                text(
                    "SELECT c.full_name FROM candidates c "
                    "WHERE c.id = :cid AND EXISTS ("
                    "  SELECT 1 FROM job_candidate_links l "
                    "   WHERE l.candidate_id = c.id AND l.tenant_id = :tid)"
                ),
                {"cid": str(candidate_id), "tid": str(tenant_id)},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return row["full_name"]


async def _candidate_bgv(
    session: AsyncSession,
    *,
    candidate_id: uuid.UUID,
    tenant_id: uuid.UUID,
    name: str | None,
) -> CandidateBGVOut:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT e.id AS employment_id, e.employer_name, e.designation, "
                    " e.started_on, e.ended_on, e.hr_name, e.hr_email, "
                    " v.id AS verification_id, v.status, v.conversation_id, "
                    " v.first_sent_at, v.responded_at, v.decided_at, "
                    " v.decision_note, v.delivery_status, v.delivered_at, "
                    " v.bounced_at, v.delivery_detail, "
                    " u.full_name AS decided_by_name "
                    "FROM candidate_employments e "
                    "LEFT JOIN bgv_verifications v "
                    "  ON v.candidate_employment_id = e.id AND v.tenant_id = :tid "
                    "LEFT JOIN users u ON u.id = v.decided_by "
                    "WHERE e.candidate_id = :cid "
                    "ORDER BY e.started_on DESC, e.id"
                ),
                {"cid": str(candidate_id), "tid": str(tenant_id)},
            )
        )
        .mappings()
        .all()
    )

    background = (
        await session.execute(
            text("SELECT employment_background FROM candidates WHERE id = :cid"),
            {"cid": str(candidate_id)},
        )
    ).scalar()

    verifications = [
        VerificationOut(
            id=row["verification_id"] or row["employment_id"],
            employment=EmploymentOut(
                id=row["employment_id"],
                employer_name=row["employer_name"],
                designation=row["designation"],
                started_on=row["started_on"],
                ended_on=row["ended_on"],
                hr_name=row["hr_name"],
                hr_email=row["hr_email"],
            ),
            status=row["status"] or VERIFICATION_NOT_STARTED,
            conversation_id=row["conversation_id"],
            first_sent_at=row["first_sent_at"],
            responded_at=row["responded_at"],
            decided_at=row["decided_at"],
            decided_by_name=row["decided_by_name"],
            decision_note=row["decision_note"],
            delivery_status=row["delivery_status"] or DELIVERY_NOT_SENT,
            delivered_at=row["delivered_at"],
            bounced_at=row["bounced_at"],
            delivery_detail=row["delivery_detail"],
        )
        for row in rows
    ]
    statuses = [row["status"] for row in rows if row["status"]]
    derived = bgv_workflow.derive_status(
        background=background, employer_count=len(rows), statuses=statuses
    )
    return CandidateBGVOut(
        candidate_id=candidate_id,
        candidate_name=name,
        background=background,
        required=background == BACKGROUND_EXPERIENCED,
        status=derived,
        employer_count=len(rows),
        verified_count=sum(1 for value in statuses if value == VERIFICATION_VERIFIED),
        verifications=verifications,
        offer_blocked_reason=bgv_workflow.blocking_message(derived),
    )


@router.get(
    "/candidates/{candidate_id}",
    response_model=CandidateBGVOut,
    dependencies=[Depends(require_organisation_wide(caps.VIEW_BGV))],
)
async def candidate_bgv(
    candidate_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_db),
) -> CandidateBGVOut:
    name = await _assert_candidate_in_tenant(session, candidate_id, user.tenant_id)
    return await _candidate_bgv(
        session, candidate_id=candidate_id, tenant_id=user.tenant_id, name=name
    )


@router.post(
    "/candidates/{candidate_id}/initiate",
    response_model=CandidateBGVOut,
    dependencies=[Depends(require_organisation_wide(caps.MANAGE_BGV))],
)
async def initiate(
    candidate_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_db),
) -> CandidateBGVOut:
    """Open one verification and one conversation per submitted employer.

    IDEMPOTENT. Two recruiters opening the same candidate on the same afternoon
    must not produce two records for one employer; the UNIQUE constraint on
    (tenant, employment) is what enforces that, and this skips what exists.

    Refused before the candidate has SUBMITTED: verifying a draft would email
    an employer about details the candidate is still editing.
    """
    name = await _assert_candidate_in_tenant(session, candidate_id, user.tenant_id)
    row = (
        (
            await session.execute(
                text(
                    "SELECT employment_background, employment_history_finalized_at "
                    "FROM candidates WHERE id = :cid"
                ),
                {"cid": str(candidate_id)},
            )
        )
        .mappings()
        .first()
    )
    if row["employment_background"] != BACKGROUND_EXPERIENCED:
        raise HTTPException(
            status_code=409,
            detail=(
                "This candidate has not declared previous employment, so there "
                "is nothing to verify."
            ),
        )
    if row["employment_history_finalized_at"] is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "This candidate has not submitted their employment details yet. "
                "Verification opens once they do."
            ),
        )

    employments = (
        (
            await session.execute(
                text(
                    "SELECT e.id, e.employer_name, e.hr_name, e.hr_email "
                    "FROM candidate_employments e "
                    "WHERE e.candidate_id = :cid AND NOT EXISTS ("
                    "  SELECT 1 FROM bgv_verifications v "
                    "   WHERE v.candidate_employment_id = e.id AND v.tenant_id = :tid)"
                ),
                {"cid": str(candidate_id), "tid": str(user.tenant_id)},
            )
        )
        .mappings()
        .all()
    )

    link_id = (
        await session.execute(
            text(
                "SELECT id FROM job_candidate_links WHERE candidate_id = :cid "
                "AND tenant_id = :tid ORDER BY created_at DESC LIMIT 1"
            ),
            {"cid": str(candidate_id), "tid": str(user.tenant_id)},
        )
    ).scalar()

    for employment in employments:
        verification_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO bgv_verifications (id, tenant_id, candidate_id, "
                " candidate_employment_id, initiated_from_link_id, status, created_at) "
                "VALUES (:id, :tid, :cid, :eid, :lid, :st, now())"
            ),
            {
                "id": str(verification_id),
                "tid": str(user.tenant_id),
                "cid": str(candidate_id),
                "eid": str(employment["id"]),
                "lid": str(link_id) if link_id else None,
                "st": VERIFICATION_NOT_STARTED,
            },
        )
        conversation = await conversations.create_bgv_conversation(
            session,
            tenant_id=user.tenant_id,
            verification_id=verification_id,
            candidate_id=candidate_id,
            candidate_name=name or "Candidate",
            employer_name=employment["employer_name"],
            hr_name=employment["hr_name"],
            hr_email=employment["hr_email"],
            actor_user_id=user.user_id,
        )
        await session.execute(
            text("UPDATE bgv_verifications SET conversation_id = :conv WHERE id = :id"),
            {"conv": str(conversation["id"]), "id": str(verification_id)},
        )
        await audit(
            session,
            tenant_id=user.tenant_id,
            actor_user_id=user.user_id,
            action=bgv_workflow.AUDIT_VERIFICATION_INITIATED,
            target_type="bgv_verification",
            target_id=verification_id,
            metadata={"employer": employment["employer_name"]},
        )
    await session.flush()
    return await _candidate_bgv(
        session, candidate_id=candidate_id, tenant_id=user.tenant_id, name=name
    )


async def _verification_or_404(
    session: AsyncSession, verification_id: uuid.UUID, tenant_id: uuid.UUID
) -> dict:
    row = (
        (
            await session.execute(
                text(
                    "SELECT v.id, v.tenant_id, v.status, v.conversation_id, "
                    " v.candidate_id, v.first_sent_at, v.form_submitted_at, "
                    " v.delivery_status, v.delivered_at, "
                    " e.id AS employment_id, e.employer_name, "
                    " e.designation, e.started_on, e.ended_on, e.hr_name, "
                    " e.hr_email, c.full_name "
                    "FROM bgv_verifications v "
                    "JOIN candidate_employments e ON e.id = v.candidate_employment_id "
                    "JOIN candidates c ON c.id = v.candidate_id "
                    "WHERE v.id = :vid"
                ),
                {"vid": str(verification_id)},
            )
        )
        .mappings()
        .first()
    )
    if row is None or str(row["tenant_id"]) != str(tenant_id):
        raise HTTPException(status_code=404, detail="Verification not found")
    return dict(row)


def _facts_for(row: dict, team: str) -> bgv_agent.FactBlock:
    return bgv_agent.FactBlock(
        candidate_name=row["full_name"] or "The candidate",
        employer_name=row["employer_name"],
        designation=row["designation"],
        started_on=row["started_on"],
        ended_on=row["ended_on"],
        hr_name=row["hr_name"],
        recruiter_team=team,
    )


async def _tenant_name(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    name = (
        await session.execute(
            text("SELECT name FROM tenants WHERE id = :tid"), {"tid": str(tenant_id)}
        )
    ).scalar()
    return f"Recruitment team, {name}" if name else "Recruitment team"


@router.post(
    "/verifications/{verification_id}/draft",
    response_model=DraftOut,
    dependencies=[Depends(require_organisation_wide(caps.MANAGE_BGV))],
)
async def draft(
    verification_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_db),
) -> DraftOut:
    """Ask the BGV Agent for a verification email. Sends nothing.

    Regenerating is allowed and cheap: the recruiter owns the final text, and a
    draft they discarded costs one model call.
    """
    row = await _verification_or_404(session, verification_id, user.tenant_id)
    facts = _facts_for(row, await _tenant_name(session, user.tenant_id))
    subject, body, generated_by_ai = await bgv_agent.draft_verification_email(facts)
    await audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.user_id,
        action=bgv_workflow.AUDIT_EMAIL_GENERATED,
        target_type="bgv_verification",
        target_id=verification_id,
        metadata={"generated_by_ai": generated_by_ai},
    )
    return DraftOut(
        verification_id=verification_id,
        subject=subject,
        body=body,
        generated_by_ai=generated_by_ai,
    )


async def _one_verification(
    session: AsyncSession, verification_id: uuid.UUID, tenant_id: uuid.UUID
) -> VerificationOut:
    row = (
        (
            await session.execute(
                text(
                    "SELECT v.id, v.status, v.conversation_id, v.first_sent_at, "
                    " v.responded_at, v.decided_at, v.decision_note, "
                    " v.delivery_status, v.delivered_at, v.bounced_at, "
                    " v.delivery_detail, "
                    " e.id AS employment_id, e.employer_name, e.designation, "
                    " e.started_on, e.ended_on, e.hr_name, e.hr_email, "
                    " u.full_name AS decided_by_name "
                    "FROM bgv_verifications v "
                    "JOIN candidate_employments e ON e.id = v.candidate_employment_id "
                    "LEFT JOIN users u ON u.id = v.decided_by "
                    "WHERE v.id = :vid AND v.tenant_id = :tid"
                ),
                {"vid": str(verification_id), "tid": str(tenant_id)},
            )
        )
        .mappings()
        .first()
    )
    if row is None:  # pragma: no cover - caller has already checked
        raise HTTPException(status_code=404, detail="Verification not found")
    return VerificationOut(
        id=row["id"],
        employment=EmploymentOut(
            id=row["employment_id"],
            employer_name=row["employer_name"],
            designation=row["designation"],
            started_on=row["started_on"],
            ended_on=row["ended_on"],
            hr_name=row["hr_name"],
            hr_email=row["hr_email"],
        ),
        status=row["status"],
        conversation_id=row["conversation_id"],
        first_sent_at=row["first_sent_at"],
        responded_at=row["responded_at"],
        decided_at=row["decided_at"],
        decided_by_name=row["decided_by_name"],
        decision_note=row["decision_note"],
        delivery_status=row["delivery_status"] or DELIVERY_NOT_SENT,
        delivered_at=row["delivered_at"],
        bounced_at=row["bounced_at"],
        delivery_detail=row["delivery_detail"],
    )


@router.post(
    "/verifications/{verification_id}/send",
    response_model=VerificationOut,
    dependencies=[Depends(require_organisation_wide(caps.MANAGE_BGV))],
)
async def send(
    verification_id: uuid.UUID,
    body: SendIn,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_db),
) -> VerificationOut:
    """Send the recruiter's edited text to the employer, and record it once.

    The message is written to the conversation BEFORE the dispatch, and the
    dispatch runs only once that write COMMITS, so a send that rolled back
    never leaves the building. The conversation row and the `email_log` row are the
    same event seen from two sides.
    """
    row = await _verification_or_404(session, verification_id, user.tenant_id)
    if row["conversation_id"] is None:  # pragma: no cover - initiate always sets it
        raise HTTPException(status_code=409, detail="This verification has no thread")

    now = datetime.now(timezone.utc)
    # The employer checkbox form link (vivekium feature 4). A FRESH token on
    # every send while unanswered: a re-send after the 3-day expiry has to
    # carry a working link, and replacing the token retires the one already
    # sitting in the mailbox rather than leaving two live credentials. Once
    # the form is submitted the token is never reissued.
    token = await bgv_delivery.issue_form_token(
        session, verification_id=verification_id, at=now
    )
    message = await conversations.post_message(
        session,
        conversation_id=uuid.UUID(str(row["conversation_id"])),
        tenant_id=user.tenant_id,
        author_party=PARTY_RECRUITER,
        author_user_id=user.user_id,
        body=body.body,
        channel=CHANNEL_EMAIL,
        delivery_status=DELIVERY_SENT,
        client_token=f"bgv-send-{verification_id}-{int(now.timestamp())}",
        now=now,
    )
    # THE REPLY ADDRESS IS WHAT MAKES THE THREAD RECEIVE ANYTHING. It carries
    # the conversation's own token, so the employer's answer routes back to
    # THIS employer's thread whatever they do to the subject line and however
    # little of the original their mail client quotes. None when the deployment
    # has no inbound domain, which `conversations.reply_address` records rather
    # than hides: the request still goes out and the reply arrives in the
    # sending mailbox instead.
    conversation = await conversations.authorize_participant(
        session,
        conversation_id=uuid.UUID(str(row["conversation_id"])),
        tenant_id=user.tenant_id,
    )
    # The form link travels UNDER the recruiter's edited text, added
    # server-side so no draft can drop it and no prompt can rewrite it. The
    # sentence states the brief's contract: three days, single use.
    outbound_body = body.body
    if token is not None:
        outbound_body = f"{body.body}\n\n{bgv_delivery.form_link_paragraph(token)}"
    # THE ADDRESS THE CANDIDATE CORRECTED, when they have corrected one. Read
    # through `effective_hr_email` rather than off the employment row, so a
    # recruiter re-sending after a bounce cannot send to the address that
    # already failed. `candidate_employments` still holds the declaration and
    # is still immutable.
    recipient = await bgv_delivery.effective_hr_email(
        session, uuid.UUID(str(row["employment_id"]))
    )
    if not recipient:  # pragma: no cover - hr_email is NOT NULL on the table
        raise HTTPException(
            status_code=409,
            detail="This employer has no HR address to send the request to.",
        )
    # The outbound record, written BEFORE the dispatch and BOUND to this
    # verification. It is what a later bounce or delivery event resolves
    # through: matching on the recipient address instead would pick whichever
    # request to that mailbox went out last (see `services/bgv_delivery`).
    email_log_id = await bgv_delivery.record_outbound(
        session,
        tenant_id=user.tenant_id,
        verification_id=verification_id,
        candidate_id=uuid.UUID(str(row["candidate_id"])),
        recipient=recipient,
        subject=body.subject,
        body=outbound_body,
        generated_by_ai=False,
        edited_by_human=True,
    )
    # After the COMMIT, for the same reason as the correction path: the task
    # resolves the `email_log` row above. A lost invoke is logged at ERROR by
    # the commit hook and leaves the verification pending, so the recruiter
    # sees it unanswered and can send again.
    dispatch_after_commit(
        session,
        "pickready.send_email",
        args=[
            str(user.tenant_id),
            recipient,
            "bgv_verification",
            {"subject": body.subject, "body": outbound_body},
            None,
            conversations.reply_address(conversation["thread_token"]),
            str(email_log_id),
        ],
    )
    await session.execute(
        text(
            "UPDATE bgv_verifications SET status = :st, updated_at = :at "
            "WHERE id = :vid"
        ),
        {"st": VERIFICATION_PENDING, "at": now, "vid": str(verification_id)},
    )
    # The delivery half: stamps `first_sent_at` on the first send and clears
    # the outcome of any PREVIOUS send, so a resend after a bounce starts a
    # fresh three days rather than inheriting a clock that has already run out.
    await bgv_delivery.mark_sent(session, verification_id=verification_id, at=now)
    await audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.user_id,
        action=bgv_workflow.AUDIT_EMAIL_SENT,
        target_type="bgv_verification",
        target_id=verification_id,
        metadata={"message_id": str(message["id"]), "employer": row["employer_name"]},
    )
    await session.flush()
    return await _one_verification(session, verification_id, user.tenant_id)


@router.post(
    "/verifications/{verification_id}/decision",
    response_model=CandidateBGVOut,
    dependencies=[Depends(require_organisation_wide(caps.MANAGE_BGV))],
)
async def decide(
    verification_id: uuid.UUID,
    body: DecisionIn,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_tenant_db),
) -> CandidateBGVOut:
    """A PERSON marks this employer verified, or not. Nothing else may.

    Reversible on purpose: an employer who replies late, or a discrepancy that
    turns out to be a payroll-system artefact, has to be recordable. What is
    NOT reversible is the fact that somebody decided, and when, and who: every
    decision is audited, and `decided_by` is ON DELETE RESTRICT.
    """
    row = await _verification_or_404(session, verification_id, user.tenant_id)
    now = datetime.now(timezone.utc)
    new_status = VERIFICATION_VERIFIED if body.verified else VERIFICATION_NOT_VERIFIED
    await session.execute(
        text(
            "UPDATE bgv_verifications SET status = :st, decided_by = :uid, "
            " decided_at = :at, decision_note = :note, updated_at = :at "
            "WHERE id = :vid"
        ),
        {
            "st": new_status,
            "uid": str(user.user_id),
            "at": now,
            "note": (body.note or "").strip() or None,
            "vid": str(verification_id),
        },
    )
    await audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.user_id,
        action=(
            bgv_workflow.AUDIT_MARKED_VERIFIED
            if body.verified
            else bgv_workflow.AUDIT_MARKED_NOT_VERIFIED
        ),
        target_type="bgv_verification",
        target_id=verification_id,
        metadata={"employer": row["employer_name"]},
    )
    await session.flush()

    candidate_id = uuid.UUID(str(row["candidate_id"]))
    result = await _candidate_bgv(
        session,
        candidate_id=candidate_id,
        tenant_id=user.tenant_id,
        name=row["full_name"],
    )
    # The moment the LAST employer lands, the offer gate opens. Recorded as its
    # own event, because "why can I suddenly extend an offer" is a question the
    # audit log should be able to answer without arithmetic.
    if result.offer_blocked_reason is None and result.required:
        await audit(
            session,
            tenant_id=user.tenant_id,
            actor_user_id=user.user_id,
            action=bgv_workflow.AUDIT_SELECTION_UNLOCKED,
            target_type="candidate",
            target_id=candidate_id,
            metadata={"employer_count": result.employer_count},
        )
    return result


# ── The employer checkbox form, public by token (vivekium feature 4) ─────────
#
# NO AUTHENTICATION: the token IS the credential, unguessable, unique to one
# employer-candidate verification, single-use and 3-day expired. This is the
# same trust model as the apply page, reached through `get_public_db`. The
# form serves and accepts ONLY the seven fixed items; nothing free-text
# crosses this boundary in either direction.


async def _verification_by_form_token(session: AsyncSession, token: str) -> dict:
    row = (
        (
            await session.execute(
                text(
                    "SELECT v.id, v.tenant_id, v.candidate_id, v.status, "
                    " v.form_token_issued_at, v.form_submitted_at, "
                    " v.delivered_at, "
                    " e.employer_name, e.hr_email, c.full_name, c.email "
                    "FROM bgv_verifications v "
                    "JOIN candidate_employments e "
                    "  ON e.id = v.candidate_employment_id "
                    "JOIN candidates c ON c.id = v.candidate_id "
                    "WHERE v.form_token = :tok"
                ),
                {"tok": token},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="This link is not valid.")
    if row["form_submitted_at"] is not None:
        # A DIFFERENT answer from expiry, deliberately: the person clicking a
        # used link most likely already answered, and telling them the link
        # "expired" would send them chasing a new one for work that is done.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This verification has already been completed. Thank you; "
                "nothing further is needed."
            ),
        )
    # THE WINDOW STARTS AT DELIVERY, not at send. An HR team cannot act on a
    # message still in a provider's retry queue, and a link that expired while
    # the message was in flight is a dead credential in a mailbox that never
    # showed it. `clock_start` falls back to the send stamp when no delivery
    # event exists, which is the honest answer under a transport that reports
    # none rather than a link that never expires.
    if bgv_delivery.link_expired(row["delivered_at"], row["form_token_issued_at"]):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                "This verification link has expired. Links work for "
                f"{get_settings().verification_link_ttl_days} days from "
                "delivery. The recruitment team can send a fresh one."
            ),
        )
    return dict(row)


@router.get("/form/{token}")
async def get_employer_checkbox_form(
    token: str, session: AsyncSession = Depends(get_public_db)
) -> dict:
    """What the employer's HR sees: who this is about, and the seven items."""
    row = await _verification_by_form_token(session, token)
    return {
        "candidate_name": row["full_name"],
        "employer_name": row["employer_name"],
        "items": bgv_form.items_payload(),
        "expires_at": bgv_delivery.link_expires_at(
            row["delivered_at"], row["form_token_issued_at"]
        ),
    }


@router.post("/form/{token}")
async def submit_employer_checkbox_form(
    token: str,
    body: dict,
    session: AsyncSession = Depends(get_public_db),
) -> dict:
    """The employer's submission: ticks in, status out, once.

    C4's boundary, held exactly: an HR person ticking the boxes IS the human
    decision, so THIS route may set the status. The model-parsed free-text
    reply path still never does, and `decided_by` stays untouched here
    because it names a platform user and the form's provenance is its own
    stored answers.
    """
    row = await _verification_by_form_token(session, token)
    answers = bgv_form.clean_answers(body.get("answers"))
    missing = [key for key in bgv_form.ITEM_KEYS if key not in answers]
    if missing:
        # Every item must be ANSWERED (true or false), or an unticked box is
        # indistinguishable from an unseen one.
        raise HTTPException(
            status_code=422,
            detail="Every item must be answered before submitting.",
        )
    now = datetime.now(timezone.utc)
    new_status = bgv_form.status_from_answers(answers)
    await session.execute(
        text(
            "UPDATE bgv_verifications SET status = :st, "
            " form_submitted_at = :at, form_answers_json = CAST(:ans AS jsonb), "
            " responded_at = COALESCE(responded_at, :at), updated_at = :at "
            "WHERE id = :vid"
        ),
        {
            "st": new_status,
            "at": now,
            "ans": json.dumps(answers),
            "vid": str(row["id"]),
        },
    )
    await audit(
        session,
        tenant_id=uuid.UUID(str(row["tenant_id"])),
        actor_user_id=None,
        action=bgv_workflow.AUDIT_FORM_SUBMITTED,
        target_type="bgv_verification",
        target_id=uuid.UUID(str(row["id"])),
        metadata={"status": new_status, "employer": row["employer_name"]},
    )
    # Email 2: the candidate is told it is DONE, with no verification detail
    # shared, per the brief. Sent only on a fully confirmed submission; a
    # partial confirmation goes to the recruiter's queue, not the candidate.
    if new_status == VERIFICATION_VERIFIED and row["email"]:
        # After the COMMIT: a form submission that rolled back must not tell
        # the candidate it is done. A lost invoke is logged at ERROR; the
        # verification itself is recorded either way.
        dispatch_after_commit(
            session,
            "pickready.send_email",
            args=[
                str(row["tenant_id"]),
                row["email"],
                "bgv_completed",
                {"candidate_name": row["full_name"] or "there"},
            ],
        )
    return {"status": "recorded"}
