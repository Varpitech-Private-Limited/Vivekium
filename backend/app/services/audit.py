"""Append-only audit trail writer (PRD §8 Auditability / ESD §16).

Every approval transition, permission change, profile status change, Super
Admin cross-tenant access, and — see the AUTH_* constants below — every
auth event goes through here.

Two entry points:

* `audit(...)`  — low-level INSERT that participates in the CALLER's
  transaction and returns the row. Use it where the audit write is part of
  the unit of work being committed (approval FSM, permission edits, etc.);
  a failure here is a real error and should surface.

* `record_auth_event(...)` — hardened wrapper for the AUTH request/worker
  path. It writes inside its own SAVEPOINT and NEVER raises into the caller:
  an audit failure must not break a login (or a background task). It tolerates
  `tenant_id=None` (Owner / candidate — a past bug crashed on exactly this)
  and `actor_user_id=None` (a failed login where no user resolved).
"""
import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import AuditLog

logger = logging.getLogger("pickready.audit")


# ── Auth action constants (single source of truth for auth audit rows) ───────
# Kept here so callers (api/auth.py, services/otp.py, workers/tasks.py,
# api/deps.py) reference names, never string literals.
AUTH_OTP_REQUESTED = "otp_requested"
AUTH_OTP_VERIFIED = "otp_verified"
AUTH_OTP_FAILED = "otp_failed"
AUTH_OTP_RATE_LIMITED = "otp_rate_limited"
AUTH_LOGIN_SUCCEEDED = "login_succeeded"
#: A proven Firebase identity was refused a session it asked for. Today the
#: only reason is an attempt to rebind an account that is already linked to a
#: different Firebase uid, which is the shape of an account takeover, so it
#: is worth a row of its own rather than silence.
AUTH_LOGIN_REFUSED = "login_refused"
AUTH_CONTEXT_SELECTED = "context_selected"
AUTH_LOGOUT = "logout"
AUTH_EMAIL_SEND_FAILED = "email_send_failed"
AUTH_OWNER_INVARIANT_VIOLATION = "owner_invariant_violation"
#: Auth hardening, 2026-09-29 (auth spec section 29). None of these rows ever
#: carries the code, a token or a password: the metadata names the FLOW and
#: nothing a caller typed. The challenge-failed action was retired with the
#: server-drawn check (A-24, 2026-10-30).
AUTH_PASSWORD_CHANGE_CODE_SENT = "password_change_code_sent"
AUTH_PASSWORD_CHANGED = "password_changed"
AUTH_STAFF_INVITE_ACCEPTED = "staff_invite_accepted"

# Every auth action this module knows how to record (handy for tests and for
# the validation harness to assert coverage against).
AUTH_ACTIONS: frozenset[str] = frozenset(
    {
        AUTH_OTP_REQUESTED,
        AUTH_OTP_VERIFIED,
        AUTH_OTP_FAILED,
        AUTH_OTP_RATE_LIMITED,
        AUTH_LOGIN_SUCCEEDED,
        AUTH_LOGIN_REFUSED,
        AUTH_CONTEXT_SELECTED,
        AUTH_LOGOUT,
        AUTH_EMAIL_SEND_FAILED,
        AUTH_OWNER_INVARIANT_VIOLATION,
        AUTH_PASSWORD_CHANGE_CODE_SENT,
        AUTH_PASSWORD_CHANGED,
        AUTH_STAFF_INVITE_ACCEPTED,
    }
)


