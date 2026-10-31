"""Every background task the product has, and where each one runs.

  pickready.send_email(tenant_id, to, template_name, context, attachments=None)
  pickready.run_matching(job_id)
  pickready.parse_resume(profile_id)
  pickready.yukti_score_profile(profile_id)
  pickready.refresh_dashboard_views()

All slow work happens here, never inline in a request handler (claude.md rule
4). A task is a plain synchronous function running async service code through
`asyncio.run`, with a FRESH engine per run: an execution environment is frozen
between invocations and a pooled connection does not survive the freeze, so a
shared engine hands the next run a socket that looks healthy and fails on use.

`@task` records the name, the destination and the retry policy in ONE
declaration (see `registry`). `Route.LAMBDA` is work measured in seconds;
`Route.ECS` is work measured in minutes, which runs as one on-demand Fargate
task per dispatch and stops when the process exits.

Retries live in `runtime.run_task`, inside the invocation, and the platform's
asynchronous retry is set to zero so the two cannot multiply. There is no soft
time limit any more: the ceiling is the Lambda's timeout or the Fargate task's
`stopTimeout`, and because the retry loop lives inside the thing that gets
killed, a task that outran its budget is not retried. That is the behaviour the
`dont_autoretry_for` exclusion list used to buy, now structural.

SECURITY (ESD §16): OTP codes and API keys are NEVER logged or written to
audit metadata. Email `context` payloads may contain OTPs and are therefore
never persisted or logged, only template/recipient/status metadata is.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings, preflight_delivery_config
from app.services.delivery_errors import (
    DeliveryError,
    PermanentDeliveryError,
    TransientDeliveryError,
    log_delivery_error,
)
from app.services.smtp_service import send_email_async as smtp_send
from app.models import (
    Candidate,
    Tenant,
)
from app.workers.dispatch import dispatch
from app.workers.registry import Route, task
from app.workers.runtime import (
    TaskContext,
    resolve_tenant_id as _resolve_tenant_id,
    tenant_worker_session as _tenant_worker_session,
    worker_session as _worker_session,
    _run,
)

logger = logging.getLogger(__name__)


# -- Startup preflight -------------------------------------------------------
# Celery ran this from a `worker_ready` signal, once per worker process. There
# is no long-lived worker process any more, so it runs at MODULE IMPORT, which
# in a Lambda execution environment is once per cold start and in a Fargate
# task is once per run. Same guarantee it always gave: a loud WARNING when an
# SMTP credential is missing, never a hard crash.
preflight_delivery_config()


async def _audit(
    session: AsyncSession,
    tenant_id: str | None,
    action: str,
    target_type: str,
    target_id: str | None,
    metadata: dict | None = None,
) -> None:
    """Append an audit_log row (append-only table  -  INSERT only)."""
    await session.execute(
        text(
            "INSERT INTO audit_log (id, tenant_id, actor_user_id, action, "
            "target_type, target_id, metadata_json, at) "
            "VALUES (:id, CAST(:tenant_id AS uuid), NULL, :action, "
            ":target_type, :target_id, CAST(:metadata AS jsonb), :at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "metadata": json.dumps(metadata or {}),
            "at": datetime.now(timezone.utc),
        },
    )
    await session.commit()


async def _audit_delivery_exhausted(
    tenant_id: str | None, to: str, template_name: str, channel: str
) -> None:
    """Write a terminal-failure audit row when transient retries are exhausted.

    Opens its own session because the per-task session used for the send has
    already unwound by the time the final failure is reported.
    """
    try:
        async with _worker_session() as session:
            await _audit(
                session,
                tenant_id if tenant_id else None,
                f"{channel}.delivery",
                channel,
                None,
                {
                    "to": to,
                    "template": template_name,
                    "status": "failed_exhausted",
                    "reason": "transient retries exhausted",
                },
            )
    except Exception:
        # Audit is best-effort here; never mask the real delivery failure.
        logger.exception("%s.audit_exhausted_failed to=%s", channel, to)


# ── Email ────────────────────────────────────────────────────────────────────

async def _deliver_email(
    *,
    to: str,
    subject: str,
    html: str,
    text: str | None = None,
    attachments: list[dict] | None = None,
    sender=None,
    correlation: dict | None = None,
    reply_to: str | None = None,
) -> str | None:
    """One door to the outbound transport (Corporate Email System spec
    section 6). The transport is DEPLOYMENT DATA (`settings.email_transport`,
    "smtp" or "ses"), exactly one per deployment, never a fallback chain.

    `sender` is an ACTIVE ClientEmailSender row or None. Under SES an active
    corporate sender IS the From address; under SMTP Gmail refuses arbitrary
    From on the authenticated mailbox, so the corporate sender travels as
    Reply-To and From stays the Gmail address (assumption recorded in
    smtp_service._build_message). Returns the provider message id.

    An EXPLICIT `reply_to` overrides both, and there is exactly one caller:
    a conversation's own reply address, which is what routes an employer's
    answer back into the thread it belongs to. It wins over the corporate
    sender deliberately -- a reply that reached the sender's mailbox instead of
    the thread is a reply the product cannot see, and a recruiter would sit
    waiting for an answer that had already arrived somewhere else.

    ASSUMPTION (spec section 6): under "ses" with no corporate sender, the
    configured `smtp_from_email` address doubles as the platform's SES-verified
    default identity; the deployment must verify it in the SES account.
    """
    settings = get_settings()
    if settings.email_transport == "ses":
        from app.services.ses_service import send_email_async as ses_send

        return await ses_send(
            from_email=sender.email if sender is not None else settings.smtp_from_email,
            from_name=sender.name if sender is not None else settings.smtp_from_name,
            to=to,
            subject=subject,
            html=html,
            text=text,
            attachments=attachments,
            reply_to=reply_to,
            # SES-ONLY, and deliberately not plumbed into the SMTP path: these
            # become SES message tags, which have no SMTP equivalent. Handing
            # them to Gmail would mean inventing headers nothing ever reads.
            correlation=correlation,
        )
    return await smtp_send(
        from_email=settings.smtp_from_email,
        from_name=settings.smtp_from_name,
        to=to,
        subject=subject,
        html=html,
        text=text,
        attachments=attachments,
        reply_to=reply_to or (sender.email if sender is not None else None),
    )


async def _send_email_async(
    session: AsyncSession,
    tenant_id: str | None,
    to: str,
    template_name: str,
    context: dict,
    attachments: list[dict] | None = None,
    reply_to: str | None = None,
    email_log_id: str | None = None,
) -> dict[str, str]:
    from app.services import email_render

    settings = get_settings()

    # ROOT-CAUSE FIX (2026-07-23): tenant_id is None for platform-level
    # emails  -  the Owner/super_admin OTP has no tenant  -  and this path used to
    # crash with ValueError('badly formed hexadecimal UUID string') from
    # uuid.UUID(str(None)) before any email was sent. No tenant → skip the
    # tenant lookup entirely: default templates + the default SMTP sender.
    tenant: Tenant | None = None
    if tenant_id:
        try:
            tenant = await session.get(Tenant, uuid.UUID(str(tenant_id)))
        except (ValueError, TypeError):
            tenant = None  # invalid id → default-sender path, never a crash
        if tenant is None:
            logger.warning(
                "email.tenant_missing template=%s tenant_id=%s  -  using default sender",
                template_name, tenant_id,
            )

    # ONE transport per deployment (settings.email_transport): the platform's
    # own identity is the From address on this template path either way, and
    # tenant-domain sender substitution happens only through an ACTIVE
    # corporate sender on the lifecycle path, never here.
    sender_path = settings.email_transport

    # Structured, secret-free log so delivery failures are diagnosable  -
    # never the message body/context (may carry OTP codes, ESD §16).
    logger.info(
        "email.sender provider=%s path=%s template=%s tenant_id=%s spf_dkim=%s env=%s",
        settings.email_transport,
        sender_path,
        template_name,
        str(tenant.id) if tenant is not None else "-",
        tenant.spf_dkim_status if tenant is not None else "-",
        settings.environment,
    )

    # Rendering happens INSIDE the audited block. It used to sit above the
    # try/finally, so a template that resolved to neither a tenant row nor a
    # default raised ValueError before the audit was armed: the invitation was
    # discarded with no email_log row, no audit_log row, and a 200 already
    # returned to the caller. The only trace was a stderr traceback. Any future
    # render failure now lands in audit_log with status="failed" like every
    # other delivery failure.
    subject = ""
    body = ""
    html_body = ""
    delivery_status = "sent"
    message_id = ""
    err_meta: dict = {}
    try:
        subject, body = await email_render.render(
            session, tenant.id if tenant is not None else None, template_name, context
        )
        html_body = email_render.text_to_html(body)
        message_id = await _deliver_email(
            to=to,
            subject=subject,
            html=html_body,
            text=body,
            attachments=attachments,
            reply_to=reply_to,
        ) or ""
    except DeliveryError as err:
        delivery_status = "failed"
        err_meta = err.as_audit_metadata()
        log_delivery_error(
            "email", err,
            template=template_name, to=to, sender_path=sender_path,
        )
        raise  # task decides: permanent → no retry, transient → backoff
    except Exception as exc:  # unexpected (e.g. template/render error)
        delivery_status = "failed"
        err_meta = {"error_name": type(exc).__name__, "provider_message": str(exc)[:500]}
        logger.exception(
            "email.delivery_failed kind=unexpected template=%s sender_path=%s",
            template_name, sender_path,
        )
        raise
    finally:
        # SETTLE THE email_log ROW, when the caller made one. Without this the
        # row stays `queued` for ever and an SES event cannot find it: the
        # webhook matches on `provider_message_id`, which only the delivery
        # attempt knows. A binding written at send time and never settled is a
        # correlation that looks present and resolves nothing, which is worse
        # than an absent one because it reads as working.
        #
        # In the `finally` deliberately: `delivery_status` is already correct
        # on both the success and the failure path, so one write covers both
        # and cannot disagree with the audit row written beside it.
        if email_log_id:
            await session.execute(
                text(
                    "UPDATE email_log SET status = :status, "
                    " provider_message_id = COALESCE(:mid, provider_message_id), "
                    " transport = :transport, "
                    " sent_at = CASE WHEN :status = 'sent' THEN now() "
                    "            ELSE sent_at END, "
                    " failed_at = CASE WHEN :status = 'failed' THEN now() "
                    "              ELSE failed_at END "
                    "WHERE id = :id"
                ),
                {
                    "id": email_log_id,
                    "status": delivery_status,
                    "mid": message_id or None,
                    "transport": settings.email_transport,
                },
            )
        # Log delivery to audit_log (ESD §11). NEVER include `context`  -  it may
        # carry OTP codes. Template name + recipient + status + failure taxonomy.
        await _audit(
            session,
            str(tenant.id) if tenant is not None else None,  # platform-level email
            "email.delivery",
            "email",
            message_id or None,
            {
                "to": to,
                "template": template_name,
                "status": delivery_status,
                "sender_path": sender_path,
                **({"failure": err_meta} if err_meta else {}),
            },
        )
    return {"status": "sent", "message_id": message_id}


@task(
    name="pickready.send_email",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "delivers platform and staff mail whose tenant_id is None, so there is no "
        "tenant to confine it to"
    ),
    max_attempts_setting="delivery_max_retries",
    backoff_seconds=60.0,
    backoff_max_seconds=60.0,
    retry_on=(TransientDeliveryError,),
    bind=True,
)
def send_email(
    ctx: TaskContext,
    tenant_id: str | None,
    to: str,
    template_name: str,
    context: dict,
    attachments: list[dict] | None = None,
    reply_to: str | None = None,
    email_log_id: str | None = None,
):
    """attachments: [{"filename": str, "content": <base64 str>}] (SMTP MIME part).

    `reply_to` is optional and TRAILING, so every existing four-argument
    dispatch keeps working unchanged -- including any message already in flight
    during a rolling deploy, which is the reason it is not inserted earlier in
    the list.

    tenant_id None = platform-level email (e.g. Owner OTP): default template,
    default SMTP sender. Interview invites and verification emails also route
    through here, so they inherit the verified-domain/default-sender selection.

    Failure handling:
      * PermanentDeliveryError → swallowed after logging + audit (no retry).
      * TransientDeliveryError → fixed 60-second delay, capped at
        settings.delivery_max_retries; audited when retries are exhausted.
    """
    async def _task():
        async with _worker_session() as session:
            return await _send_email_async(
                session, tenant_id, to, template_name, context, attachments,
                reply_to=reply_to, email_log_id=email_log_id,
            )

    try:
        return _run(_task())
    except PermanentDeliveryError as err:
        # Already logged + audited inside _send_email_async. Do NOT re-raise  - 
        # a permanent failure must not consume the retry budget.
        logger.error(
            "email.permanent_failure_final template=%s to=%s  -  not retrying. "
            "ACTION: %s", template_name, to, err.hint,
        )
        return {"status": "failed", "error": err.error_name}
    except TransientDeliveryError:
        # `runtime.run_task` owns the backoff and the cap. This branch records
        # the TERMINAL failure, so it must fire on the last attempt and on no
        # other: auditing every attempt would report one undelivered email
        # several times and make the audit log disagree with itself about how
        # many were lost.
        if ctx.is_final_attempt:
            _run(
                _audit_delivery_exhausted(
                    tenant_id, to, template_name, "email"
                )
            )
        raise


# ── Lifecycle emails (spec §6) ───────────────────────────────────────────────

async def _send_lifecycle_email_async(session: AsyncSession, email_log_id: str) -> dict:
    """Deliver one already-recorded candidate email and settle its log row.

    The row exists before this runs (`services/email_outbox` is its one
    writer), so this task never invents content: it sends exactly what was
    recorded and then records the outcome.

    THE ROW IS CLAIMED BEFORE ANYTHING IS SENT. `email_outbox.claim` moves it
    from `queued` to `processing` in one conditional UPDATE and the claim is
    COMMITTED before the transport is called, so two invocations for one row
    (a redelivery, a re-dispatch by `reconcile_queued_emails`, a double
    dispatch) cannot both send it: the loser finds no row to claim and does
    nothing. Reading the status and then sending, as this used to, let both
    through. A transient failure puts the row back to `queued` for the retry;
    every other outcome is terminal.
    """
    from app.models.conversation import DELIVERY_FAILED, DELIVERY_SENT
    from app.models.email_log import (
        STATUS_FAILED,
        STATUS_QUEUED,
        STATUS_SENT,
        EmailLog,
    )
    from app.services import conversations, email_outbox
    from app.services.lifecycle_email import to_html

    row_id = uuid.UUID(str(email_log_id))
    claimed = await email_outbox.claim(session, row_id)
    await session.commit()
    row = await session.get(EmailLog, row_id, populate_existing=True)
    if row is None:
        logger.warning("lifecycle_email.log_row_missing id=%s", email_log_id)
        return {"status": "skipped", "reason": "log row not found"}
    if not claimed:
        logger.info(
            "lifecycle_email.already_claimed id=%s status=%s  -  not resending",
            row.id, row.status,
        )
        return {"status": row.status, "resent": False}

    # ── The sender validation chokepoint (Corporate Email System spec
    #    sections 10 and 11) ─────────────────────────────────────────────────
    # When the row carries a sender, the sender is re-loaded HERE, at send
    # time, not trusted from queue time. That is what makes revocation take
    # effect for already-queued emails: a sender revoked between queue and
    # send fails the message honestly rather than sending under an identity
    # the client has withdrawn.
    sender = None
    if row.sender_id is not None:
        from app.models.email_sender import SENDER_ACTIVE, ClientEmailSender

        sender = await session.get(ClientEmailSender, row.sender_id)
        if sender is None or sender.status != SENDER_ACTIVE:
            reason = (
                "Sender was removed" if sender is None
                else f"Sender is {sender.status.replace('_', ' ')}, not active"
            )
            row.status = STATUS_FAILED
            row.error = f"Refused at send time: {reason}"
            await email_outbox.settle_thread_message(
                session, row.id, state=DELIVERY_FAILED, detail=row.error
            )
            await session.commit()
            await _audit(
                session, str(row.tenant_id), "lifecycle_email_sender_refused",
                "email_log", str(row.id),
                {
                    "email_type": row.email_type,
                    "sender_id": str(row.sender_id),
                    "sender_status": sender.status if sender is not None else "missing",
                },
            )
            return {"status": "failed", "error": "sender_not_active"}

    # THE THREAD'S OWN ADDRESS, when this email belongs to a conversation, so
    # an emailed answer lands in the thread rather than in a mailbox nobody
    # watches. None when the deployment receives no mail (pilot, 2026-09):
    # the transport then keeps its previous Reply-To.
    reply_to = None
    if row.conversation_id is not None:
        token = (
            await session.execute(
                text("SELECT thread_token FROM conversations WHERE id = :cid"),
                {"cid": str(row.conversation_id)},
            )
        ).scalar_one_or_none()
        if token:
            reply_to = conversations.reply_address(token)

    try:
        provider_message_id = await _deliver_email(
            to=row.recipient_email,
            subject=row.subject,
            html=to_html(row.body),
            text=row.body,
            sender=sender,
            reply_to=reply_to,
            # Correlation for the SES event that comes back minutes later.
            # `notification_id` IS the email_log row id: that is the record an
            # event has to find, so naming anything else here would leave the
            # tag pointing at something the webhook does not look up.
            correlation={
                "tenant_id": row.tenant_id,
                "sender_id": row.sender_id,
                "candidate_id": row.candidate_id,
                "job_id": row.job_id,
                "notification_id": row.id,
            },
        )
    except PermanentDeliveryError as err:
        # Terminal: record it and do NOT re-raise, so the retry budget is not
        # burned on something that can never succeed.
        row.status = STATUS_FAILED
        row.error = err.error_name
        # STAMPED ON THE FAILURE PATH TOO. A row that failed still went out
        # over a transport, and knowing which one is most of the diagnosis.
        row.failed_at = datetime.now(timezone.utc)
        row.transport = get_settings().email_transport
        await email_outbox.settle_thread_message(
            session, row.id, state=DELIVERY_FAILED, detail=err.error_name
        )
        await session.commit()
        log_delivery_error("lifecycle_email", err)
        await _audit(
            session, str(row.tenant_id), "lifecycle_email_failed",
            "email_log", str(row.id),
            {"email_type": row.email_type, "error": err.error_name},
        )
        return {"status": "failed", "error": err.error_name}
    except TransientDeliveryError:
        # RELEASE THE CLAIM so the retry can take it again; the runtime owns
        # the backoff. Committed before re-raising, or the retry would find
        # the row still `processing` and do nothing.
        row.status = STATUS_QUEUED
        row.claimed_at = None
        await session.commit()
        raise

    row.status = STATUS_SENT
    row.sent_at = datetime.now(timezone.utc)
    # RECORDED PER ROW, never inferred later from the current setting: a
    # deployment that switches transport would otherwise relabel history, and
    # `sent` means different things under each (terminal under smtp, awaiting
    # a delivery event under ses).
    row.transport = get_settings().email_transport
    row.error = None
    # The provider id is what a later SES delivery/bounce/complaint event is
    # matched back on (spec section 8). The SMTP Message-ID is recorded too;
    # Gmail simply never reports events against it.
    row.provider_message_id = provider_message_id
    await email_outbox.settle_thread_message(
        session, row.id, state=DELIVERY_SENT, detail=None
    )
    await session.commit()
    await _audit(
        session, str(row.tenant_id), "lifecycle_email_sent",
        "email_log", str(row.id),
        # Recipient address and body are never written to audit metadata
        # (ESD §16)  -  the email_log row is the record of the content.
        {"email_type": row.email_type, "edited_by_human": row.edited_by_human},
    )
    return {"status": "sent"}


@task(
    name="pickready.send_lifecycle_email",
    route=Route.LAMBDA,
    rls="tenant",
    max_attempts_setting="delivery_max_retries",
    backoff_seconds=60.0,
    backoff_max_seconds=60.0,
    retry_on=(TransientDeliveryError,),
    bind=True,
)
def send_lifecycle_email(ctx: TaskContext, email_log_id: str):
    """Send one of the six lifecycle emails from its `email_log` row."""
    async def _task():
        tenant_id = await _resolve_tenant_id("email_log", email_log_id)
        if tenant_id is None:
            logger.warning("lifecycle_email.log_row_missing id=%s", email_log_id)
            return {"status": "skipped", "reason": "log row not found"}
        async with _tenant_worker_session(tenant_id) as session:
            return await _send_lifecycle_email_async(session, email_log_id)

    try:
        return _run(_task())
    except TransientDeliveryError:
        if ctx.is_final_attempt:
            # Retries exhausted  -  settle the row as failed so the log never
            # leaves a message stuck in `queued` forever.
            async def _mark_failed():
                from app.models.email_log import STATUS_FAILED, STATUS_QUEUED, EmailLog

                tenant_id = await _resolve_tenant_id("email_log", email_log_id)
                if tenant_id is None:
                    return
                async with _tenant_worker_session(tenant_id) as session:
                    row = await session.get(EmailLog, uuid.UUID(str(email_log_id)))
                    if row is not None and row.status == STATUS_QUEUED:
                        from app.models.conversation import DELIVERY_FAILED
                        from app.services import email_outbox

                        row.status = STATUS_FAILED
                        row.error = "Delivery retries exhausted"
                        await email_outbox.settle_thread_message(
                            session, row.id, state=DELIVERY_FAILED, detail=row.error
                        )
                        await session.commit()

            _run(_mark_failed())
        raise


async def _autosend_lifecycle_email(
    session: AsyncSession,
    link_id: str,
    email_type: str,
    extra_context: dict | None = None,
    *,
    dedupe_key: str,
) -> dict:
    """Draft and queue one of the AUTOMATIC lifecycle emails.

    Types 1 and 2 (application confirmation, assessment reminder) fire on an
    event rather than on a recruiter's click, so there is no human in the loop
    to approve the copy  -  the draft goes straight into `email_log` and out.
    `edited_by_human` is therefore False on these rows, which is exactly the
    distinction the audit trail needs to record.

    Types 3, 4 and 5 deliberately do NOT come through here: telling someone
    they were rejected, shortlisted, or put on hold is a decision a person
    makes and should read before it is sent (api/emails).

    IDEMPOTENT BY `dedupe_key`, which names the STAGE. It used to be "any row
    of this type for this application", so the 72 hour reminder always found
    the 24 hour one and was never sent. The key is checked BEFORE drafting, so
    a redelivery does not pay for a model call, and the outbox's insert is
    `ON CONFLICT DO NOTHING` on the unique key, so two concurrent runs still
    produce one row. The row carries the tenant's DEFAULT sender, resolved by
    the outbox, and is not threaded: nobody watches replies to a message
    nobody wrote.
    """
    from app.models.candidate import Candidate, JobCandidateLink
    from app.models.job import Job
    from app.services import assessment_invite, email_outbox, lifecycle_email

    link = await session.get(JobCandidateLink, uuid.UUID(str(link_id)))
    if link is None:
        return {"status": "skipped", "reason": "link not found"}
    candidate = await session.get(Candidate, link.candidate_id)
    job = await session.get(Job, link.job_id)
    if candidate is None or job is None or not candidate.email:
        return {"status": "skipped", "reason": "no recipient"}
    if await email_outbox.dedupe_key_exists(session, dedupe_key):
        return {"status": "skipped", "reason": "already sent"}

    tenant = await session.get(Tenant, link.tenant_id)
    settings = get_settings()
    frontend = settings.frontend_url.rstrip("/")
    context = {
        "candidate_name": candidate.full_name or "there",
        "job_title": job.title,
        "company_name": tenant.name if tenant else "our team",
        # Same signed link as the recruiter-drafted path. Built through the
        # one builder so a reminder and an invitation can never point at
        # different things (services/assessment_invite).
        "assessment_link": assessment_invite.assessment_link_url(
            frontend, link_id=link.id, email=candidate.email
        ),
        **(extra_context or {}),
    }
    draft = await lifecycle_email.draft(email_type, context, session=session)

    row = await email_outbox.queue_candidate_email(
        session,
        tenant_id=link.tenant_id,
        email_type=email_type,
        recipient_email=candidate.email,
        candidate_id=candidate.id,
        job_id=job.id,
        link_id=link.id,
        subject=draft["subject"],
        body=draft["body"],
        generated_by_ai=draft["generated_by_ai"],
        edited_by_human=False,      # automatic: no recruiter reviewed it
        sent_by=None,
        dedupe_key=dedupe_key,
        thread=False,
    )
    # The commit is what dispatches the send (dispatch_after_commit).
    await session.commit()
    if row is None:
        return {"status": "skipped", "reason": "already sent"}
    return {"status": "queued", "email_log_id": str(row.id)}


@task(
    name="pickready.send_application_confirmation",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "reads the candidate row, which a tenant session cannot see when a databank "
        "candidate owned by another tenant is linked to this job"
    ),
)
def send_application_confirmation(link_id: str):
    """Email type 1: confirm an application was received (spec §6.1)."""
    async def _task():
        from app.services import email_outbox

        async with _worker_session() as session:
            return await _autosend_lifecycle_email(
                session,
                link_id,
                "application_confirmation",
                dedupe_key=email_outbox.confirmation_key(link_id),
            )

    return _run(_task())


def reminder_stage_for(hours_elapsed: int) -> int:
    """The schedule stage a reminder payload with no explicit stage belongs to.

    The largest `REMINDER_SCHEDULE_HOURS` entry at or below the elapsed hours,
    which is what a payload queued before stages were explicit meant; below the
    first entry it is the first. Only such in-flight payloads reach this: the
    reconciliation passes the stage itself.
    """
    from app.services.credit_reconciliation import REMINDER_SCHEDULE_HOURS

    due = [hours for hours in REMINDER_SCHEDULE_HOURS if hours <= int(hours_elapsed)]
    return max(due) if due else REMINDER_SCHEDULE_HOURS[0]


@task(
    name="pickready.send_assessment_reminder",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "reads the candidate row, which a tenant session cannot see when a databank "
        "candidate owned by another tenant is linked to this job"
    ),
)
def send_assessment_reminder(
    link_id: str, hours_elapsed: int = 24, reminder_stage: int | None = None
):
    """Email type 2: nudge a candidate whose assessment is still unfinished.

    `reminder_stage` is the schedule entry in hours (24, then 72) and it keys
    the idempotence, so each stage sends once. It is TRAILING and optional
    because a payload queued before this release carries only the elapsed
    hours; `reminder_stage_for` derives its stage.
    """
    async def _task():
        from app.services import email_outbox

        stage = (
            int(reminder_stage)
            if reminder_stage is not None
            else reminder_stage_for(hours_elapsed)
        )
        async with _worker_session() as session:
            return await _autosend_lifecycle_email(
                session,
                link_id,
                "assessment_reminder",
                {"hours_elapsed": str(hours_elapsed)},
                dedupe_key=email_outbox.reminder_key(link_id, stage),
            )

    return _run(_task())


@task(
    name="pickready.notify_candidate_of_message",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "reads the candidate row, which a tenant session cannot see when a databank "
        "candidate owned by another tenant is linked to this job"
    ),
)
def notify_candidate_of_message(conversation_id: str, message_id: str):
    """Tell a candidate a recruiter wrote to them: one Updates entry and one
    email per burst (`services/candidate_message_notifications`). Dispatched
    after the message commits; the email's own send is dispatched by this
    task's commit."""
    async def _task():
        from app.services import candidate_message_notifications

        async with _worker_session() as session:
            outcome = await candidate_message_notifications.notify(
                session,
                conversation_id=uuid.UUID(str(conversation_id)),
                message_id=uuid.UUID(str(message_id)),
            )
            await session.commit()
            logger.info(
                "candidate_message_notification conversation=%s notified=%s "
                "emailed=%s reason=%s",
                conversation_id, outcome.notified, outcome.emailed, outcome.reason,
            )
            return outcome.as_dict()

    return _run(_task())


