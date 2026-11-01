"""Candidate sourcing, review-screen, decisions, pipeline status and
interview scheduling (FR-4.3/4.4, FR-7.x, FR-8.x)."""
import base64
import html
import io
import uuid
from urllib.parse import urlencode
from datetime import datetime, timezone

from docx import Document
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    get_tenant_db,
    require_capability,
    require_organisation_wide,
)
from app.models.candidate import (
    Candidate,
    CandidateTeamReview,
    Interview,
    JobCandidateLink,
    Profile,
    SOURCE_TYPE_SOURCED,
)
from app.models.enums import LinkSource
from app.models.job import Job
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.candidates import (
    CandidateOut,
    InterviewIn,
    InterviewOut,
    LinkArchiveOut,
    ProfileOut,
    TeamReviewIn,
    TeamReviewOut,
    TeamReviewRewriteIn,
    TeamReviewRewriteOut,
    TeamReviewsOut,
    UploadResumeOut,
    )
from app.services import candidate_identity, hiring_pipeline
from app.services import capabilities as caps
from app.services import department_access
from app.services import email_render
from app.services import rbac
from app.services import team_review
from app.services.audit import audit
from app.services import llm_router
from app.services.resume_storage import (
    apply_resume_asset,
    fetch_resume_bytes,
    ResumeStorageError,
    store_resume,
)
from app.services.resume_access import issue_resume_token, verify_resume_token
from app.workers.dispatch import dispatch_after_commit

router = APIRouter()

async def _get_link(
    session: AsyncSession, user: CurrentUser, link_id: uuid.UUID
) -> JobCandidateLink:
    link = await session.get(JobCandidateLink, link_id)
    if link is None or link.tenant_id != user.tenant_id:  # defense in depth
        raise HTTPException(status_code=404, detail="Link not found")
    # The department boundary (spec 2.11): an application on a job outside a
    # Functional Head's department answers like one in another tenant.
    await department_access.require_link_in_scope(
        session, user, link.id, detail="Link not found"
    )
    return link


async def _link_for_profile_in_scope(
    session: AsyncSession, user: CurrentUser, profile_id: uuid.UUID
) -> JobCandidateLink | None:
    """An application in this tenant AND the caller's department scope that
    carries this resume, or None. One statement, so a profile reused across a
    Finance and an Engineering application is readable by the Engineering
    Functional Head through the Engineering one only."""
    scope = await department_access.department_scope(session, user)
    return (
        await session.execute(
            select(JobCandidateLink)
            .join(Job, Job.id == JobCandidateLink.job_id)
            .where(
                JobCandidateLink.profile_id == profile_id,
                JobCandidateLink.tenant_id == user.tenant_id,
                department_access.job_scope_clause(scope, Job.department_id),
            )
            .limit(1)
        )
    ).scalars().first()