def _coerce_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    """None / empty -> None; str/UUID -> UUID. Never raises for the None case
    (the tenant_id=None crash this module is hardened against)."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    text = str(value).strip()
    if not text:
        return None
    return uuid.UUID(text)


def _new_audit_row(
    *,
    tenant_id: uuid.UUID | str | None,
    actor_user_id: uuid.UUID | str | None,
    action: str,
    target_type: str | None,
    target_id: uuid.UUID | str | None,
    metadata: dict[str, Any] | None,
) -> AuditLog:
    return AuditLog(
        tenant_id=_coerce_uuid(tenant_id),
        actor_user_id=_coerce_uuid(actor_user_id),
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        metadata_json=metadata,
    )


async def audit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | str | None,
    actor_user_id: uuid.UUID | str | None,
    action: str,
    target_type: str | None = None,
    target_id: uuid.UUID | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Insert one immutable audit_log row inside the caller's transaction and
    return it. The application role has no UPDATE/DELETE grants on this table
    (enforced in the migration). Raises on failure — use for writes that are
    part of the committed unit of work.

    Tolerates `tenant_id=None` (platform-level / Owner / candidate events) and
    `actor_user_id=None` (unauthenticated / failed-login events)."""
    row = _new_audit_row(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        metadata=metadata,
    )
    session.add(row)
    await session.flush()
    return row