@task(
    name="pickready.reconcile_queued_emails",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def reconcile_queued_emails():
    """Every fifteen minutes: re-dispatch candidate emails whose send was lost.

    `email_outbox` dispatches a send after its request commits, and that invoke
    can fail after the row is durable. Such a row sits `queued` for ever, and
    "not sent" and "nothing to send" produce the same empty log. This sweep
    asks the TABLE (`email_outbox.reconcile_queued`), re-dispatches what is
    recent enough to still be worth sending, and reports at ERROR what is not
    and what is stuck mid-send. It never resends a `processing` row: that send
    may have happened, and the worker's claim is what stops a re-dispatch from
    doubling one that has not.
    """
    async def _task():
        from app.services import email_outbox

        async with _worker_session() as session:
            found = await email_outbox.reconcile_queued(session)
        for email_log_id in found["redispatch"]:
            dispatch("pickready.send_lifecycle_email", args=[email_log_id])
        if found["abandoned_queued"] or found["stuck_processing"]:
            logger.error(
                "email_outbox.needs_attention abandoned_queued=%s "
                "stuck_processing=%s  -  not resent; a person decides",
                found["abandoned_queued"], found["stuck_processing"],
            )
        result = {
            "redispatched": len(found["redispatch"]),
            "abandoned_queued": found["abandoned_queued"],
            "stuck_processing": found["stuck_processing"],
        }
        logger.info("email_outbox.reconciled %s", result)
        return result

    return _run(_task())


# ── Matching / parsing pipelines ────────────────────────────────────────────

@task(
    name="pickready.run_matching",
    route=Route.ECS,
    rls="bypass",
    rls_reason=(
        "databank discovery reads the tenant-free profiles and candidates of every "
        "consenting candidate"
    ),
    max_attempts=2,
    backoff_seconds=5.0,
    bind=True,
)
def run_matching(ctx: TaskContext, job_id: str):
    from app.services import matching, matching_progress

    # The recruiter watches these stages on the job page instead of a blocking
    # modal. They go through the SAME run-status record that carries the
    # terminal state, so the progress and the outcome come from one place: a
    # task that has finished cannot still be showing a stage as running.
    # The run id is ALSO the activity operation id. The browser holds it
    # before the task is picked up, so every payload is attributable from the
    # first poll and a second run cannot repaint this one (Case 3 section 24).
    progress = matching_progress.Progress(
        publish=ctx.publish, operation_id=ctx.run_id
    )

    async def _task():
        async with _worker_session() as session:
            scored = await matching.run_matching(session, job_id, progress=progress)
            progress.complete()
            logger.info("matching.complete job_id=%s scored=%d", job_id, scored)
            # Nothing else. This task used to re-dispatch report synthesis for
            # every completed conversation on the job with no report (PLAN-p2
            # NF-5), a side effect of ranking that duplicated
            # `pickready.release_held_assessments`, the scheduled sweep that
            # owns exactly that repair. One owner per repair.
            #
            # The FINAL stage payload is the return value, so the run-status
            # record's SUCCESS payload carries the finished stages and the
            # degraded flag. Returning nothing left SUCCESS holding None and
            # the job page redrew every finished run as an all-pending plan.
            return progress.payload()
    return _run(_task())


@task(
    name="pickready.generate_job_swot",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "one tenant's work, not yet proven under tenant_worker_session (PLAN-p7 3.3 "
        "converts only with a real-Postgres read-back test)"
    ),
)
def generate_job_swot(
    job_id: str,
    confirm_overwrite: bool = False,
    requested_by: str = "",
    requested_version: int = 0,
):
    """Bodha: draft one job's SWOT document, dispatched after the request commits.

    The request (`swot_analysis.request_generation`) has already refused over
    the team's edits and over a JD too thin to draft from, and left the row
    `generating`. This body calls the model with no lock held, then writes only
    if the row is still the one that was asked about: a human save meanwhile
    wins, and the generation stands down at INFO.

    ONE attempt. A failed generation is a STATE the team retries from the tab;
    retrying here would spend a second model call on a row the failure already
    moved to `failed`. The failure is committed, then re-raised, so the
    function's error metric moves. A lost invoke needs no sweep: a
    `generating` row past `swot_generation_stale_minutes` reads as failed.

    RLS: registered `rls="bypass"` until a real-Postgres read-back test proves
    it under `tenant_worker_session`; it reads and writes one tenant's job and
    SWOT row, so it is the next candidate for conversion.
    """
    from app.models.job import Job
    from app.services import swot_analysis
    from app.services.audit import record_agent_action

    async def _task():
        async with _worker_session() as session:
            job = await session.get(Job, uuid.UUID(str(job_id)))
            if job is None:
                logger.info("job_setup.swot_job_gone job_id=%s", job_id)
                return
            try:
                row = await swot_analysis.run_generation(
                    session,
                    job,
                    confirm_overwrite=bool(confirm_overwrite),
                    requested_version=int(requested_version),
                )
            except swot_analysis.SwotAnalysisError:
                await session.commit()
                raise
            if row is None:
                await session.commit()
                return
            if requested_by:
                from app.models.user import User

                principal = await session.get(User, uuid.UUID(str(requested_by)))
                # RBAC 34: an AI-initiated mutation names BOTH the human who
                # asked for it and the agent that did it, in ONE insert.
                await record_agent_action(
                    session,
                    action="job_swot_analysis_generated",
                    agent_name="bodha",
                    principal_user_id=uuid.UUID(str(requested_by)),
                    principal_role=(
                        None if principal is None
                        else getattr(principal.role, "value", principal.role)
                    ),
                    tenant_id=job.tenant_id,
                    resource_type="job",
                    resource_id=job.id,
                    job_id=job.id,
                    correlation_id=job.correlation_id,
                    metadata={
                        "version": row.version,
                        "replaced_human_edits": bool(confirm_overwrite and row.human_edited),
                    },
                )
            await session.commit()
            logger.info("job_setup.swot_generated job_id=%s version=%d", job_id, row.version)
    _run(_task())