@router.post(
    "/jobs/{job_id}/upload-resume",
    response_model=UploadResumeOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_resume(
    job_id: uuid.UUID,
    file: UploadFile = File(...),
    email: str = Form(...),
    full_name: str | None = Form(default=None),
    phone: str | None = Form(default=None),
    user: CurrentUser = Depends(require_capability(caps.UPLOAD_RESUMES)),
    session: AsyncSession = Depends(get_tenant_db),
) -> UploadResumeOut:
    """Recruiter uploads ONE resume they found outside Vivekium (FR-4.3).

    Creates the candidate, the profile and a link that enters at `sourced`
    with its history row (`hiring_pipeline.start_sourced`): a person whose
    resume a recruiter found has not read this job or applied to it, and this
    route used to record them as `applied` (audit Part 1 #5). The parse is
    dispatched AFTER the commit, and the parse dispatches Yukti's reading.
    """
    job = await session.get(Job, job_id)
    if job is None or job.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="Job not found")
    await department_access.require_job_in_scope(session, user, job.id)
    if job.ratified_at is None:
        # ASSUMPTION: sourcing starts once the job has reached HR (FR-3.4).
        raise HTTPException(status_code=409, detail="Job is not ratified yet")

    # The CANONICAL record for this address, case-insensitively, through the
    # one resolver: an exact, unordered match here attached the same person to
    # a different record depending on how the recruiter typed the address.
    candidate = await candidate_identity.find_canonical_by_email(session, email)
    if candidate is None:
        candidate = Candidate(
            tenant_id=user.tenant_id, email=email, full_name=full_name, phone=phone
        )
        session.add(candidate)
        await session.flush()

    dup = (
        await session.execute(
            select(JobCandidateLink).where(
                JobCandidateLink.job_id == job.id,
                JobCandidateLink.candidate_id == candidate.id,
            )
        )
    ).scalars().first()
    if dup is not None:
        raise HTTPException(status_code=409, detail="Candidate is already linked to this job")

    asset = await store_resume(file)
    profile = Profile(candidate_id=candidate.id, source_tenant_id=user.tenant_id)
    apply_resume_asset(profile, asset)
    session.add(profile)
    await session.flush()

    link = JobCandidateLink(
        tenant_id=user.tenant_id, job_id=job.id, candidate_id=candidate.id,
        profile_id=profile.id, source=LinkSource.fresh,
        # ASSUMPTION (2026-07-28): a recruiter uploading ONE resume they found
        # elsewhere is `sourced`, not `databank`. Databank is specifically the
        # bulk upload (POST /jobs/{id}/candidates/databank, up to 25 files);
        # this single-file route predates it and describes a candidate the
        # recruiter procured from outside Vivekium.
        source_type=SOURCE_TYPE_SOURCED,
    )
    await hiring_pipeline.start_sourced(
        session,
        link,
        actor_user_id=user.user_id,
        remarks="Uploaded to the job by the recruitment team",
    )

    dispatch_after_commit(session, "pickready.parse_resume", args=[str(profile.id)])
    await audit(session, tenant_id=user.tenant_id, actor_user_id=user.user_id,
                action="resume_uploaded", target_type="profile", target_id=profile.id,
                metadata={"job_id": str(job.id), "candidate_id": str(candidate.id),
                          "resume_public_id": asset.public_id, "resume_sha256": asset.sha256})
    return UploadResumeOut(
        candidate_id=candidate.id, profile_id=profile.id, link_id=link.id,
        source=LinkSource.fresh, resume_public_id=asset.public_id, resume_url=asset.secure_url,
    )


async def _require_full_profile_access(session: AsyncSession, user: CurrentUser) -> None:
    """Refuse a caller who does not hold SEND_OUTREACH, BEFORE any file is read.

    This used to have a second half: a caller holding only VIEW_REVIEW_SCREEN
    (a Hiring Manager) could also read a candidate once HR had set
    `job_candidate_links.hm_access_granted` on one of that candidate's links.
    The one route that set the flag was deleted in the 2026-09 route scrap
    (`tests/test_dead_routes_removed.py`) and nothing else writes it, so the
    data half could only ever answer "not granted". Reading a flag nobody can
    set is a permission rule that exists in prose only; the column survives in
    the database (server default false, pilot holds no link rows) and is read
    by nothing.
    """
    if not await rbac.has_capability(
        session, user.tenant_id, user.role, caps.SEND_OUTREACH
    ):
        raise HTTPException(status_code=403, detail="Profile access not granted")