async def record_auth_event(
    session: AsyncSession,
    *,
    action: str,
    actor_user_id: uuid.UUID | str | None = None,
    tenant_id: uuid.UUID | str | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Record an auth event WITHOUT ever raising into the caller.

    Designed for the login path and background tasks: an audit failure must not
    break a login or a task. The write happens inside a SAVEPOINT so that a
    failure rolls back only the audit insert and leaves the caller's
    transaction intact and usable. Returns True if the row was written, False
    if it was swallowed (the failure is logged, never propagated).

    Works identically whether called from a request handler (session opened via
    `get_session`) or a background task (session-level `app.bypass_rls`): the
    audit_log table has no RLS policy, so no tenant var is required.

    `tenant_id=None` and `actor_user_id=None` are fully supported.
    """
    if action not in AUTH_ACTIONS:
        # Not fatal — record it anyway, but flag the drift so unknown auth
        # actions don't silently accumulate.
        logger.warning("record_auth_event called with unregistered action %r", action)
    try:
        # SAVEPOINT isolation: a failure here rolls back only this insert.
        async with session.begin_nested():
            row = _new_audit_row(
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                metadata=metadata,
            )
            session.add(row)
            await session.flush()
        return True
    except Exception:  # noqa: BLE001 — auth audit must never break the caller
        logger.exception(
            "audit write failed (swallowed) action=%s tenant_id=%s actor=%s",
            action,
            tenant_id,
            actor_user_id,
        )
        return False


# ═══════════════════════════════════════════════════════════════════════════
# RBAC 30: what an authorization-sensitive mutation must record
# ═══════════════════════════════════════════════════════════════════════════
#
# The row above carries actor, tenant, action, target and a free JSONB blob.
# RBAC 30 asks for nine more things and spec-doc6 4.1 says to add them as
# COLUMNS rather than leaving them in the blob. The reason is the Super Admin
# activity view (31): "who changed this, what did they change, what was the
# previous state" has to be answerable by a query with a WHERE clause on it,
# and a question you can only answer by parsing every row's JSON is one the
# dashboard will answer by not asking.
#
# The columns arrive in migration 0061, all nullable, so a rolling deploy has
# an old writer and a new reader coexisting without either failing.
#
# WHY THE AGENT COLUMNS ARE TWO COLUMNS
# -------------------------------------
# 34: every AI-initiated mutation must be attributable to BOTH the human
# principal and the executing agent. One column cannot hold both, and
# overloading `actor_user_id` with an agent name would make "which human
# authorised this" unanswerable exactly where it matters most. So
# `actor_user_id` stays the human, always, and `agent_name` says which agent
# executed on their behalf. An agent row with a null actor is refused by
# `record_action` rather than written and explained later.

AUDIT_ACTOR_ROLE_UNKNOWN = "unknown"

#: Actions the Super Admin activity view (31) treats as important company
#: activity. Not a filter on what gets WRITTEN, which is everything: a
#: presentation list, kept here so the view and the writer name the same
#: strings.
JOB_CREATED = "job_created"
JOB_JD_EDITED = "job_jd_edited"
JOB_CRITERIA_EDITED = "job_criteria_edited"
JOB_FINALIZED = "job_finalized"
JOB_PUBLISHED = "job_published"
CANDIDATE_APPLIED = "candidate_applied"
CANDIDATE_SHORTLISTED = "candidate_shortlisted"
CANDIDATE_REJECTED = "candidate_rejected"
CANDIDATE_STAGE_MOVED = "candidate_stage_moved"
INTEGRITY_FLAG_RAISED = "integrity_flag_raised"
INTEGRITY_DISPOSITION_RECORDED = "integrity_disposition_recorded"
TEAM_REVIEW_REMARK_ADDED = "team_review_remark_added"
AUTHORIZATION_REFUSED = "authorization_refused"

ACTIVITY_ACTIONS: tuple[str, ...] = (
    JOB_CREATED,
    JOB_JD_EDITED,
    JOB_CRITERIA_EDITED,
    JOB_FINALIZED,
    JOB_PUBLISHED,
    CANDIDATE_APPLIED,
    CANDIDATE_SHORTLISTED,
    CANDIDATE_REJECTED,
    CANDIDATE_STAGE_MOVED,
    INTEGRITY_FLAG_RAISED,
    INTEGRITY_DISPOSITION_RECORDED,
    TEAM_REVIEW_REMARK_ADDED,
    AUTHORIZATION_REFUSED,
)

#: READS of a candidate's assessment record by a hiring team member (PLAN-p5
#: WP5-F). The PRISM PDF is the copy that leaves the product, the transcript is
#: the candidate's raw answers, and the citation view resolves report
#: statements back to those answers; each read is a row, written in the ONE
#: insert `record_action` makes, in the request's own transaction.
PRISM_PDF_DOWNLOADED = "prism_report.pdf_downloaded"
ASSESSMENT_TRANSCRIPT_VIEWED = "assessment_transcript.viewed"
PRISM_CITATIONS_VIEWED = "prism_report.citations_viewed"

#: Actions that record a candidate leaving the process. RBAC 39 and this
#: project's own rule say no flag ever auto-rejects, so every one of these
#: must be traceable to a recorded human disposition. Asserted by
#: `tests/test_audit_invariants.py`.
REJECTION_ACTIONS: frozenset[str] = frozenset({CANDIDATE_REJECTED})


class AgentPrincipalError(ValueError):
    """An agent action was recorded without the human it acted for.

    Raised rather than defaulted. RBAC 34 makes dual attribution the whole
    point of auditing an agent, and a row that silently lost half of it looks
    exactly like a human action, which is the one reading that must never be
    possible.
    """


async def record_action(
    session: AsyncSession,
    *,
    action: str,
    actor_user_id: uuid.UUID | str | None,
    actor_role: str | None,
    tenant_id: uuid.UUID | str | None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | str | None = None,
    previous_state: dict[str, Any] | None = None,
    new_state: dict[str, Any] | None = None,
    job_id: uuid.UUID | str | None = None,
    application_id: uuid.UUID | str | None = None,
    candidate_id: uuid.UUID | str | None = None,
    request_method: str | None = None,
    request_path: str | None = None,
    request_ip: str | None = None,
    correlation_id: str | None = None,
    agent_name: str | None = None,
    exceptional: bool = False,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    """Write one RBAC 30 audit row inside the caller's transaction.

    Every field 30 names is a named parameter, so a caller that forgets one
    has an obviously empty column rather than a plausible-looking row with the
    detail buried in a dict nobody reads.

    Raises on failure, exactly like `audit`: an authorization-sensitive
    mutation whose audit row did not write is a mutation nobody can review,
    and the caller's transaction should not commit. That is the opposite of
    `record_auth_event`'s contract and deliberately so -- a login must survive
    an audit failure, a rejection must not.

    `actor_role` is the role AT THE TIME OF THE ACTION (30), copied onto the
    row rather than joined to `users` later. A person's role changes; what
    authority a past action was taken under does not.
    """
    if agent_name is not None and actor_user_id is None:
        raise AgentPrincipalError(
            f"agent {agent_name!r} recorded action {action!r} with no human "
            "principal; RBAC 34 requires both"
        )
    row = _new_audit_row(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type=resource_type,
        target_id=resource_id,
        metadata=metadata,
    )
    row.actor_role = str(actor_role) if actor_role else AUDIT_ACTOR_ROLE_UNKNOWN
    row.previous_state = previous_state
    row.new_state = new_state
    row.job_id = _coerce_uuid(job_id)
    row.application_id = _coerce_uuid(application_id)
    row.candidate_id = _coerce_uuid(candidate_id)
    row.request_method = request_method
    row.request_path = request_path
    row.request_ip = request_ip
    row.correlation_id = correlation_id
    row.agent_name = agent_name
    row.exceptional = bool(exceptional)
    session.add(row)
    await session.flush()
    return row


async def record_agent_action(
    session: AsyncSession,
    *,
    action: str,
    agent_name: str,
    principal_user_id: uuid.UUID | str,
    principal_role: str | None,
    tenant_id: uuid.UUID | str | None,
    **kwargs: Any,
) -> AuditLog:
    """`record_action` for an AI-initiated mutation (RBAC 34).

    A separate entry point rather than an optional argument, because the
    argument that must not be omitted is the human, and a required positional
    is how you make it impossible to omit. `principal_user_id` has no default.
    """
    return await record_action(
        session,
        action=action,
        actor_user_id=principal_user_id,
        actor_role=principal_role,
        tenant_id=tenant_id,
        agent_name=agent_name,
        **kwargs,
    )


# ── The Super Admin activity view (RBAC 31) ──────────────────────────────────
#
# 31 lists seven questions the Super Admin must be able to answer and then
# says the audit trail MUST NOT depend exclusively on dashboard rendering.
# That sentence is why this is a reader over the same rows the writers above
# produce, and why `tests/test_audit_invariants.py` asserts the rows exist
# with nothing rendered at all.

#: What the activity view returns per row. Every one of 31's seven questions
#: is answered by a field here: who (actor, actor_role), what (action,
#: resource_type, resource_id), when (at), which job (job_id), which candidate
#: (candidate_id), previous state, current state.
ACTIVITY_FIELDS: tuple[str, ...] = (
    "id",
    "at",
    "actor_user_id",
    "actor_role",
    "action",
    "resource_type",
    "resource_id",
    "job_id",
    "application_id",
    "candidate_id",
    "previous_state",
    "new_state",
    "agent_name",
    "exceptional",
)


async def activity(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | str,
    limit: int = 50,
    offset: int = 0,
    actions: tuple[str, ...] | None = None,
    job_id: uuid.UUID | str | None = None,
    candidate_id: uuid.UUID | str | None = None,
) -> list[dict[str, Any]]:
    """Company activity for one tenant, newest first.

    Filtered and paginated in SQL. The Provider Portal already learned this
    lesson on its customer list: filtering a fetched page in the browser makes
    the result depend on which page happened to be loaded.

    `tenant_id` is required and is never taken from a request body: RBAC 4
    says the client identifier comes from the authenticated session.
    """
    clauses = ["tenant_id = :tid"]
    params: dict[str, Any] = {
        "tid": str(tenant_id),
        "limit": max(1, min(int(limit), 200)),
        "offset": max(0, int(offset)),
    }
    if actions:
        clauses.append("action = ANY(:actions)")
        params["actions"] = list(actions)
    if job_id is not None:
        clauses.append("job_id = :job_id")
        params["job_id"] = str(job_id)
    if candidate_id is not None:
        clauses.append("candidate_id = :candidate_id")
        params["candidate_id"] = str(candidate_id)

    columns = ", ".join(
        "target_type AS resource_type"
        if field == "resource_type"
        else "target_id AS resource_id"
        if field == "resource_id"
        else field
        for field in ACTIVITY_FIELDS
    )
    rows = (
        await session.execute(
            text(
                f"SELECT {columns} FROM audit_log WHERE {' AND '.join(clauses)} "
                "ORDER BY at DESC, id DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        )
    ).mappings().all()
    return [dict(row) for row in rows]