@task(
    name="pickready.draft_job_skills",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "one tenant's work, not yet proven under tenant_worker_session (PLAN-p7 3.3 "
        "converts only with a real-Postgres read-back test)"
    ),
    max_attempts=2,
    backoff_seconds=5.0,
)
def draft_job_skills(
    job_id: str,
    requested_swot_version: int | None = None,
    mode: str = "initial",
    confirm_overwrite: bool = False,
    requested_by: str | None = None,
):
    """Sutra: draft a job's skills from its JD, with the saved SWOT as
    optional context (CONTRACT v10).

    Dispatched after the commit by `skills.request_draft`: a job created with
    a JD, a JD saved while the job has no skill rows of any kind, the "Draft
    skills" action (or a re-draft the team confirmed), or
    `pickready.reconcile_job_setup` repairing a lost one. A SWOT save never
    dispatches it.

    ONE model call, no lock held across it, then the writes under the skills
    lock after re-checking everything the call may have raced (a candidate
    start, a newer request, the team's own edits). A FIRST draft on a job that
    already has rows is a no-op, which is what makes a redelivered message
    harmless. A failure is the `failed` state with zero rows: there is no
    template draft. Two attempts cover a database blip between the model call
    and the commit; a second attempt after a recorded failure stands down,
    because the state is no longer `drafting`.

    RLS: bypass, like every task today; it reads and writes one tenant's job,
    SWOT and skills.
    """
    from app.models.job import Job
    from app.services import skills
    from app.services.hiring import pipeline_halt, sutra

    async def _task():
        async with _worker_session() as session:
            job = await session.get(Job, uuid.UUID(str(job_id)))
            if job is None:
                logger.info("job_setup.skills_job_gone job_id=%s", job_id)
                return
            try:
                outcome = await skills.draft(
                    session,
                    job,
                    mode=mode,
                    requested_swot_version=requested_swot_version,
                    confirm_overwrite=bool(confirm_overwrite),
                    requested_by=uuid.UUID(str(requested_by)) if requested_by else None,
                )
            except sutra.SutraUnavailable:
                await session.commit()
                raise
            except pipeline_halt.PipelineHalted:
                # Already logged and audited by `pipeline_halt.enforce`. Not
                # retried and not a failure of the draft: an operator stopped
                # the stage. The job stays `drafting` and the sweep offers it
                # again once the halt is cleared.
                await session.rollback()
                return
            await session.commit()
            logger.info(
                "job_setup.skills_draft job_id=%s outcome=%s skills=%d",
                job_id, outcome.outcome, outcome.skills,
            )
    _run(_task())