@router.get("/{candidate_id}/profile", response_model=ProfileOut)
async def get_profile(
    candidate_id: uuid.UUID,
    profile_id: uuid.UUID | None = Query(default=None),
    user: CurrentUser = Depends(require_capability(caps.VIEW_REVIEW_SCREEN)),
    session: AsyncSession = Depends(get_tenant_db),
) -> ProfileOut:
    """Full Profile for the HR Review Screen (FR-7.1/7.2).

    Full access to any profile is the SEND_OUTREACH capability, and nothing
    else: see `_require_full_profile_access` for the grant flag it replaced.
    """
    candidate = await session.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    await department_access.require_candidate_in_scope(session, user, candidate_id)

    await _require_full_profile_access(session, user)

    profile_query = select(Profile).where(Profile.candidate_id == candidate_id)
    if profile_id is not None:
        profile_query = profile_query.where(Profile.id == profile_id)
    else:
        # A department-scoped caller gets the newest resume sent to a job in
        # THEIR department, never the newest one overall: a candidate who
        # applied to Finance after Engineering must not hand the Engineering
        # Functional Head the Finance application's resume and validation
        # answers (security review 2026-09-29).
        scope = await department_access.department_scope(session, user)
        if scope is not None:
            profile_query = profile_query.where(
                Profile.id.in_(
                    select(JobCandidateLink.profile_id)
                    .join(Job, Job.id == JobCandidateLink.job_id)
                    .where(
                        JobCandidateLink.candidate_id == candidate_id,
                        JobCandidateLink.tenant_id == user.tenant_id,
                        department_access.job_scope_clause(scope, Job.department_id),
                    )
                )
            )
        profile_query = profile_query.order_by(Profile.created_at.desc())
    profile = (await session.execute(profile_query)).scalars().first()
    if profile is None:
        raise HTTPException(status_code=404, detail="No profile for this candidate")
    if profile_id is not None:
        # EXISTENCE check only. The same (candidate, tenant, profile) triple
        # legitimately appears on many rows, one per job the candidate is
        # linked to in this tenant, and a reused resume keeps the same profile
        # so `scalar_one_or_none()` here raised MultipleResultsFound (500)
        # for any candidate linked to more than one job, which the review
        # screen surfaced as "Could not load this candidate's profile".
        scoped_profile_link = (
            await session.execute(
                select(JobCandidateLink.id)
                .join(Job, Job.id == JobCandidateLink.job_id)
                .where(
                    JobCandidateLink.candidate_id == candidate_id,
                    JobCandidateLink.tenant_id == user.tenant_id,
                    JobCandidateLink.profile_id == profile_id,
                    department_access.job_scope_clause(
                        await department_access.department_scope(session, user),
                        Job.department_id,
                    ),
                )
                .limit(1)
            )
        ).scalars().first()
        if scoped_profile_link is None:
            raise HTTPException(status_code=404, detail="No profile for this candidate")

    # The tenant-owned verification_requests read that sat here is RETIRED
    # (vivekium C8); the recruiter's verification surface is the BGV panel.
    return ProfileOut(
        id=profile.id,
        candidate=CandidateOut.model_validate(candidate),
        resume_url=profile.resume_url,
        resume_public_id=profile.resume_public_id,
        resume_original_filename=profile.resume_original_filename,
        resume_mime_type=profile.resume_mime_type,
        resume_size_bytes=profile.resume_size_bytes,
        resume_uploaded_at=profile.resume_uploaded_at,
        aspects_json=profile.aspects_json,
        parsed_fields_json=profile.parsed_fields_json,
        aspects_completed_at=profile.aspects_completed_at,
    )


@router.get("/{candidate_id}/project-evidence")
async def get_project_evidence(
    candidate_id: uuid.UUID,
    user: CurrentUser = Depends(require_capability(caps.VIEW_REVIEW_SCREEN)),
    session: AsyncSession = Depends(get_tenant_db),
) -> dict:
    """Derived Project Evidence for the review screen.

    Same access rules as the full profile (VIEW_REVIEW_SCREEN, with the
    HM-grant gate for non-HR holders), plus one stricter check: the candidate
    must be linked to a job in THIS tenant, and a candidate who is not answers
    404 rather than 403 (RBAC: a cross-tenant read must not confirm
    existence).

    `projects` is DERIVED intelligence for evidence-ready rows only: candidate
    claims labelled as claims, observed evidence labelled as observed, strength
    as a WORD. `submissions` and `counts` cover EVERY submitted project with a
    word for its state (queued, analysing, ready, summary_only, failed), so a
    project still being analysed is never reported as absent. There is no
    original file to offer, because none is retained.
    """
    from app.services.projects import context as project_context

    candidate = await session.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    linked = (
        await session.execute(
            select(JobCandidateLink.id)
            .where(
                JobCandidateLink.candidate_id == candidate_id,
                JobCandidateLink.tenant_id == user.tenant_id,
            )
            .limit(1)
        )
    ).scalars().first()
    if linked is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    await department_access.require_candidate_in_scope(session, user, candidate_id)
    await _require_full_profile_access(session, user)
    return await project_context.recruiter_overview(session, candidate_id)