@task(
    name="pickready.reconcile_job_setup",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def reconcile_job_setup():
    """Find every job whose skills draft never landed, and ask again.

    THE RULE THIS ENFORCES: a timestamp is not evidence that work happened, and
    neither is the ABSENCE of rows, because deletion here is soft.

    It selects a job only when it HAS A JD (CONTRACT v10: the skills are
    drafted from the JD, and the SWOT is optional context, so a saved SWOT is
    no longer asked for) and either

      * no draft was ever asked for (`skills_draft_status = 'not_started'`) and
        it has ZERO `job_competencies` rows of ANY kind, active or not; or
      * a draft was asked for and never reported back (`drafting`, requested
        longer ago than `skills.DRAFT_STALE_AFTER`).

    A FROZEN JOB IS NEVER SELECTED: its skills are the contract, and a draft
    would be refused anyway. A JD too thin to draft from is recorded as the
    draft's failed state (`skills.after_jd_saved`'s rule), so the job stops
    being selected until somebody saves a JD.

    A JOB WHOSE ROWS ARE ALL SOFT-DELETED IS NEVER SELECTED. The previous
    version asked for jobs with no ACTIVE row, so a hiring manager who removed
    every generated item had Sutra put them back fifteen minutes later (audit
    #8). A set a person emptied is a decision, not a missing draft.

    Every dispatch goes through `skills.request_draft`, the same entry point a
    human save takes, after this sweep's own commit. Bounded per tick; the next
    tick picks up where this one stopped. Deliberately NOT scoped to a tenant:
    the failure this repairs was never tenant-specific.

    RLS: bypass, because it iterates every tenant.
    """
    from app.models.assessment import JobCompetency
    from app.models.job import SKILLS_DRAFT_DRAFTING, SKILLS_DRAFT_NOT_STARTED, Job
    from sqlalchemy import func

    from app.models.job_skill_snapshot import JobSkillSnapshot
    from app.services import skills

    #: Bounded per tick. Each selected job costs one dispatch here and at most
    #: one model call in its own invocation.
    BATCH = 25

    async def _task():
        async with _worker_session() as session:
            any_row = (
                select(JobCompetency.id).where(JobCompetency.job_id == Job.id).exists()
            )
            frozen = (
                select(JobSkillSnapshot.id).where(JobSkillSnapshot.job_id == Job.id).exists()
            )
            jobs = (
                await session.execute(
                    select(Job)
                    .where(
                        Job.archived_at.is_(None),
                        # A JD to draft from. The SQL asks only that the
                        # document is not blank; whether it is long enough is
                        # the ONE Python gate below, never restated here.
                        func.length(func.btrim(func.coalesce(Job.jd_markdown, ""))) > 0,
                        ~frozen,
                        (
                            (Job.skills_draft_status == SKILLS_DRAFT_NOT_STARTED) & ~any_row
                        )
                        | (
                            (Job.skills_draft_status == SKILLS_DRAFT_DRAFTING)
                            & (
                                Job.skills_draft_requested_at.is_(None)
                                | (
                                    Job.skills_draft_requested_at
                                    < skills.stale_drafting_cutoff()
                                )
                            )
                        ),
                    )
                    # NEWEST first. Every job this tick examines leaves the
                    # selectable set (it is `drafting`, or `failed` with a
                    # sentence), so no job is starved; newest first means a
                    # job whose create-time draft was lost is repaired on the
                    # next tick even while an older backlog (the published
                    # jobs that predate CONTRACT v10) is still being worked
                    # through, BATCH at a time.
                    .order_by(Job.created_at.desc(), Job.id)
                    .limit(BATCH)
                )
            ).scalars().all()
            if not jobs:
                logger.debug("job_setup.reconcile_noop")
                return
            queued = 0
            skipped: dict[str, int] = {}
            for job in jobs:
                # The one definition of "a JD to draft from"
                # (`generation_sufficiency.skills_draft_input_state`). A thin
                # JD is RECORDED as the draft's failed state with the fixed
                # sentence, which is also what stops this sweep selecting the
                # job again every tick.
                if await skills.record_thin_jd(session, job):
                    skipped["jd_too_thin"] = skipped.get("jd_too_thin", 0) + 1
                    continue
                try:
                    handle = await skills.request_draft(
                        session, job, requested_by=None, confirm_overwrite=False
                    )
                except (skills.SkillsLocked, skills.SkillsError) as refusal:
                    # A lost draft this sweep may not repeat (the skills are
                    # locked now, or a re-draft would replace the team's own
                    # skills without their confirmation) is FINISHED as failed,
                    # so it reads as "draft again" and stops being selected.
                    reason = type(refusal).__name__
                    skipped[reason] = skipped.get(reason, 0) + 1
                    await skills.abandon_lost_draft(session, job)
                    continue
                if handle is not None:
                    queued += 1
            await session.commit()
            logger.info(
                "job_setup.reconciled examined=%d queued=%d skipped=%s",
                len(jobs), queued, skipped or "{}",
            )
    _run(_task())


@task(
    name="pickready.run_functional_assessment",
    route=Route.ECS,
    rls="bypass",
    rls_reason="reads the tenant-free candidate row and profile",
    max_attempts=2,
    backoff_seconds=5.0,
)
def run_functional_assessment(link_id: str):
    """Score the conversation and write the report.

    HELD, NOT FAILED, WHEN THE CREDIT POOL IS EMPTY (spec §11)
    ----------------------------------------------------------
    A candidate already inside an active conversation when the pool hits zero is
    not cut off mid-session: that one conversation runs to completion. Its
    credit is drawn at finalisation exactly as any other completion is, and if
    the pool is still at zero when finalisation is reached, finalisation itself
    is blocked pending top-up.

    "Blocked" means the report is not written and the task RETURNS, rather than
    raising. Raising would burn the five autoretries against a condition no
    retry can fix and then dead-letter the work permanently; returning leaves a
    completed conversation with no report, which is precisely the state
    `pickready.release_held_assessments` looks for when a bundle is purchased.

    Nothing is lost by waiting. The transcript is the evidence and it is already
    stored; the report is written from it whenever the customer tops up.

    ONE RUN PER APPLICATION, ENFORCED ACROSS PROCESSES
    ---------------------------------------------------
    Five call sites dispatch this task: the assessment route, proctoring
    ingestion, the video pipeline, the credit-hold release sweep and the
    reconciler. `Route.ECS` starts one Fargate container per dispatch, so two of
    them firing for one application are two processes sharing nothing but the
    database, and `services/coalescing` cannot see across that boundary by its
    own stated design.

    `uq_functional_report_link` already stops two REPORTS existing. What it does
    not stop is the cost and the damage of getting there. Both runs spend Miti's
    five evaluators and Siddhi's synthesis before the constraint fires at
    COMMIT; the loser then raises, and `max_attempts=2` runs the entire chain
    again; and on that retry the row EXISTS, so the writer takes its UPDATE
    branch and rewrites a report that may already have been delivered. Reports
    are immutable in this product (insert-only since migration 0130), so a
    race rewriting one in place is that rule failing without a sound.

    So the second run RETURNS. That is not an error and not a degradation: the
    first run is doing exactly what the second came to do. The lock is
    transaction scoped, released by this task's own commit or by the rollback
    that replaces it, with no `finally` to forget and no leak when a container
    is killed mid-run.

    SINCE THE VIVEKIUM RELEASE (PLAN-p5 WP5-D) the writer has no UPDATE branch
    at all: the report is INSERT-only, refused an UPDATE by trigger and by
    REVOKE (migration 0130), and an existing report found under the lock means
    return before anything is spent. A run that could not assess a skill
    writes no report and returns; this task's own retry does not re-run it,
    the hourly `release_held_assessments` sweep does, bounded by
    `miti_not_assessed_attempts`.
    """
    from app.models.assessment import AssessmentConversation
    from app.models.candidate import JobCandidateLink
    from app.models.job import Job
    from app.services import entitlements, locks
    from app.services.assessment_pipeline import persistence
    from app.services.coding_assessment import submissions as coding_submissions
    from app.services.functional_assessment import run_assessment
    from app.workers.dispatch import dispatch_after_commit

    async def _task():
        async with _worker_session() as session:
            # BEFORE the work, never after. A lock taken after the model calls
            # would report a duplicate rather than prevent one.
            if not await locks.try_advisory_lock(session, locks.SCORING, link_id):
                logger.info(
                    "functional_assessment.already_running link_id=%s "
                    "another run holds the scoring lock, returning",
                    link_id,
                )
                return
            link = await session.get(JobCandidateLink, uuid.UUID(str(link_id)))
            if link is None:
                raise ValueError(f"Application {link_id} not found")
            # A SECOND RUN IS A NO-OP (CONTRACT v1 "Grading"). The report is
            # immutable and insert-only; under the lock, an existing one means
            # the work is done, and a redelivered dispatch, the retry sweep or
            # a coding completion racing the conversation's own dispatch all
            # land here and return.
            if await persistence.report_exists(session, link.id):
                logger.info(
                    "functional_assessment.already_written link_id=%s", link_id
                )
                return
            job = await session.get(Job, link.job_id)
            conversation = (
                await session.execute(
                    select(AssessmentConversation).where(
                        AssessmentConversation.job_candidate_link_id == link.id
                    )
                )
            ).scalars().first()
            # SCORING WAITS FOR AN OWED CODING ANSWER (CONTRACT v2 P4, v8).
            # Answering the last question completes the conversation and
            # dispatches scoring while a final coding submission may still be
            # executing. Held, not failed: the submission's own completion
            # dispatches scoring again (`coding_submissions` hands it over when
            # the last open one closes), and once every open one is older than
            # `coding_execution_max_wait_hours` this proceeds and the evidence
            # reads unavailable, which Miti states as Not assessed.
            if conversation is not None:
                hold = await coding_submissions.scoring_hold(
                    session, conversation_id=conversation.id
                )
                if hold.hold:
                    logger.info(
                        "functional_assessment.held_for_coding link_id=%s open=%d",
                        link_id, hold.open_submissions,
                    )
                    return
            if await entitlements.restriction_reason(
                session, link.tenant_id, entitlements.ACTION_BILLABLE_AI_WORK
            ) is not None:
                logger.warning(
                    "functional_assessment.held_pending_credits link_id=%s tenant_id=%s",
                    link_id, link.tenant_id,
                )
                return
            result = await run_assessment(session, job, link)
            if result.report_id is not None:
                # The proctoring report is written AFTER the PRISM Report and
                # by a separate task, so the two never race on the same rows
                # and a proctoring failure can never take the assessment
                # report with it. After commit: a rolled-back report
                # dispatches nothing.
                dispatch_after_commit(
                    session, "pickready.generate_proctoring_report", args=[str(link_id)]
                )
            await session.commit()
    _run(_task())


@task(
    name="pickready.purge_proctoring_events",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def purge_proctoring_events():
    """Hourly retention sweep over `proctoring_events` (proctoring spec 5).

    `proctoring_event_retention_days` is zero by default, and zero means the
    platform's existing candidate-data policy applies: events leave with the
    tenant cascade and nothing is deleted early. The task then logs that and
    does nothing, so an operator reading the worker log can see the policy
    that is in force rather than inferring it from silence.
    """
    from datetime import timedelta

    from sqlalchemy import delete

    from app.models.proctoring import ProctoringEvent
    from app.services.proctoring.config import get_config

    async def _task():
        days = get_config().event_retention_days
        if days <= 0:
            logger.info(
                "proctoring.purge_noop retention follows the platform cascade policy"
            )
            return
        async with _worker_session() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            result = await session.execute(
                delete(ProctoringEvent).where(ProctoringEvent.occurred_at < cutoff)
            )
            await session.commit()
            logger.info(
                "proctoring.purged events=%d older_than_days=%d", result.rowcount, days
            )
    _run(_task())


@task(
    name="pickready.purge_closed_job_assessments",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def purge_closed_job_assessments():
    """Delete a closed job's assessment data once its thirty days are up.

    Change request 22, owner ruling 2026-09-22, which REVERSED the 2026-09-18
    ruling that `POST /jobs/{id}/close` deleted inline. Closure now withholds
    and schedules; this is the half that makes "thirty days" a fact rather
    than a sentence in a dialog. Without it the data is retained for ever and
    the promise made to every assessed candidate is quietly broken, silently,
    because a retention window with no sweep behind it produces exactly the
    same empty log as one with nothing to delete.

    IT ASKS THE TABLE (`assessment_purge_due_at` past and
    `assessment_purged_at` still null), never a last-swept stamp on something
    else (rule 8). That is what makes it idempotent: a job the purge finished
    is never enumerated again, and a job whose object store refused comes back
    on the very next run with its attempt counter one higher.

    ONE JOB PER TRANSACTION, COMMITTING AS IT GOES, the rule the candidate
    erasure sweep and the media sweep both follow: a single transaction over
    the whole backlog would roll back row deletions whose S3 objects the store
    has already destroyed, leaving reports that point at media that is gone.

    NEVER GIVES UP and has no attempt ceiling, for the reason
    `services/deletion_requests` states in place. Past
    `ATTEMPTS_BEFORE_ALARM` the log line becomes an error, so a job the object
    store keeps refusing is loud rather than merely present.
    """
    from app.services import deletion_requests, job_assessment_retention

    async def _task():
        purged = incomplete = 0
        async with _worker_session() as session:
            due = await job_assessment_retention.jobs_due_for_purge(session)
            for job in due:
                outcome = await job_assessment_retention.purge_job(session, job)
                job_id = job.id
                attempts = job.assessment_purge_attempts
                failure = job.assessment_purge_last_failure
                await session.commit()
                if outcome.completed:
                    purged += 1
                    continue
                incomplete += 1
                message = (
                    "job_assessment_retention.unfinished job_id=%s remaining=%d "
                    "attempts=%d failure=%s"
                )
                if attempts >= deletion_requests.ATTEMPTS_BEFORE_ALARM:
                    logger.error(
                        message, job_id, outcome.media_remaining, attempts, failure
                    )
                else:
                    logger.warning(
                        message, job_id, outcome.media_remaining, attempts, failure
                    )
            if due:
                logger.info(
                    "job_assessment_retention.swept due=%d purged=%d incomplete=%d",
                    len(due), purged, incomplete,
                )
    _run(_task())


@task(
    name="pickready.release_held_assessments",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "a sweep: with no tenant argument one run reads every tenant's held assessments"
    ),
    max_attempts=3,
)
def release_held_assessments(tenant_id: str | None = None):
    """Finish every report that was held for want of credits (spec §11).

    A held assessment is a COMPLETED conversation whose application has no
    report. That is derived rather than flagged, deliberately: a status column
    would have to be written in the same transaction as the hold and cleared in
    the same transaction as the release, and either write failing would strand
    the report in a state nothing sweeps. The absence of the report IS the state.

    Enqueued when a credit bundle is granted, so a customer who tops up sees
    their pending reports appear rather than having to ask why they are missing.
    Also safe to run on a schedule or by hand: `run_functional_assessment` is
    idempotent and re-checks the balance itself, so releasing a tenant who is
    still at zero re-holds every one of them and changes nothing.

    IT IS ALSO THE RETRY OF A NOT-ASSESSED RUN (PLAN-p5 P5-D4). A run in which
    a skill could not be assessed writes no report and records the attempt on
    the live evaluation row, so the application still matches this query and
    is re-dispatched here. The bound lives in the scoring run, not here: on
    attempt `miti_not_assessed_attempts` the report is written with those
    skills stated "Not assessed", which removes the application from this
    query for good.
    """
    from app.models.assessment import AssessmentConversation, FunctionalSkillsReport
    from app.services import entitlements

    async def _task():
        async with _worker_session() as session:
            query = (
                select(AssessmentConversation.job_candidate_link_id,
                       AssessmentConversation.tenant_id)
                .outerjoin(
                    FunctionalSkillsReport,
                    FunctionalSkillsReport.job_candidate_link_id
                    == AssessmentConversation.job_candidate_link_id,
                )
                .where(
                    AssessmentConversation.status == "completed",
                    FunctionalSkillsReport.id.is_(None),
                )
            )
            if tenant_id:
                query = query.where(
                    AssessmentConversation.tenant_id == uuid.UUID(str(tenant_id))
                )
            rows = (await session.execute(query)).all()

            released = 0
            checked: dict[uuid.UUID, bool] = {}
            for link_id, row_tenant in rows:
                if row_tenant not in checked:
                    checked[row_tenant] = (
                        await entitlements.restriction_reason(
                            session, row_tenant, entitlements.ACTION_BILLABLE_AI_WORK
                        )
                        is None
                    )
                if not checked[row_tenant]:
                    continue
                dispatch(
                    "pickready.run_functional_assessment", args=[str(link_id)]
                )
                released += 1
            logger.info(
                "credits.held_assessments_released tenant_id=%s found=%d released=%d",
                tenant_id, len(rows), released,
            )
    _run(_task())


@task(
    name="pickready.remind_unsaved_skills",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def remind_unsaved_skills():
    """One reminder per job whose drafted skills nobody has saved.

    REPLACES the technical-questions reminder, which kept a name describing
    a bank deleted on 2026-08-06, chased a matrix nobody reads any more, mailed
    people by ROLE rather than by what they may do, and linked to a setup page
    that does not exist.

    Selects jobs that are not archived, whose Sutra draft landed
    (`skills_draft_status = 'drafted'`) more than
    `settings.skills_setup_reminder_hours` ago, whose skills are not saved
    (`framework_approved_at IS NULL`, reused as "skills saved at"), and that
    were never reminded (`question_reminder_sent_at`, persisted name reused).
    Until the skills are saved the job can take applications and invite
    nobody, and nothing else on the screen says why.

    RECIPIENTS BY CAPABILITY, never by role name: every active user of the
    tenant for whom `rbac.authorize(FINALIZE_ROLE_DEFINITION)` on THIS job
    answers allowed, which honours the per-user overlay, the invariant ceiling
    and the Hiring Manager's assignment scope. Somebody whose access was pinned
    off stops being mailed without anybody editing this task.

    RLS: bypass, because it iterates every tenant.
    """
    from datetime import timedelta

    from app.models.enums import UserStatus
    from app.models.job import SKILLS_DRAFT_DRAFTED, Job
    from app.models.user import User
    from app.services import capabilities, rbac

    async def _task():
        threshold = datetime.now(timezone.utc) - timedelta(
            hours=get_settings().skills_setup_reminder_hours
        )
        async with _worker_session() as session:
            jobs = (
                await session.execute(
                    select(Job).where(
                        Job.archived_at.is_(None),
                        Job.skills_draft_status == SKILLS_DRAFT_DRAFTED,
                        Job.framework_approved_at.is_(None),
                        Job.skills_drafted_at.isnot(None),
                        Job.skills_drafted_at <= threshold,
                        Job.question_reminder_sent_at.is_(None),
                    )
                )
            ).scalars().all()
            if not jobs:
                logger.debug("job_setup.skills_reminder_noop")
                return
            reminded = 0
            for job in jobs:
                resource = await rbac.load_job_resource(session, job.id)
                users = (
                    await session.execute(
                        select(User)
                        .where(
                            User.tenant_id == job.tenant_id,
                            User.status != UserStatus.disabled,
                            User.email.is_not(None),
                        )
                        .order_by(User.created_at)
                    )
                ).scalars().all()
                recipients: list[str] = []
                for user in users:
                    decision = await rbac.authorize(
                        session,
                        rbac.Principal(
                            user_id=user.id, tenant_id=user.tenant_id, role=user.role
                        ),
                        capabilities.FINALIZE_ROLE_DEFINITION,
                        resource,
                    )
                    if decision.allowed and user.email:
                        recipients.append(user.email)
                link = f"{get_settings().frontend_url}/org/jobs/{job.id}"
                for email in recipients:
                    await _send_email_async(
                        session,
                        str(job.tenant_id),
                        email,
                        "outreach_direct",
                        {
                            "subject": f"Skills waiting to be saved, {job.title}",
                            "body": (
                                f"The skills for {job.title} were drafted and have "
                                "not been saved. No candidate can be invited to the "
                                "assessment until they are. Review them, change "
                                f"anything that is wrong, and save them: {link}"
                            ),
                        },
                    )
                job.question_reminder_sent_at = datetime.now(timezone.utc)
                reminded += 1
                logger.info(
                    "job_setup.skills_reminder job_id=%s recipients=%d",
                    job.id, len(recipients),
                )
            await session.commit()
            logger.info("job_setup.skills_reminders_sent jobs=%d", reminded)
    _run(_task())


@task(
    name="pickready.parse_resume",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "a profile is tenant-free (source_tenant_id, shared through the databank)"
    ),
    max_attempts=3,
)
def parse_resume(profile_id: str):
    from app.services import resume_parsing

    async def _task():
        async with _worker_session() as session:
            await resume_parsing.parse_resume(session, profile_id)
            logger.info("resume.parsed profile_id=%s", profile_id)
    _run(_task())
    # AFTER the parse, and as a SEPARATE dispatch. `resume_parsing` commits its
    # own transaction, so by here `profiles.resume_text` is durable and the
    # indexer reads the text that was actually stored rather than the text this
    # invocation happened to hold. Separate rather than inline because indexing
    # embeds, and an embedding provider outage must not turn a successful parse
    # into a retried one: the resume is parsed either way, and the hourly sweep
    # repairs an index write that never happened.
    dispatch("pickready.index_document", args=["resume", str(profile_id)])
    # Yukti reads the resume that was just stored, against every job it is
    # applied to (PLAN-p2 3.8). After the commit for the same reason as the
    # index: it reads the committed text. Separate, because a model outage in
    # the reading must not turn a successful parse into a retried one.
    dispatch("pickready.yukti_score_profile", args=[str(profile_id)])


@task(
    name="pickready.yukti_score_profile",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "a profile is tenant-free (source_tenant_id, shared through the databank)"
    ),
    max_attempts=2,
    backoff_seconds=5.0,
)
def yukti_score_profile(profile_id: str):
    """Read one parsed resume against every open job it is applied to.

    Route.LAMBDA: one resume against the jobs it is on is one model call per
    job, which is seconds, not the minutes a whole pool takes
    (`pickready.run_matching`, Route.ECS). A model failure is recorded on the
    link as "Not assessed" by Yukti itself and is NOT a task failure, so the
    retry here is for infrastructure (the database, the dispatch), never a
    second paid reading of the same resume. Dispatched by
    `pickready.parse_resume` after its commit, and by nothing else.
    """
    from app.services import matching

    async def _task():
        async with _worker_session() as session:
            await matching.score_profile(session, profile_id)
    _run(_task())


# ── The retrieval index (RPN-AI-UP-001 W2) ───────────────────────────────────
#
# `services/rag/index.index_document` existed, was correct, and had NO CALLER
# for its entire life. So `context_chunks` was empty in every environment, and
# retrieval over an empty table returns nothing SILENTLY -- the lexical
# retriever ORs its terms and fusion tolerates an empty list, so the failure
# looks exactly like a query with no good matches. These two tasks are what
# make the index exist.

@task(
    name="pickready.index_document",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "indexes tenant-free resumes as well as one tenant's JDs and assessments"
    ),
    max_attempts=3,
    backoff_seconds=2.0,
)
def index_document(source_type: str, source_id: str):
    """Index one document into the chunk index.

    Seconds of work over one document, so Lambda. Dispatched from every place a
    document's text becomes final -- a parsed resume, a published or edited JD,
    a finished assessment -- and never run inline, because indexing embeds and
    an interactive request must not wait on an embedding provider.

    IDEMPOTENT BY CONSTRUCTION, which is what makes the retry budget safe.
    `index_document` upserts on (source_type, source_id, ordinal) and re-embeds
    only chunks whose `content_sha256` changed, so a redelivery of this message
    costs one SELECT and writes nothing.

    A document that resolves to None is a no-op and NOT a failure: the row may
    have been deleted between the dispatch and the run, the JD may still be a
    draft, the resume may not be parsed yet. Raising on those would spend three
    attempts against a state that is not going to change on its own.
    """
    from app.services.rag import index as rag_index

    async def _task():
        async with _worker_session() as session:
            result = await rag_index.index_source(
                session, source_type=source_type, source_id=uuid.UUID(str(source_id))
            )
            if result is None:
                logger.info(
                    "rag.index.nothing_to_index source_type=%s source_id=%s",
                    source_type,
                    source_id,
                )
                return
            await session.commit()
            # `degraded` is logged rather than raised: the text IS indexed and
            # the keyword half of retrieval works on it, because `content_tsv`
            # is generated by Postgres and never depended on the model. What is
            # missing is the vector, and the sweep does not repair that, so
            # this line is the only record that a chunk is lexically searchable
            # and semantically invisible.
            logger.info(
                "rag.index.written source_type=%s source_id=%s written=%d "
                "unchanged=%d deleted=%d embedded=%d degraded=%s",
                result.source_type,
                result.source_id,
                result.written,
                result.unchanged,
                result.deleted,
                result.embedded,
                result.degraded,
            )
    _run(_task())


@task(
    name="pickready.sweep_consent_lifecycle",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def sweep_consent_lifecycle():
    """Consent renewal, the final warning, and the inactivity rule (feature 8).

    THE LETTERS ALWAYS GO OUT. THE ERASURE IS GATED, AND THE SPLIT IS THE WHOLE
    DESIGN. A reminder is reversible and is in the candidate's own interest;
    permanent erasure is neither, so it runs only when
    `consent_auto_deletion_enabled` is set. Off, this task still computes who
    WOULD be erased and logs the count, which is the same shape
    `purge_proctoring_events` uses: an operator reading the worker log can see
    the policy in force rather than inferring it from silence.

    That default is not timidity. Until `last_engagement_at` has been recorded
    for longer than `consent_inactivity_months`, every dormancy answer is
    computed from REGISTRATION, so a genuinely active candidate reads as
    dormant. The measurement is safe from day one; the deletion is not.

    ONE ROW AT A TIME, COMMITTING AS IT GOES, and never one bulk statement. A
    sweep over the whole databank that failed half way through a single
    transaction would roll back the letters it had already sent, and the next
    run would send them again to everybody who had received one. Per candidate,
    the stamp and its letter land together or neither does.

    Both clocks are evaluated INDEPENDENTLY (C6): consent expiry and dormancy
    are different facts, and the erasure records WHICH applied, so a complaint
    can always be answered with the reason.
    """
    from app.core.config import get_settings
    from app.models.candidate import Candidate
    from app.services import consent_lifecycle as cl
    from app.services import consent_renewal, erasure

    async def _task():
        settings = get_settings()
        thresholds = cl.Thresholds(
            renewal_months=settings.consent_renewal_months,
            grace_days=settings.consent_grace_days,
            inactivity_months=settings.consent_inactivity_months,
        )
        deletion_armed = settings.consent_auto_deletion_enabled
        now = cl.utcnow()
        reminded = warned = erased = would_erase = 0
        dormancy_warned = 0

        async with _worker_session() as session:
            candidates = (
                await session.execute(select(Candidate))
            ).scalars().all()

            for candidate in candidates:
                consented_at = cl.consented_at_for(
                    created_at=candidate.created_at,
                    renewed_at=candidate.consent_renewed_at,
                )
                stage = cl.stage_for(
                    now=now,
                    consented_at=consented_at,
                    reminder_sent_at=candidate.consent_reminder_sent_at,
                    final_warning_sent_at=candidate.consent_final_warning_at,
                    thresholds=thresholds,
                )
                # THE INACTIVITY CLOCK NOW HAS A LETTER AND A WINDOW. It used
                # to be one boolean: dormant, erased, with no warning of any
                # kind and no way for the person to stop it. The stage is
                # gated on the warning having ACTUALLY been sent, exactly as
                # the consent stages are, so a scheduler outage delays the
                # letter instead of skipping somebody to deletion.
                dormancy = cl.dormancy_stage_for(
                    now=now,
                    last_engagement_at=cl.engagement_at_for(
                        created_at=candidate.created_at,
                        last_engagement_at=candidate.last_engagement_at,
                    ),
                    warning_sent_at=candidate.dormancy_warning_sent_at,
                    thresholds=thresholds,
                )

                reason = None
                if dormancy == cl.DORMANCY_DELETION_DUE:
                    reason = cl.REASON_DORMANT
                elif stage == cl.STAGE_DELETION_DUE:
                    reason = cl.REASON_CONSENT_EXPIRED

                if reason is not None:
                    if not deletion_armed:
                        would_erase += 1
                        continue
                    # THE ONE CANONICAL DELETION WORKFLOW. The two reasons are
                    # reasons recorded on the same erasure, not two engines:
                    # the record captures the object keys before the rows go,
                    # the cascade runs, and the objects are finished by the
                    # same resumable pass `DELETE /portal/me` uses.
                    request = await erasure.open_deletion_request(
                        session, candidate.id, reason=reason
                    )
                    receipt = await erasure.cascade_erasure(
                        session, candidate.id, reason=reason
                    )
                    await erasure.mark_rows_erased(session, request)
                    await erasure.run_object_deletion(session, request)
                    state = request.state
                    await session.commit()
                    erased += 1
                    logger.info(
                        "consent.erased candidate_id=%s reason=%s "
                        "sign_in_accounts=%d deletion_state=%s",
                        receipt.candidate_id,
                        reason,
                        receipt.sign_in_accounts_deleted,
                        state,
                    )
                    continue

                if dormancy == cl.DORMANCY_WARNING_DUE and candidate.email:
                    candidate.dormancy_warning_sent_at = now
                    await session.commit()
                    dispatch(
                        "pickready.send_email",
                        args=[
                            None,
                            candidate.email,
                            "dormancy_deletion_warning",
                            {},
                        ],
                    )
                    dormancy_warned += 1
                    # AT MOST ONE LETTER PER CANDIDATE PER SWEEP. The two
                    # clocks are independent and can both come due, and two
                    # letters about deletion in one morning reads as a fault
                    # rather than as diligence. Skipping the consent letter
                    # here costs a day, because its stage is latched on a
                    # stamp that was not written: tomorrow's sweep sends it.
                    continue

                # THE STAMP IS WRITTEN BEFORE THE LETTER IS DISPATCHED, and
                # that ordering is deliberate. `dispatch` RAISES, so a failed
                # enqueue leaves a stamp and no letter: the candidate keeps the
                # full window and is simply not written to, which costs them
                # nothing. The other order risks a letter with no stamp, and
                # the next sweep would send it again, and the one after that,
                # for ever.
                #
                # THE TOKEN IS MINTED AND STORED IN THE SAME UNIT OF WORK AS
                # THE STAMP, and its URL is built in the same expression that
                # reads the address it is sent to. That pairing is the whole
                # mitigation for the hazard the templates' own comment names:
                # a per-candidate slot in a letter the sweep sends inside a
                # loop is a slot a loop variable eventually fills with the
                # wrong person's value. Here the address and the link come
                # from one row in one statement, and there is no intermediate
                # variable that could survive an iteration.
                if stage == cl.STAGE_REMINDER_DUE and candidate.email:
                    minted = consent_renewal.mint(now=now)
                    await consent_renewal.store_token(session, candidate.id, minted)
                    candidate.consent_reminder_sent_at = now
                    await session.commit()
                    dispatch(
                        "pickready.send_email",
                        args=[
                            None,
                            candidate.email,
                            "consent_renewal_reminder",
                            {"renewal_url": consent_renewal.renewal_url(minted.token)},
                        ],
                    )
                    reminded += 1
                elif stage == cl.STAGE_FINAL_WARNING_DUE and candidate.email:
                    minted = consent_renewal.mint(now=now)
                    await consent_renewal.store_token(session, candidate.id, minted)
                    candidate.consent_final_warning_at = now
                    await session.commit()
                    dispatch(
                        "pickready.send_email",
                        args=[
                            None,
                            candidate.email,
                            "consent_final_warning",
                            {"renewal_url": consent_renewal.renewal_url(minted.token)},
                        ],
                    )
                    warned += 1

        logger.info(
            "consent.sweep reminded=%d warned=%d dormancy_warned=%d erased=%d "
            "would_erase=%d deletion_armed=%s",
            reminded,
            warned,
            dormancy_warned,
            erased,
            would_erase,
            deletion_armed,
        )
        if would_erase and not deletion_armed:
            logger.warning(
                "consent.sweep_not_armed %d candidate(s) meet a deletion "
                "threshold and NONE were erased. Set "
                "CONSENT_AUTO_DELETION_ENABLED once last_engagement_at has "
                "been recorded for longer than the inactivity window.",
                would_erase,
            )

    _run(_task())


@task(
    name="pickready.sweep_bgv_reminders",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def sweep_bgv_reminders():
    """Email 3 of the vivekium BGV flow: the day-3 non-response chase.

    Finds every verification whose request went out at least
    `verification_link_ttl_days` ago with no response and no chase yet, tells
    the CANDIDATE (with the HR address partially masked), and stamps
    `reminder_sent_at` so the letter goes exactly once. Committing per row,
    the consent sweep's rule: the stamp and its letter land together or
    neither does.

    The candidate is told rather than the recruiter because the candidate is
    the one who can act: it is their former employer, and the brief's own
    template asks them to contact that HR team directly.
    """
    from app.core.config import get_settings
    from app.services import bgv_delivery, bgv_form

    async def _task():
        ttl_days = get_settings().verification_link_ttl_days
        chased = 0
        async with _worker_session() as session:
            rows = (
                (
                    # The three days run from CONFIRMED DELIVERY, not from
                    # the send, and a bounced request is never chased: sending
                    # somebody to argue with an HR team that received nothing
                    # spends their credibility on a failure we caused. The
                    # predicate lives in bgv_delivery beside the Python clock
                    # it has to agree with, and a test drives both over the
                    # same rows.
                    await session.execute(
                        text(bgv_delivery.reminder_due_sql()),
                        {"days": ttl_days},
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                if not row["candidate_email"]:
                    continue
                dispatch(
                    "pickready.send_email",
                    args=[
                        str(row["tenant_id"]),
                        row["candidate_email"],
                        "bgv_no_response",
                        {
                            "candidate_name": row["full_name"] or "there",
                            "masked_hr_email": bgv_form.masked_email(
                                row["hr_email"]
                            ),
                        },
                    ],
                )
                await session.execute(
                    text(
                        "UPDATE bgv_verifications SET reminder_sent_at = now() "
                        "WHERE id = :vid"
                    ),
                    {"vid": str(row["id"])},
                )
                await session.commit()
                chased += 1
        logger.info("bgv.reminder_sweep chased=%d", chased)

    _run(_task())


@task(
    name="pickready.bgv_auto_maintenance",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "reads candidate_employments, the candidate's tenant-free employment history"
    ),
)
def bgv_auto_maintenance(candidate_id: str, employment_id: str):
    """Feature 5's wiring: an appended employer fires its own verification.

    Dispatched by `POST /bgv/me/employers`. For every tenant already
    verifying this candidate, opens the new employer's verification and
    sends the deterministic inquiry with its form link. Seconds of SQL and
    dispatches, so Lambda; `_worker_session` because the tenants involved
    span the candidate's whole databank reach.
    """
    from app.services import bgv_maintenance

    async def _task():
        async with _worker_session() as session:
            opened = await bgv_maintenance.auto_open_verifications(
                session,
                candidate_id=uuid.UUID(candidate_id),
                employment_id=uuid.UUID(employment_id),
            )
            await session.commit()
        logger.info(
            "bgv.auto_maintenance candidate=%s employment=%s opened=%d",
            candidate_id,
            employment_id,
            opened,
        )

    _run(_task())


# ── Erasure and learning revocation (RPN-AI-UP-001 W9.5, W3.6) ───────────────

@task(
    name="pickready.cascade_erasure",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="erases one candidate from every tenant that holds their data",
)
def cascade_erasure(
    candidate_id: str,
    actor_user_id: str | None = None,
    request_id: str | None = None,
):
    """Erase one candidate: rows, VECTORS, caches and STORED OBJECTS (W9.5).

    RESUMABLE, AND THE THIRD ARGUMENT IS WHAT MAKES IT SO. Called with a
    `request_id` it picks a `candidate_deletion_requests` record up wherever it
    was left: `pending` means the database half never ran, `rows_erased` means
    only the objects are outstanding, `completed` means somebody else finished
    it and this call is a redelivery. Called without one, as the AI runtime's
    own call sites do, it opens its own record first, so there is exactly one
    shape of erasure in the product rather than an orchestrated one and a bare
    one that forgets the files.

    A handful of statements, a Redis scan and a bounded set of object deletes,
    so Lambda.

    THE VECTORS ARE THE POINT. An embedding is not a one-way hash: published
    inversion work recovers 50 to 70% of input words from popular sentence
    embeddings, and because the model is public and queryable, a dictionary
    attack against stolen vectors is practical. An erasure that deleted the
    rows and left `profiles.embedding` behind would leave the candidate's
    resume recoverable from a column nobody thinks of as personal data.

    `worker_session` runs with `app.bypass_rls = 'on'`, which this needs: a
    candidate spans tenants via the databank, and their chunks may sit under a
    tenant the erasing operator is not scoped to.
    """
    from app.models.deletion import CandidateDeletionRequest
    from app.services import deletion_requests, erasure

    async def _task():
        async with _worker_session() as session:
            request = None
            if request_id is not None:
                request = await session.get(
                    CandidateDeletionRequest, uuid.UUID(request_id)
                )
                if request is None:
                    # LOUD, not skipped. A dispatch naming a record that does
                    # not exist means either the transaction that opened it
                    # rolled back after the enqueue, or something deleted the
                    # only thing that still knows which objects have to go.
                    # Both are worth an alarm; neither is worth pretending an
                    # erasure completed.
                    raise LookupError(
                        f"no deletion request {request_id}, so there is "
                        "nothing to resume and no record of what was owed"
                    )
            if request is None:
                request = await erasure.open_deletion_request(
                    session,
                    candidate_id,
                    reason=deletion_requests.REASON_CANDIDATE_REQUESTED,
                    requested_by_user_id=actor_user_id,
                )

            receipt = None
            if request.state == deletion_requests.STATE_PENDING:
                receipt = await erasure.cascade_erasure(
                    session, request.candidate_id, actor_user_id=actor_user_id
                )
                await erasure.mark_rows_erased(session, request)
                await session.commit()
                logger.info("erasure.cascaded candidate_id=%s", candidate_id)

            outcome = await erasure.run_object_deletion(session, request)
            state = request.state
            objects_deleted = request.objects_deleted
            objects_total = request.objects_total
            await session.commit()
            logger.info(
                "erasure.request candidate_id=%s state=%s objects=%d/%d "
                "failure=%s",
                candidate_id,
                state,
                objects_deleted,
                objects_total,
                outcome.failure,
            )
            return {
                "candidate_id": str(request.candidate_id),
                "deletion_request_id": str(request.id),
                "state": state,
                "objects_deleted": objects_deleted,
                "objects_total": objects_total,
                "receipt": receipt.as_json() if receipt is not None else None,
            }
    return _run(_task())


@task(
    name="pickready.reconcile_candidate_erasures",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def reconcile_candidate_erasures():
    """Finish every erasure that is not finished (feature 7, change 15).

    THE STATE THIS EXISTS FOR IS `rows_erased`: the person is out of the
    database and some of their stored files are not out of the object store.
    Before the deletion record existed that state was not merely unrepaired,
    it was UNDETECTABLE: the erasure was one inline transaction, the objects
    were never touched at all, and nothing anywhere held a list of what was
    owed.

    It asks the TABLE, never a timestamp, which is the rule
    `reconcile_context_index` states and the reason
    `reconcile_project_intake` exists in the same shape: a dispatch that never
    arrived leaves no trace, so the only durable record of outstanding work is
    the row that describes it.

    NEVER GIVES UP. There is no attempt ceiling and no terminal failure state,
    because "we stopped trying to delete this person's documents" is not an
    outcome this product may reach. What a persistent failure moves is the
    attempt counter, and past `ATTEMPTS_BEFORE_ALARM` the log line becomes an
    error so a stuck request is loud rather than merely present.

    ONE REQUEST AT A TIME, COMMITTING AS IT GOES, the consent sweep's rule: a
    single transaction over the whole backlog would roll back the deletions it
    had already confirmed and leave counters that disagree with the store.
    """
    from app.models.deletion import CandidateDeletionRequest
    from app.services import deletion_requests, erasure

    async def _task():
        resumed = completed = stuck = 0
        async with _worker_session() as session:
            requests = (
                await session.execute(
                    select(CandidateDeletionRequest)
                    .where(
                        CandidateDeletionRequest.state
                        != deletion_requests.STATE_COMPLETED
                    )
                    .order_by(CandidateDeletionRequest.requested_at)
                )
            ).scalars().all()

            for request in requests:
                resumed += 1
                if request.state == deletion_requests.STATE_PENDING:
                    # The rows were never erased: the process died between
                    # opening the record and running the cascade. Finish what
                    # the person asked for rather than leaving them half in.
                    await erasure.cascade_erasure(
                        session, request.candidate_id, reason=request.reason
                    )
                    await erasure.mark_rows_erased(session, request)
                await erasure.run_object_deletion(session, request)
                state = request.state
                attempts = request.deletion_attempts
                failure = request.last_failure
                candidate_id = request.candidate_id
                remaining = request.objects_total - request.objects_deleted
                await session.commit()

                if state == deletion_requests.STATE_COMPLETED:
                    completed += 1
                    continue
                stuck += 1
                message = (
                    "erasure.unfinished candidate_id=%s remaining=%d "
                    "attempts=%d failure=%s"
                )
                if attempts >= deletion_requests.ATTEMPTS_BEFORE_ALARM:
                    logger.error(
                        message, candidate_id, remaining, attempts, failure
                    )
                else:
                    logger.warning(
                        message, candidate_id, remaining, attempts, failure
                    )

        if resumed:
            logger.info(
                "erasure.reconcile resumed=%d completed=%d unfinished=%d",
                resumed,
                completed,
                stuck,
            )
    _run(_task())


@task(
    name="pickready.reconcile_context_index",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def reconcile_context_index():
    """Hourly: find documents with text and no chunks, and index them.

    IT ASKS THE TABLE, NOT A TIMESTAMP. The question is "which documents have
    no chunk rows", answered relationally with a NOT EXISTS, which is the
    question `reconcile_job_setup` learned to ask after 19 of 35 live jobs
    carried a generation timestamp and zero competency rows.

    It exists because the call sites cannot cover their own failure. A dispatch
    that was never accepted, a Lambda that died before committing, a resume
    parsed by a release that predates this task -- none of those leaves a
    trace, and an unindexed resume is invisible to retrieval forever, because
    nothing would ever ask again.

    Deliberately NOT a staleness check. `chunking.source_version` is a hash
    over Python's whitespace normalisation, and recomputing it in SQL would be
    a second implementation whose disagreement is invisible: the sweep would
    re-index everything on every pass and the only symptom would be a bill.
    Staleness is the call sites' job, into an indexer already incremental by
    content hash.
    """
    from app.services.rag import sources as rag_sources

    async def _task():
        async with _worker_session() as session:
            limit = get_settings().retrieval_index_sweep_batch
            missing = await rag_sources.pending(session, limit=limit)
            orphaned = await rag_sources.unindexable_count(session)
        for source_type, source_id in missing:
            dispatch("pickready.index_document", args=[source_type, str(source_id)])
        # Always logged, including the all-zero case. A sweep that logs nothing
        # when it finds nothing is indistinguishable from a sweep that is not
        # running, and this codebase has already paid for that once.
        logger.info(
            "rag.reconcile.swept queued=%d limit=%d unindexable=%d",
            len(missing),
            limit,
            orphaned,
        )
        if orphaned:
            logger.warning(
                "rag.reconcile.unindexable_profiles count=%d "
                "reason=resume_text_with_no_source_tenant_id",
                orphaned,
            )
    _run(_task())


# ── Project Evidence Intelligence ────────────────────────────────────────────

@task(
    name="pickready.process_candidate_project",
    route=Route.ECS,
    rls="bypass",
    rls_reason="candidate projects are candidate-owned and tenant-free",
    max_attempts=2,
    backoff_seconds=5.0,
)
def process_candidate_project(project_id: str):
    """Run the whole evidence pipeline for one submitted project.

    Idempotent by construction: derived output lives in columns on the one
    project row, a completed project returns immediately, and staging keys
    are content-addressed, so a redelivery reruns safely. Transient
    failures (storage, a provider 429) raise and use the retry budget;
    deterministic refusals (a hostile archive, a rejected repository URL) are
    recorded as terminal statuses inside the pipeline and do NOT raise.
    """
    from app.services.projects import pipeline as project_pipeline

    async def _task():
        async with _worker_session() as session:
            project = await project_pipeline.process_project(
                session, uuid.UUID(str(project_id))
            )
            await session.commit()
            logger.info(
                "project_evidence.processed project_id=%s status=%s "
                "original_deleted=%s",
                project_id,
                project.status,
                project.original_deleted_at is not None,
            )
    _run(_task())


@task(
    name="pickready.reconcile_project_intake",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def reconcile_project_intake():
    """Hourly sweeper for the two states that must not persist quietly.

    1. Temporary originals whose deletion failed: evidence is durable, the
       staged objects should be gone, so retry the verified deletion. This is
       the observability half of the brief's deletion contract -- a failed
       deletion is counted and retried, never assumed away.
    2. Projects stuck in `submitted` or `processing` for over 30 minutes: the
       task was lost (broker hiccup, worker restart mid-run), so re-enqueue.
       The pipeline is idempotent, so a duplicate enqueue costs a rerun and
       nothing else.
    3. `partially_processed` projects (deterministic evidence persisted, the
       AI interpretation missing): re-enqueue the AI-only completion, BOUNDED
       by the run counter so a permanently failing interpretation cannot loop
       forever on the platform's own budget.
    """
    from datetime import timedelta

    from sqlalchemy import or_

    from app.models.project import (
        STATUS_PARTIALLY_PROCESSED,
        STATUS_PROCESSING,
        STATUS_SUBMITTED,
        CandidateProject,
    )
    from app.services.projects import pipeline as project_pipeline

    MAX_AUTOMATIC_RUNS = 5

    async def _task():
        async with _worker_session() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
            rows = (
                await session.execute(
                    select(CandidateProject).where(
                        or_(
                            CandidateProject.processed_at.isnot(None)
                            & (CandidateProject.original_deleted_at.is_(None)),
                            CandidateProject.status.in_(
                                [STATUS_SUBMITTED, STATUS_PROCESSING]
                            )
                            & (CandidateProject.created_at < cutoff),
                            CandidateProject.status == STATUS_PARTIALLY_PROCESSED,
                        )
                    )
                )
            ).scalars().all()
            retried_deletion = 0
            requeued = 0
            for project in rows:
                if project.processed_at is not None and (
                    project.original_deleted_at is None
                ):
                    await project_pipeline.delete_intake_objects(session, project)
                    retried_deletion += 1
                elif project.status in (STATUS_SUBMITTED, STATUS_PROCESSING):
                    dispatch(
                        "pickready.process_candidate_project",
                        args=[str(project.id)],
                    )
                    requeued += 1
                elif project.status == STATUS_PARTIALLY_PROCESSED:
                    runs = int((project.telemetry_json or {}).get("runs", 0))
                    if runs < MAX_AUTOMATIC_RUNS:
                        dispatch(
                            "pickready.process_candidate_project",
                            args=[str(project.id)],
                        )
                        requeued += 1
            await session.commit()
            if retried_deletion or requeued:
                logger.info(
                    "project_evidence.reconcile deletions_retried=%d requeued=%d",
                    retried_deletion,
                    requeued,
                )
    _run(_task())


# ── Employer verification (ESD §10) ─────────────────────────────────────────

@task(
    name="pickready.reconcile_assessment_credits",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def reconcile_assessment_credits():
    """Daily sweep: charge abandoned assessments and queue due reminders.

    A completed assessment charges itself the moment it completes. An abandoned
    one has no such moment, so it is settled here once the reminder sequence has
    been exhausted (services/credit_reconciliation). Every write is idempotent,
    so a retry of this task never double-charges.
    """
    async def _task():
        from app.services import credit_reconciliation

        from app.workers.dispatch import dispatch_after_commit

        async with _worker_session() as session:

            def _queue(link_id: str, hours_elapsed: int, stage_hours: int) -> None:
                # AFTER the commit that increments `reminders_sent`: a run that
                # rolls back queues nothing, and the next run re-derives it.
                dispatch_after_commit(
                    session,
                    "pickready.send_assessment_reminder",
                    args=[link_id, hours_elapsed, stage_hours],
                )

            result = await credit_reconciliation.reconcile(session, queue_reminder=_queue)
            await session.commit()
            return result.as_dict()

    return _run(_task())


@task(
    name="pickready.send_credit_warning_email",
    route=Route.LAMBDA,
    rls="tenant",
)
def send_credit_warning_email(tenant_id: str, level: int):
    """The Part 5 §4 balance warnings: LOW at 20 credits, CRITICAL at 10.

    Enqueued by `credits._sync_warning_flags` exactly once per tier per
    purchase cycle. The estimate is computed HERE, at send time, so the figure
    in the email matches the balance the deduction left behind rather than a
    stale snapshot from whenever the task queued.
    """
    async def _task():
        async with _tenant_worker_session(uuid.UUID(str(tenant_id))) as session:
            from app.services import credits as credits_service

            row = (
                await session.execute(
                    text(
                        "SELECT u.email, t.name FROM users u JOIN tenants t ON t.id = u.tenant_id "
                        "WHERE u.tenant_id = :tid AND u.role = 'client' "
                        "AND u.status <> 'disabled' AND u.email IS NOT NULL "
                        "ORDER BY u.created_at LIMIT 1"
                    ),
                    {"tid": tenant_id},
                )
            ).mappings().first()
            if row is None:
                logger.warning(
                    "billing.credit_warning_email_no_recipient tenant=%s", tenant_id
                )
                return {"sent": False, "reason": "no recipient"}

            tid = uuid.UUID(tenant_id)
            balance = await credits_service.balance_subunits(session, tid)
            average = await credits_service.average_credits_per_assessment(session, tid)
            estimate = credits_service.estimated_assessments_remaining(balance, average)
            stem_note = ""
            dispatch(
                "pickready.send_email",
                args=[
                    tenant_id,
                    row["email"],
                    "credit_warning_critical" if level >= 2 else "credit_warning_low",
                    {
                        "company_name": row["name"],
                        "balance_credits": str(
                            credits_service.credits_from_subunits(balance)
                        ),
                        "estimated_assessments": str(estimate),
                        "stem_note": stem_note,
                        "billing_url": f"{get_settings().frontend_url}/org/billing",
                    },
                ],
            )
            return {"sent": True, "level": level}

    return _run(_task())


@task(
    name="pickready.expire_credit_lots",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
)
def expire_credit_lots(limit: int = 500):
    """Materialise every credit lot that has passed its expiry.

    Change request 25. A lot reaching its expiry writes an `expiry` ledger
    debit for whatever was left on it, so `balance = SUM(subunits_delta)`
    stays the single definition of the balance and the statement says why it
    fell. `credit_lots.expire_due` is also called by every gate and by the
    billing summary, so an ACTIVE customer's balance is already exact when
    they look at it. This sweep exists for the customer nobody is looking at:
    without it their balance would overstate until the next time somebody
    happened to read it, and the Provider Portal's cross-tenant overview would
    be reading those stale figures.

    Asks the TABLE, never a "last swept" stamp: a sweep keyed on a timestamp
    skips exactly the tenant whose previous run died between the stamp and the
    work. Idempotent per lot three times over; see `expire_due`.

    DAILY, and the interval is not load-bearing: expiry is materialised on
    every read and every deduction, so running late costs a stale number on an
    idle account and can never let an expired credit be spent.

    Committing per tenant, the consent sweep's rule: a tenant's lot zeroing
    and its ledger debit land together or neither does, and one tenant's
    failure does not discard the work already done for the others.
    """
    from app.services import credit_lots

    async def _task():
        swept = 0
        subunits = 0
        async with _worker_session() as session:
            tenant_ids = await credit_lots.tenants_with_due_lots(session, limit)
            for tenant_id in tenant_ids:
                expired = await credit_lots.expire_due(session, tenant_id)
                await session.commit()
                if expired:
                    swept += 1
                    subunits += expired
        logger.info(
            "credits.expiry_sweep tenants=%d subunits=%d", swept, subunits
        )
        return {"tenants": swept, "subunits": subunits}

    return _run(_task())


@task(
    name="pickready.send_credit_invoice_email",
    route=Route.LAMBDA,
    rls="tenant",
)
def send_credit_invoice_email(purchase_id: str):
    """Email the GST invoice PDF for a settled credit-pack purchase.

    Master Directive Part 5 §3.3 step 5 / §7.3: the invoice goes to the
    account admin immediately on payment confirmation. Rendered HERE, not in
    the settlement path: a payment confirmation must never wait on PDF
    generation, and settlement dispatches this AFTER its commit, so a lost
    invoke is logged rather than turned into a failed settlement. The PDF is
    regenerated from the stored
    purchase row, so a redelivered task sends the same invoice again rather
    than a different one.
    """
    async def _task():
        import base64

        owner = await _resolve_tenant_id("purchase", purchase_id)
        if owner is None:
            logger.warning(
                "billing.credit_invoice_email_not_paid purchase=%s", purchase_id
            )
            return {"sent": False, "reason": "purchase not paid"}
        async with _tenant_worker_session(owner) as session:
            from app.models.billing import PURCHASE_PAID, CreditPurchase
            from app.services import credit_packs

            purchase = (
                await session.execute(
                    select(CreditPurchase).where(
                        CreditPurchase.id == uuid.UUID(purchase_id)
                    )
                )
            ).scalars().first()
            if purchase is None or purchase.status != PURCHASE_PAID:
                logger.warning(
                    "billing.credit_invoice_email_not_paid purchase=%s", purchase_id
                )
                return {"sent": False, "reason": "purchase not paid"}
            tenant = (
                await session.execute(
                    select(Tenant).where(Tenant.id == purchase.tenant_id)
                )
            ).scalars().first()
            row = (
                await session.execute(
                    text(
                        "SELECT u.email, t.name FROM users u JOIN tenants t ON t.id = u.tenant_id "
                        "WHERE u.tenant_id = :tid AND u.role = 'client' "
                        "AND u.status <> 'disabled' AND u.email IS NOT NULL "
                        "ORDER BY u.created_at LIMIT 1"
                    ),
                    {"tid": str(purchase.tenant_id)},
                )
            ).mappings().first()
            if row is None or tenant is None:
                logger.warning(
                    "billing.credit_invoice_email_no_recipient purchase=%s", purchase_id
                )
                return {"sent": False, "reason": "no recipient"}

            pdf = credit_packs.render_invoice_pdf(purchase, tenant)
            dispatch(
                "pickready.send_email",
                args=[
                    str(purchase.tenant_id),
                    row["email"],
                    "credit_invoice",
                    {
                        "company_name": row["name"],
                        "credits_total": str(
                            purchase.credits_purchased + purchase.bonus_credits
                        ),
                        "invoice_number": purchase.invoice_number or "",
                        "total_inr": f"{purchase.total_inr:,}",
                        # From the stored row, never the constant: an
                        # invoice issued before change request 25 carries
                        # NULL and keeps saying what it said when it was
                        # issued.
                        "validity_sentence": (
                            "These credits never expire."
                            if purchase.credit_validity_months is None
                            else (
                                "These credits are valid for "
                                f"{purchase.credit_validity_months} months "
                                "from the date of this invoice."
                            )
                        ),
                        "billing_url": f"{get_settings().frontend_url}/org/billing",
                    },
                ],
                kwargs={
                    "attachments": [
                        {
                            "filename": f"{purchase.invoice_number or purchase_id}.pdf",
                            "content": base64.b64encode(pdf).decode("ascii"),
                        }
                    ]
                },
            )
            return {"sent": True, "invoice": purchase.invoice_number}

    return _run(_task())


# ── Dashboard (ESD §14) ─────────────────────────────────────────────────────

@task(
    name="pickready.refresh_dashboard_views",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's rows",
    max_attempts=3,
)
def refresh_dashboard_views():
    """Refresh the dashboard materialized view (scheduled, every 5 min).
    CONCURRENTLY requires the unique index on job_id and must run outside a
    transaction block  -  hence the AUTOCOMMIT connection.

    THE REFRESH MUST BYPASS RLS, OR IT REBUILDS THE VIEW EMPTY.
    `dashboard_job_metrics` aggregates `jobs` and `job_candidate_links`, both of
    which have FORCE ROW LEVEL SECURITY  -  forced, so being the table owner does
    not exempt the refresh. This connection is brand new and belongs to no
    tenant, so without an escape hatch every base row is filtered out and
    REFRESH faithfully rebuilds the view from zero rows. It raises nothing: an
    empty aggregate is a perfectly valid result. Measured on production before
    this fix, the view held 0 rows against 35 live jobs, and every dashboard
    reading it rendered blank behind a clean 200.

    Setting the flag is correct rather than a workaround: this is the same
    audit-logged cross-tenant escape hatch the policies define, and a
    platform-wide aggregate is cross-tenant BY DEFINITION. The view is never
    served raw  -  `api/dashboard` filters it by the caller's tenant.

    The sentinel tenant is pinned alongside it for the reason set out in
    `core/db.superadmin_scope`: `current_setting` is STABLE, so the planner
    constant-folds the policies' `::uuid` cast before the bypass OR is ever
    evaluated, and an empty-string GUC therefore raises during planning no
    matter what the bypass flag says. Migration 0034 guards the cast with
    nullif() so this can no longer bite, but pinning the sentinel keeps the task
    correct even against an unmigrated database.
    """
    async def _task():
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.connect() as conn:
                await conn.execution_options(isolation_level="AUTOCOMMIT")
                # false => session-level, so it survives the REFRESH's own
                # implicit transaction rather than reverting underneath it.
                await conn.execute(
                    text("SELECT set_config('app.bypass_rls', 'on', false)")
                )
                await conn.execute(
                    text(
                        "SELECT set_config('app.tenant_id',"
                        " '00000000-0000-0000-0000-000000000000', false)"
                    )
                )
                # THROUGH THE FUNCTION, NEVER THE STATEMENT. `REFRESH
                # MATERIALIZED VIEW` requires OWNERSHIP, and since the
                # 2026-09-11 credential split this connection is
                # `pickready_app`, a least-privileged NOINHERIT role that
                # deliberately owns nothing. Issued directly it raised
                # "must be owner of materialized view dashboard_job_metrics"
                # on every run for a week, and the only symptom was a
                # CloudWatch alarm whose SNS subscription was unconfirmed.
                #
                # Migration 0099 defines `refresh_dashboard_job_metrics()` as
                # SECURITY DEFINER, owned by the object owner, with EXECUTE
                # granted to this role and to nothing else. One capability,
                # rather than the `SET ROLE` that would have handed a scheduled
                # background task everything the owner can do.
                #
                # The function sets the two GUCs above itself, so a caller
                # cannot forget the bypass and silently rebuild the view empty.
                # Setting them here as well is deliberate redundancy: this task
                # must keep working against a database where 0099 has not been
                # applied yet, which during a rolling deploy is every database.
                await conn.execute(text("SELECT refresh_dashboard_job_metrics()"))
        finally:
            await engine.dispose()
    _run(_task())


# ── Background verification (add-features spec 2026-09-05) ──────────────────

@task(
    name="pickready.send_bgv_inquiry",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "reads candidate_employments, the candidate's tenant-free employment history"
    ),
    max_attempts_setting="delivery_max_retries",
    backoff_seconds=60.0,
    backoff_max_seconds=60.0,
    retry_on=(TransientDeliveryError,),
    bind=True,
)
def send_bgv_inquiry(ctx: TaskContext, inquiry_id: str):
    """Send one background-verification inquiry to one previous employer's
    departmental mailbox (add-features spec 2026-09-05, Candidate
    Verification).

    The email is the FIXED `bgv_inquiry` template (services/email_render):
    factual, professional, no generation. It is dispatched only on the
    candidate's own explicit action, and following up with the employer is
    the candidate's responsibility by design, so there is no reminder sweep.

    Every failure is visible on the row: a permanent delivery failure or an
    unexpected error lands the inquiry in `dispatch_failed`; a transient SMTP
    failure retries on the delivery budget and lands there when the budget is
    exhausted. Success stamps `dispatched` and `inquiry_sent_at` only after
    SMTP accepted the message, because a timestamp is not evidence that work
    happened unless it is written after the work did.
    """
    from app.models.bgv import (
        DISPATCHABLE_STATUSES,
        STATUS_DISPATCHED,
        STATUS_DISPATCH_FAILED,
        BGVInquiry,
    )

    async def _task():
        async with _worker_session() as session:
            inquiry = await session.get(BGVInquiry, uuid.UUID(str(inquiry_id)))
            if inquiry is None:
                raise ValueError(f"BGVInquiry {inquiry_id} not found")
            if inquiry.status not in DISPATCHABLE_STATUSES:
                # Already dispatched (or further along): a duplicate inquiry
                # email to an HR mailbox reads as spam and burns the
                # candidate's credibility, so a re-delivered task is a no-op.
                logger.info(
                    "bgv.inquiry_not_dispatchable id=%s status=%s",
                    inquiry_id, inquiry.status,
                )
                return {"status": inquiry.status, "sent": False}

            candidate = await session.get(Candidate, inquiry.candidate_id)
            context = {
                "employer_name": inquiry.employer_name,
                "candidate_name": (
                    candidate.full_name
                    if candidate is not None and candidate.full_name
                    else "the candidate"
                ),
                "reply_token": inquiry.reply_token,
            }
            now = datetime.now(timezone.utc)
            try:
                await _send_email_async(
                    session, None, inquiry.departmental_email,
                    "bgv_inquiry", context,
                )
            except PermanentDeliveryError as err:
                inquiry.status = STATUS_DISPATCH_FAILED
                inquiry.updated_at = now
                await session.commit()
                logger.error(
                    "bgv.inquiry_permanent_failure id=%s to=%s ACTION: %s",
                    inquiry_id, inquiry.departmental_email, err.hint,
                )
                return {"status": "dispatch_failed", "error": err.error_name}
            except TransientDeliveryError:
                if ctx.is_final_attempt:
                    inquiry.status = STATUS_DISPATCH_FAILED
                    inquiry.updated_at = now
                    await session.commit()
                raise
            except Exception:
                # A render or configuration failure is not retryable here and
                # must not strand the row looking untouched: the candidate is
                # told the dispatch failed, and the re-raise moves the error
                # metric.
                inquiry.status = STATUS_DISPATCH_FAILED
                inquiry.updated_at = now
                await session.commit()
                raise

            inquiry.status = STATUS_DISPATCHED
            inquiry.inquiry_sent_at = now
            inquiry.updated_at = now
            await session.commit()
            return {"status": "dispatched"}

    return _run(_task())


@task(
    name="pickready.parse_bgv_reply",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "reads candidate_employments, the candidate's tenant-free employment history"
    ),
    max_attempts=3,
)
def parse_bgv_reply(inquiry_id: str, raw_email_text: str):
    """Extract the seven BGV fields from an employer's reply (add-features
    spec 2026-09-05). The raw reply is already on the row (the inbound-email
    webhook wrote it before dispatching this task), so a failed extraction
    loses nothing: the inquiry lands in `parse_failed` honestly and the reply
    text is kept for a later attempt. A provider outage propagates so the
    task's retry policy applies instead of being mislabelled a parse failure.
    """
    from app.models.bgv import (
        STATUS_PARSED,
        STATUS_PARSE_FAILED,
        STATUS_RESPONSE_RECEIVED,
        BGVInquiry,
    )
    from app.services import bgv as bgv_service

    async def _task():
        async with _worker_session() as session:
            inquiry = await session.get(BGVInquiry, uuid.UUID(str(inquiry_id)))
            if inquiry is None:
                raise ValueError(f"BGVInquiry {inquiry_id} not found")
            if inquiry.status not in {STATUS_RESPONSE_RECEIVED, STATUS_PARSE_FAILED}:
                logger.info(
                    "bgv.reply_parse_ignored id=%s status=%s",
                    inquiry_id, inquiry.status,
                )
                return {"status": inquiry.status, "parsed": False}

            now = datetime.now(timezone.utc)
            try:
                parsed = await bgv_service.parse_reply(
                    raw_email_text, session=session
                )
            except bgv_service.BGVParseError as exc:
                inquiry.status = STATUS_PARSE_FAILED
                inquiry.updated_at = now
                await session.commit()
                logger.warning(
                    "bgv.reply_parse_failed id=%s reason=%s", inquiry_id, exc
                )
                return {"status": "parse_failed"}

            inquiry.parsed_fields_json = parsed
            inquiry.status = STATUS_PARSED
            inquiry.updated_at = now
            await session.commit()
            return {"status": "parsed"}

    return _run(_task())


@task(
    name="pickready.notify_support_message",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "a customer message is routed to Vivekium staff, whose users rows carry no "
        "tenant"
    ),
    max_attempts=2,
)
def notify_support_message(thread_id: str, message_id: str):
    """Tell the other side of a support thread that a message arrived.

    Route.LAMBDA: this is work measured in seconds. It resolves recipients and
    hands each one to `pickready.send_email`, which owns delivery, the
    transport choice and the retry policy. Two hops rather than one because the
    fan-out and the send are different failures: one recipient's address
    bouncing must not stop the others being told.

    WHO IS TOLD DEPENDS ON WHO WROTE
    ----------------------------------
    A STAFF message goes to the customer who opened the thread. A CUSTOMER
    message goes to every Vivekium staff member holding
    `handle_support_threads`, asked of the permission ROWS through the rbac
    engine rather than branched on by role name, so a future support role is a
    seeded row instead of an edit to this function.

    A recipient with no email is SKIPPED and counted, never silently dropped:
    a fan-out that told nobody and a fan-out that told everybody produce the
    same empty log otherwise, which is the failure `dispatch` raising was added
    to make visible.

    NOTHING ABOUT A CANDIDATE IS IN THE PAYLOAD, and nothing could be. The
    email carries the thread's subject, the customer's name and a link. The
    message BODY is deliberately not included: it is free text a human typed,
    it may quote something a customer pasted, and an email is the one copy of
    it this product cannot recall. The recipient signs in to read it.
    """
    from app.models.support import SIDE_STAFF, SupportMessage, SupportThread
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services import rbac
    from app.services.capabilities import HANDLE_SUPPORT_THREADS

    async def _task():
        async with _worker_session() as session:
            message = await session.get(SupportMessage, uuid.UUID(str(message_id)))
            thread = await session.get(SupportThread, uuid.UUID(str(thread_id)))
            if message is None or thread is None:
                # Not an error: a thread deleted with its tenant between the
                # dispatch and the invocation is an ordinary race, and there is
                # nobody left to notify. Logged so it is not invisible.
                logger.info(
                    "support.notify_skipped reason=row_gone thread=%s", thread_id
                )
                return {"notified": 0, "skipped": 0, "reason": "row_gone"}

            tenant = await session.get(Tenant, thread.tenant_id)
            company_name = getattr(tenant, "name", "") or "your organisation"

            if message.author_side == SIDE_STAFF:
                recipients = await _support_customer_recipients(session, thread)
                template = "support_reply_to_customer"
                url = f"{get_settings().frontend_url}/org/support/{thread.id}"
            else:
                recipients = await _support_staff_recipients(
                    session, rbac, User, HANDLE_SUPPORT_THREADS
                )
                template = "support_message_for_staff"
                url = f"{get_settings().frontend_url}/admin/support/{thread.id}"

            notified = 0
            skipped = 0
            for email in recipients:
                if not (email or "").strip():
                    skipped += 1
                    continue
                dispatch(
                    "pickready.send_email",
                    args=[
                        # The staff notification is a PLATFORM email and
                        # carries no tenant, so it uses the default sender
                        # rather than the customer's own verified one: sending
                        # Vivekium's internal queue notice as the customer
                        # would be wrong in both directions.
                        str(thread.tenant_id)
                        if message.author_side == SIDE_STAFF
                        else None,
                        email,
                        template,
                        {
                            "company_name": company_name,
                            "subject_line": thread.subject,
                            "support_url": url,
                        },
                    ],
                )
                notified += 1

            logger.info(
                "support.notified thread=%s side=%s notified=%d skipped=%d",
                thread_id, message.author_side, notified, skipped,
            )
            return {"notified": notified, "skipped": skipped}

    return _run(_task())


async def _support_customer_recipients(session, thread) -> list[str]:
    """The person who opened the thread, or the tenant's Super Admin.

    The fallback matters: `opened_by_user_id` is ON DELETE SET NULL, so a
    thread whose author has left the company would otherwise notify nobody and
    a reply would sit unread for as long as the customer took to look.
    """
    from app.models.user import User

    if thread.opened_by_user_id:
        opener = await session.get(User, thread.opened_by_user_id)
        if opener is not None and (opener.email or "").strip():
            return [opener.email]
    rows = await session.execute(
        select(User.email)
        .where(
            User.tenant_id == thread.tenant_id,
            User.role == "client",
            User.status != "disabled",
            User.email.isnot(None),
        )
        .order_by(User.created_at)
        .limit(1)
    )
    return [email for (email,) in rows]


async def _support_staff_recipients(session, rbac, User, capability) -> list[str]:
    """Every platform user the permission ROWS say may handle support.

    Asked of the engine per user rather than filtered by role name here, so the
    answer honours the per-user overlay: somebody whose access was pinned off
    stops being paged without anybody editing this task.
    """
    rows = await session.execute(
        select(User.id, User.email, User.role)
        .where(
            User.tenant_id.is_(None),
            User.status != "disabled",
            User.email.isnot(None),
        )
        .order_by(User.created_at)
    )
    recipients: list[str] = []
    for user_id, email, role in rows:
        if await rbac.has_capability(session, None, role, capability, user_id):
            recipients.append(email)
    return recipients


# The Phase 3 tasks live in their own modules since 2026-09-24 (PLAN-p3 WP0).
# Importing them here is what registers them: `registry.resolve` imports this
# module and nothing else, so a task module not imported here would be a task
# no dispatch could reach.
from app.workers import tasks_media, tasks_proctoring, tasks_questions, tasks_voice  # noqa: E402,F401
from app.workers import tasks_invitations  # noqa: E402,F401
# Phase 4 WP-4B2: the coding submission, its sweep, the sandbox probe and the
# operator's sandbox verification. Registered by the same import rule.
from app.workers import coding_tasks  # noqa: E402,F401
# The semantic index repair sweep (PLAN-p5 WP5-E), registered the same way.
from app.workers import tasks_retrieval  # noqa: E402,F401
# The platform security email (auth hardening, 2026-09-29), registered the
# same way.
from app.workers import tasks_security_email  # noqa: E402,F401
# Public demo retention sweep. Registered by the same import rule.
# The Leadership Intelligence draft (2026-09-29, spec 17), registered the
# same way.
from app.workers import tasks_leadership  # noqa: E402,F401