@router.get("/profiles/{profile_id}/resume-preview", response_class=HTMLResponse)
async def preview_resume(
    profile_id: uuid.UUID,
    user: CurrentUser = Depends(require_capability(caps.VIEW_REVIEW_SCREEN)),
    session: AsyncSession = Depends(get_tenant_db),
) -> HTMLResponse:
    """Render a DOCX resume inside Vivekium as safe, monochrome HTML.

    Browsers cannot natively display Word documents. The server downloads only
    the trusted private asset already stored on the profile, extracts
    paragraphs and tables with python-docx, HTML-escapes every value, and
    returns a same-app preview. PDF/image previews continue to use the browser's
    native viewer.
    """
    profile = await session.get(Profile, profile_id)
    if profile is None or not profile.resume_url:
        raise HTTPException(status_code=404, detail="Resume not found")
    link = await _link_for_profile_in_scope(session, user, profile.id)
    if link is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    await _require_full_profile_access(session, user)

    try:
        resume_bytes = await fetch_resume_bytes(profile)
    except ResumeStorageError as exc:
        raise HTTPException(status_code=502, detail="Resume could not be loaded") from exc
    try:
        document = Document(io.BytesIO(resume_bytes))
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Resume is not a readable DOCX file") from exc

    blocks: list[str] = []
    for paragraph in document.paragraphs:
        text_value = paragraph.text.strip()
        if not text_value:
            continue
        safe = html.escape(text_value)
        style_name = (paragraph.style.name if paragraph.style else "").lower()
        if style_name.startswith("heading"):
            blocks.append(f"<h2>{safe}</h2>")
        else:
            blocks.append(f"<p>{safe}</p>")
    for table in document.tables:
        rows: list[str] = []
        for row in table.rows:
            cells = "".join(
                f"<td>{html.escape(cell.text.strip())}</td>" for cell in row.cells
            )
            rows.append(f"<tr>{cells}</tr>")
        if rows:
            blocks.append(f"<table>{''.join(rows)}</table>")

    filename = html.escape(profile.resume_original_filename or "Resume")
    body = "\n".join(blocks) or "<p>No readable text was found in this document.</p>"
    return HTMLResponse(
        content=f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{filename}</title><style>
html{{background:#f5f5f5;color:#111;font-family:Arial,sans-serif}}
body{{box-sizing:border-box;max-width:850px;margin:24px auto;padding:56px;background:#fff;
box-shadow:0 1px 8px rgba(0,0,0,.14);line-height:1.55}}
h2{{font-size:1.1rem;margin:1.5rem 0 .5rem;border-bottom:1px solid #bbb;padding-bottom:.25rem}}
p{{margin:.55rem 0;white-space:pre-wrap}}table{{width:100%;border-collapse:collapse;margin:1rem 0}}
td{{border:1px solid #bbb;padding:.45rem;vertical-align:top}}
@media(max-width:700px){{body{{margin:0;padding:24px;box-shadow:none}}}}
</style></head><body>{body}</body></html>"""
    )


@router.get("/profiles/{profile_id}/resume-file")
async def resume_file(
    request: Request,
    profile_id: uuid.UUID,
    download: bool = Query(default=False),
    access_token: str | None = Query(default=None),
    user: CurrentUser = Depends(require_capability(caps.VIEW_REVIEW_SCREEN)),
    session: AsyncSession = Depends(get_tenant_db),
) -> Response:
    """Proxy an authorized resume without exposing a raw storage URL.

    The token round trip below redirects to THIS route. The target is taken
    from `request.url.path`, never written out by hand: this router is mounted
    at `/api/v1` only, and a hardcoded `/api/v2/candidates/...` here meant every
    resume view and every download 307ed to a path that does not exist and
    404ed. It read as a broken file (or, since only PDFs take this path while
    DOCX goes to `resume-preview`, as a broken FORMAT) rather than as a broken
    URL. Deriving the path means the mount and the redirect cannot drift again.
    """
    profile = await session.get(Profile, profile_id)
    if profile is None or not profile.resume_url:
        raise HTTPException(status_code=404, detail="Resume not found")
    link = await _link_for_profile_in_scope(session, user, profile.id)
    if link is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    await _require_full_profile_access(session, user)
    if access_token is None:
        query = urlencode(
            {
                "download": str(download).lower(),
                "access_token": issue_resume_token(profile_id, user.tenant_id),
            }
        )
        return RedirectResponse(
            url=f"{request.url.path}?{query}",
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Cache-Control": "private, no-store"},
        )
    try:
        verify_resume_token(access_token, profile_id, user.tenant_id)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=403, detail="Resume link is invalid or expired") from exc
    try:
        content = await fetch_resume_bytes(profile)
    except ResumeStorageError as exc:
        raise HTTPException(status_code=502, detail="Resume could not be loaded") from exc
    filename = (profile.resume_original_filename or "resume").replace('"', "")
    disposition = "attachment" if download else "inline"
    return Response(
        content=content,
        media_type=profile.resume_mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )




@router.delete("/links/{link_id}", response_model=LinkArchiveOut)
async def archive_candidate_application(
    link_id: uuid.UUID,
    user: CurrentUser = Depends(require_capability(caps.UPDATE_PIPELINE_STATUS)),
    session: AsyncSession = Depends(get_tenant_db),
) -> LinkArchiveOut:
    """Archive this job application without deleting the shared candidate."""
    link = await _get_link(session, user, link_id)
    if link.archived_at is None:
        link.archived_at = datetime.now(timezone.utc)
        await session.flush()
        await audit(
            session,
            tenant_id=user.tenant_id,
            actor_user_id=user.user_id,
            action="candidate_application_archived",
            target_type="job_candidate_link",
            target_id=link.id,
        )
    return LinkArchiveOut(link_id=link.id, archived=True)


@router.post("/links/{link_id}/restore", response_model=LinkArchiveOut)
async def restore_candidate_application(
    link_id: uuid.UUID,
    user: CurrentUser = Depends(require_capability(caps.UPDATE_PIPELINE_STATUS)),
    session: AsyncSession = Depends(get_tenant_db),
) -> LinkArchiveOut:
    link = await _get_link(session, user, link_id)
    if link.archived_at is not None:
        link.archived_at = None
        await session.flush()
        await audit(
            session,
            tenant_id=user.tenant_id,
            actor_user_id=user.user_id,
            action="candidate_application_restored",
            target_type="job_candidate_link",
            target_id=link.id,
        )
    return LinkArchiveOut(link_id=link.id, archived=False)


#: Display and tie-break order only; nothing scores these. Read from the one
#: place the vocabulary is defined so a fourth verdict cannot be added here and
#: nowhere else.
_TEAM_RATING_ORDER = list(team_review.VERDICTS)


def _clean_review_text(value: str) -> str:
    text_value = " ".join(value.strip().split())
    if not text_value:
        return text_value
    text_value = text_value[0].upper() + text_value[1:]
    if text_value[-1] not in ".!?":
        text_value += "."
    return text_value.replace(chr(8212), " - ")


async def _team_reviews_out(
    session: AsyncSession,
    link_id: uuid.UUID,
    current_user_id: uuid.UUID,
) -> TeamReviewsOut:
    rows = (
        await session.execute(
            select(CandidateTeamReview, User.full_name, User.email)
            .join(User, User.id == CandidateTeamReview.reviewer_user_id)
            .where(CandidateTeamReview.job_candidate_link_id == link_id)
            .order_by(CandidateTeamReview.updated_at.desc(), CandidateTeamReview.id)
        )
    ).all()
    reviews = [
        TeamReviewOut(
            id=review.id,
            reviewer_user_id=review.reviewer_user_id,
            reviewer_name=full_name or email or "Team member",
            rating=review.rating,
            remarks=review.remarks,
            ai_rewritten_remarks=review.ai_rewritten_remarks,
            is_current_user=review.reviewer_user_id == current_user_id,
            created_at=review.created_at,
            updated_at=review.updated_at,
        )
        for review, full_name, email in rows
    ]
    if not reviews:
        return TeamReviewsOut(
            reviews=[], overall_rating=None, overall_remarks=None, review_count=0
        )

    ordered = sorted(_TEAM_RATING_ORDER.index(review.rating) for review in reviews)
    overall_rating = _TEAM_RATING_ORDER[ordered[(len(ordered) - 1) // 2]]
    observations = []
    for review in reviews:
        source = review.ai_rewritten_remarks or review.remarks
        first_sentence = source.split(". ", 1)[0].strip().rstrip(".")
        observations.append(f"{review.reviewer_name}: {first_sentence}")
    overall_remarks = (
        f"Team consensus from {len(reviews)} "
        f"{'review' if len(reviews) == 1 else 'reviews'}. "
        + "; ".join(observations)
    )[:1800]
    if not overall_remarks.endswith("."):
        overall_remarks += "."
    return TeamReviewsOut(
        reviews=reviews,
        overall_rating=overall_rating,
        overall_remarks=overall_remarks,
        review_count=len(reviews),
    )


@router.get("/links/{link_id}/team-reviews", response_model=TeamReviewsOut)
async def team_reviews(
    link_id: uuid.UUID,
    user: CurrentUser = Depends(require_capability(caps.VIEW_REVIEW_SCREEN)),
    session: AsyncSession = Depends(get_tenant_db),
) -> TeamReviewsOut:
    await _get_link(session, user, link_id)
    return await _team_reviews_out(session, link_id, user.user_id)


@router.put("/links/{link_id}/team-reviews", response_model=TeamReviewsOut)
async def save_team_review(
    link_id: uuid.UUID,
    body: TeamReviewIn,
    user: CurrentUser = Depends(require_capability(caps.DECIDE_PROFILE)),
    session: AsyncSession = Depends(get_tenant_db),
) -> TeamReviewsOut:
    link = await _get_link(session, user, link_id)
    existing = (
        await session.execute(
            select(CandidateTeamReview).where(
                CandidateTeamReview.job_candidate_link_id == link.id,
                CandidateTeamReview.reviewer_user_id == user.user_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = CandidateTeamReview(
            tenant_id=user.tenant_id,
            job_candidate_link_id=link.id,
            reviewer_user_id=user.user_id,
            rating=body.rating,
            remarks=_clean_review_text(body.remarks),
            ai_rewritten_remarks=(
                _clean_review_text(body.ai_rewritten_remarks)
                if body.ai_rewritten_remarks
                else None
            ),
        )
        session.add(existing)
    else:
        existing.rating = body.rating
        existing.remarks = _clean_review_text(body.remarks)
        existing.ai_rewritten_remarks = (
            _clean_review_text(body.ai_rewritten_remarks)
            if body.ai_rewritten_remarks
            else None
        )
        existing.updated_at = datetime.now(timezone.utc)
    await session.flush()
    await audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.user_id,
        action="candidate_team_review_saved",
        target_type="job_candidate_link",
        target_id=link.id,
        metadata={"rating": body.rating, "used_ai_rewrite": bool(body.ai_rewritten_remarks)},
    )
    return await _team_reviews_out(session, link.id, user.user_id)


@router.post(
    "/links/{link_id}/team-reviews/rewrite",
    response_model=TeamReviewRewriteOut,
)
async def rewrite_team_review(
    link_id: uuid.UUID,
    body: TeamReviewRewriteIn,
    user: CurrentUser = Depends(require_capability(caps.DECIDE_PROFILE)),
    session: AsyncSession = Depends(get_tenant_db),
) -> TeamReviewRewriteOut:
    await _get_link(session, user, link_id)
    fallback = _clean_review_text(body.remarks)
    used_ai = False
    rewritten = fallback
    try:
        raw = await llm_router.invoke_llm(
            "report_synthesis",
            [
                {
                    "role": "system",
                    "content": (
                        "Rewrite a hiring-team observation for clarity and professionalism. "
                        "Preserve every factual claim and the reviewer's meaning. Do not add "
                        "a decision, score, protected characteristic or new evidence. Return "
                        "only the revised remark in 25 to 80 words."
                    ),
                },
                {"role": "user", "content": body.remarks},
            ],
            session=session,
            timeout=8,
            total_budget=12,
        )
        candidate = _clean_review_text(raw.strip().strip("\"'"))
        if 10 <= len(candidate.split()) <= 120:
            rewritten = candidate
            used_ai = True
    except (llm_router.LLMUnavailableError, ValueError):
        pass
    await audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.user_id,
        action="candidate_team_review_rewritten",
        target_type="job_candidate_link",
        target_id=link_id,
        metadata={"used_ai": used_ai},
    )
    return TeamReviewRewriteOut(rewritten_remarks=rewritten, used_ai=used_ai)


@router.post(
    "/links/{link_id}/interviews",
    response_model=InterviewOut,
    status_code=status.HTTP_201_CREATED,
)
async def schedule_interview(
    link_id: uuid.UUID,
    body: InterviewIn,
    user: CurrentUser = Depends(require_capability(caps.SCHEDULE_INTERVIEWS)),
    session: AsyncSession = Depends(get_tenant_db),
) -> InterviewOut:
    """Interview invite with .ics, sent ONLY from the tenant's verified
    sending domain (FR-8.3 / claude.md rule 5), never Gmail/Outlook."""
    link = await _get_link(session, user, link_id)
    candidate = await session.get(Candidate, link.candidate_id)
    tenant = await session.get(Tenant, user.tenant_id)
    if candidate is None or tenant is None:
        raise HTTPException(status_code=404, detail="Candidate or tenant missing")

    job = await session.get(Job, link.job_id)
    job_title = job.title if job is not None else "the role"

    ics_uid = f"{uuid.uuid4()}@{tenant.domain}"
    sent_from = f"no-reply@{tenant.domain}"
    interview = Interview(
        tenant_id=user.tenant_id, job_candidate_link_id=link.id,
        scheduled_at=body.scheduled_at, sent_from_email=sent_from,
        ics_uid=ics_uid, notes=body.notes,
    )
    session.add(interview)
    await session.flush()

    # THE .ICS IS BUILT AND ATTACHED HERE, which it was not until 2026-09-01.
    #
    # This route minted an `ics_uid`, passed it inside the template context and
    # stopped there. The docstring above, ESD section 12 and the PRD all say an
    # interview invite carries a calendar attachment; `email_render.build_ics`
    # has existed the whole time and nothing called it, so every invitation
    # went out with no way to accept it into a calendar. The dead-code gate is
    # what surfaced the function, and the function is what surfaced the gap.
    #
    # `pickready.send_email` takes `attachments` as
    # [{"filename": str, "content": <base64 str>}] and `smtp_service` decodes
    # the base64 back to bytes for the MIME part.
    ics = email_render.build_ics(
        uid=ics_uid,
        summary=f"{job_title} interview with {tenant.name}",
        starts_at=body.scheduled_at,
        organizer_email=sent_from,
        attendee_emails=[candidate.email] if candidate.email else [],
        description=body.notes or "",
    )
    # After the COMMIT: the invitation names the interview row written above,
    # and a rolled-back schedule must not send a calendar invite for a slot
    # that does not exist. A lost invoke is logged at ERROR by the commit hook.
    dispatch_after_commit(
        session,
        "pickready.send_email",
        args=[
            str(user.tenant_id), candidate.email, "interview_invite",
            {
                # The three the template actually addresses the candidate with.
                # Without them an unknown placeholder renders as "", so the
                # invitation went out as "Dear ," / "the position at ,".
                "candidate_name": candidate.full_name or "there",
                "job_title": job_title,
                "company_name": tenant.name,
                "scheduled_at": body.scheduled_at.isoformat(),
                "notes": body.notes,
                "ics_uid": ics_uid,
                "interview_id": str(interview.id),
            },
            [
                {
                    "filename": "interview.ics",
                    "content": base64.b64encode(ics).decode("ascii"),
                }
            ],
        ],
    )
    await audit(session, tenant_id=user.tenant_id, actor_user_id=user.user_id,
                action="interview_scheduled", target_type="interview",
                target_id=interview.id, metadata={"link_id": str(link.id)})
    return InterviewOut.model_validate(interview)


# ── Background verification, recruiter view (add-features spec 2026-09-05) ──


@router.get("/{candidate_id}/bgv")
async def get_bgv_results(
    candidate_id: uuid.UUID,
    # Company-wide like every other BGV surface (`api/bgv.py`): the inquiries
    # are the candidate's across every job in the tenant, with no department-
    # shaped subset, so a department-scoped principal is refused outright.
    user: CurrentUser = Depends(require_organisation_wide(caps.VIEW_REVIEW_SCREEN)),
    session: AsyncSession = Depends(get_tenant_db),
) -> dict:
    """Background-verification results, gated by the candidate's own consent.

    Access mirrors `/project-evidence` exactly: VIEW_REVIEW_SCREEN, the
    candidate must be linked to a job in THIS tenant (a candidate who is not
    answers 404, never 403 -- a cross-tenant read must not confirm
    existence), and non-HR holders need the HM grant.

    The consent boundary is the locked spec decision: an inquiry is shown
    ONLY where the candidate wrote a `bgv_share_consents` row for this
    tenant. An unshared inquiry appears as a single unshared marker carrying
    NO employer name, no mailbox and no fields, so the section can honestly
    read "Not shared by the candidate" without leaking who was asked. The
    raw reply email is never serialized to anyone; recruiters read the
    parsed fields and the domain-match word only.
    """
    from app.models.bgv import BGVInquiry, BGVShareConsent

    candidate = await session.get(Candidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    linked = (
        await session.execute(
            select(JobCandidateLink.id)
            .where(
                JobCandidateLink.candidate_id == candidate_id,
                JobCandidateLink.tenant_id == user.tenant_id,
            )
            .limit(1)
        )
    ).scalars().first()
    if linked is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    await department_access.require_candidate_in_scope(session, user, candidate_id)
    await _require_full_profile_access(session, user)

    inquiries = (
        await session.execute(
            select(BGVInquiry)
            .where(BGVInquiry.candidate_id == candidate_id)
            .order_by(BGVInquiry.created_at)
        )
    ).scalars().all()
    shared_ids = {
        row
        for row in (
            await session.execute(
                select(BGVShareConsent.bgv_inquiry_id).where(
                    BGVShareConsent.bgv_inquiry_id.in_(
                        [i.id for i in inquiries]
                    ),
                    BGVShareConsent.tenant_id == user.tenant_id,
                )
            )
        ).scalars()
    } if inquiries else set()

    items: list[dict] = []
    for inquiry in inquiries:
        if inquiry.id in shared_ids:
            items.append(
                {
                    "shared": True,
                    "employer_name": inquiry.employer_name,
                    "departmental_email": inquiry.departmental_email,
                    "domain_match_result": inquiry.domain_match_result,
                    "status": inquiry.status,
                    "inquiry_sent_at": (
                        inquiry.inquiry_sent_at.isoformat()
                        if inquiry.inquiry_sent_at
                        else None
                    ),
                    "response_received_at": (
                        inquiry.response_received_at.isoformat()
                        if inquiry.response_received_at
                        else None
                    ),
                    "parsed_fields": inquiry.parsed_fields_json,
                }
            )
        else:
            items.append(
                {"shared": False, "note": "Not shared by the candidate"}
            )
    # Destinations two and three of the brief's "three destinations"
    # (vivekium feature 6): the BGV record and the candidate page in the
    # Executive Profile both read the SAME candidate_consents table the
    # candidate's own portal reads, so the three surfaces cannot disagree.
    # Stamps and server-authored wording only; no free text of the
    # candidate's crosses here.
    from app.services import consent_catalog

    return {
        "inquiries": items,
        "consent_items": await consent_catalog.items_for(session, candidate_id),
    }
