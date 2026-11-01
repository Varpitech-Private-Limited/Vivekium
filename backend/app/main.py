"""FastAPI application entrypoint.

Routers live one-module-per-PRD-section under app/api (claude.md §2):
auth, admin (super-admin console), companies, jobs, candidates, matching,
verification, dashboard, portal (candidate portal).
"""
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import leadership as leadership_api
from app.api import (
    bgv,
    assessment_coding,
    assessment_conversation,
    assessment_recording,
    assessment_reports,
    admin,
    auth,
    bd,
    billing,
    candidates,
    companies,
    company_onboarding,
    conversations,
    dashboard,
    email_senders,
    emails,
    employer_pages,
    intelligence,
    job_setup,
    job_status,
    jobs,
    matching,
    outreach,
    pipeline,
    portal,
    proctoring,
    provider,
    support,
    telemetry,
    verification,
    videos,
)
from app.core.config import get_settings
from app.core.logging import RequestIdMiddleware, configure_logging

# Before anything logs. structlog was previously running unconfigured, beside
# the stdlib rather than through it; `configure_logging` is the one place that
# is settled. See app/core/logging.py.
configure_logging()

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", environment=get_settings().environment)
    # Warm the engine + session factory once at boot rather than on the first
    # request, so the very first page load does not pay for pool construction.
    from app.core.db import get_session_factory

    get_session_factory()
    # Delivery-credential preflight: log a loud WARNING (not a hard crash, dev
    # without keys must still boot) if the Gmail SMTP config is
    # missing. Gmail SMTP is the only outbound mail path (claude.md rule 5);
    # legacy email-provider integrations have been removed from the codebase.
    from app.core.config import preflight_delivery_config

    missing = preflight_delivery_config()
    if missing:
        log.warning("delivery.preflight_missing_keys", missing=missing)
    yield
    # Stop the realtime subscription BEFORE disposing the engine, so the loop
    # is torn down with nothing still awaiting a socket on it. `leave()` only
    # stops the reader when the last socket goes, which is not what a deploy
    # does: a deploy stops a task with sockets still open, so the reader and its
    # redis connection were never closed at all. On Windows that is not untidy
    # but fatal -- `ProactorEventLoop.close()` waits on outstanding overlapped
    # I/O, which hung `TestClient.__exit__` and with it the whole test suite,
    # silently and with no failing test to point at.
    from app.services import realtime

    await realtime.hub.shutdown()
    # Same argument, applied to the cache's connection pool. It is a
    # module-level client now that `tenant_cache` delegates to it, so the RBAC
    # read on the last request before a deploy leaves a live socket on this
    # loop. Closed BEFORE the engine for the same ordering reason, and the
    # close itself is guarded inside `cache.close`.
    from app.core import cache

    await cache.close()
    from app.core.db import get_engine
    await get_engine().dispose()


# THE INTERACTIVE DOCS ARE OFF ON ANY DEPLOYMENT A BROWSER CAN REACH.
#
# `/docs`, `/redoc` and `/openapi.json` were served unconditionally, so the full
# API surface of a multi-tenant hiring platform, every route, every schema and
# every field name, was published to anyone who typed the URL. That is not a
# vulnerability by itself, since each route still authorizes, but it hands an
# attacker the map for free and it is a one-line thing to stop.
#
# GATED ON `serves_over_https`, NOT ON `is_production`, AND THAT DISTINCTION IS
# THE WHOLE POINT. `is_production` is `environment == "production"`, and the
# deployment actually serving readypick.ai sets `ENVIRONMENT=pilot`. Gating on
# `is_production` would therefore have left the docs exposed on the live site
# while reading, in the diff, as though it had closed them.
#
# This repository has already been bitten by exactly that: `serves_over_https`
# exists because the auth cookie's `Secure` flag was tied to `is_production`,
# so pilot and staging issued cookies without it over genuine HTTPS origins.
# The property means "a browser reaches this deployment over TLS", which is the
# same question being asked here, so the two now agree by construction rather
# than by somebody remembering.
_settings = get_settings()
_docs_enabled = not _settings.serves_over_https

app = FastAPI(
    title="Vivekium API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    # `/openapi.json` DELIBERATELY STAYS OPEN, and this is the one place the
    # rule is not carried all the way. `scripts/smoke-test.sh` probes it
    # unauthenticated after every deploy and FAILS the deploy if it is not 200,
    # because the registered route list is how this project verifies that the
    # image it just shipped actually serves the contract it claims to. Closing
    # it here would break deployment verification silently, which trades a real
    # capability for a modest one: the schema names routes, and every one of
    # them still authorizes, whereas `/docs` additionally hands over a
    # point-and-click client for them.
    #
    # If the owner decides the schema should close too, `smoke-test.sh` around
    # lines 169 to 174 has to change IN THE SAME COMMIT, or the next deploy
    # fails for a reason nobody will connect to this line.
)

# ── Middleware ───────────────────────────────────────────────────────────────
# Starlette wraps in reverse order of registration: the LAST one added is the
# outermost. Registration order below is therefore CORS (innermost), then GZip,
# then the perf timer, then the request id (outermost).
#
# The request id goes on LAST, and that placement is the point: it binds the
# contextvar before anything downstream can log, so a line written by the perf
# timer, by CORS, or by a handler all carry the same id. The perf timer is
# consequently no longer the outermost middleware; what it stops measuring is
# one dict copy and a uuid4, which is not the latency anybody is chasing.

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        get_settings().frontend_url,
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Browsers otherwise re-run the OPTIONS preflight before EVERY cross-origin
    # request. A dashboard firing eight calls paid eight extra round trips; with
    # this the browser caches the preflight for 10 minutes.
    max_age=600,
    # Diagnostics headers must be readable by the browser's network panel.
    # `X-Request-Id` is here for the same reason: the browser has to be able to
    # read the id back to quote it in a bug report.
    expose_headers=["Server-Timing", "X-Query-Count", "X-Request-Id"],
)

# JSON list payloads (the candidate table, the jobs board, the customers list)
# compress roughly 5:1. Below 1 KB compression costs more than it saves.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Per-request timing + SQL query count (see app/core/instrumentation.py).
#
# GATED ON AN EXPLICIT OPT-IN, NOT ON `is_production`, AND THE REASON IS THAT
# THE OLD GATE WAS OPEN ON THE LIVE SITE. This read `if not
# get_settings().is_production`, `is_production` is `environment ==
# "production"`, and the deployment serving readypick.ai runs
# `ENVIRONMENT=pilot`. So the middleware was installed in production while
# `instrumentation.py` stated in its own docstring that it could not be:
# `Server-Timing` carrying a SQL duration on every response, and
# `X-Debug-SQL: 1` accepted from an anonymous caller.
#
# This is the third defect from that one root cause, after the unsigned
# Razorpay webhook and the exposed API docs, so it is deliberately NOT fixed by
# reaching for another derived property. `serves_over_https` would be wrong
# here as well, because it is true of staging where these diagnostics are
# wanted. See `Settings.expose_request_diagnostics`.
if get_settings().expose_request_diagnostics:
    from app.core.instrumentation import install_query_counter, timing_middleware

    install_query_counter()
    app.add_middleware(BaseHTTPMiddleware, dispatch=timing_middleware)

# Outermost. Registered after the conditional block above so the id is bound
# first in every environment, production included, where the timer is absent.
app.add_middleware(RequestIdMiddleware)

API_PREFIX = "/api/v1"
app.include_router(auth.router, prefix=f"{API_PREFIX}/auth", tags=["auth"])
app.include_router(admin.router, prefix=f"{API_PREFIX}/admin", tags=["admin"])
app.include_router(companies.router, prefix=f"{API_PREFIX}/companies", tags=["companies"])
app.include_router(jobs.router, prefix=f"{API_PREFIX}/jobs", tags=["jobs"])
app.include_router(job_status.router, prefix=f"{API_PREFIX}/job-status", tags=["job-status"])
app.include_router(candidates.router, prefix=f"{API_PREFIX}/candidates", tags=["candidates"])
app.include_router(matching.router, prefix=f"{API_PREFIX}/matching", tags=["matching"])
app.include_router(verification.router, prefix=f"{API_PREFIX}/verification", tags=["verification"])
app.include_router(outreach.router, prefix=f"{API_PREFIX}/outreach", tags=["outreach"])
app.include_router(dashboard.router, prefix=f"{API_PREFIX}/dashboard", tags=["dashboard"])
app.include_router(portal.router, prefix=f"{API_PREFIX}/portal", tags=["portal"])
app.include_router(telemetry.router, prefix=f"{API_PREFIX}/telemetry", tags=["telemetry"])
app.include_router(emails.router, prefix=f"{API_PREFIX}/emails", tags=["emails"])
# Corporate email senders (Corporate Email System spec, 2026-09-05): the
# registration/OTP/authorization lifecycle, the fixed template registry, and
# the SES delivery-event webhook. New in this release, mounted once.
app.include_router(
    email_senders.router,
    prefix=f"{API_PREFIX}/email-senders",
    tags=["email-senders"],
)
app.include_router(pipeline.router, prefix=f"{API_PREFIX}/pipeline", tags=["pipeline"])
app.include_router(provider.router, prefix=f"{API_PREFIX}/provider", tags=["provider"])
# Business Development Portal, the fourth portal (/bd in the UI and the API).
app.include_router(bd.router, prefix=f"{API_PREFIX}/bd", tags=["bd"])
# Credits, credit-pack purchases and the ledger. Mounted at one path only
# (no /api/v2 alias): it is new in this release, so there is no v1 client to keep working
# and a second prefix would just be a second URL for Razorpay's webhook to be
# configured against by mistake.
app.include_router(billing.router, prefix=f"{API_PREFIX}/billing", tags=["billing"])
# Company self-registration (owner spec 2026-09-29, section 5): the first
# Company Super Admin registers, verifies the mailbox, buys the first credit
# pack and sets a password. Public by design, one path only.
app.include_router(
    company_onboarding.router,
    prefix=f"{API_PREFIX}/company-onboarding",
    tags=["company-onboarding"],
)
# In-product customer support (2026-09-10), which replaced a deleted
# third-party sync (claude.md, 2026-09-10). Two routers, two audiences,
# one write path: `router` is the customer's
# own threads under the org audience, `provider_router` is Vivekium's queue
# across every customer and mounts under /provider beside the customer list,
# because that is where this product's Provider API lives. Mounted at one
# prefix only, no /api/v2 alias: the feature is new in this release, so there
# is no v1 client to keep working and a second URL for one surface is a second
# thing to keep in step.
app.include_router(support.router, prefix=f"{API_PREFIX}/support", tags=["support"])
# Background verification. ONE router, two audiences: `/bgv/me` runs on the
# candidate session and everything else is behind require_capability on the
# tenant session, so neither audience can reach the other's routes.
app.include_router(bgv.router, prefix=f"{API_PREFIX}/bgv", tags=["bgv"])
app.include_router(leadership_api.router, prefix=f"{API_PREFIX}/leadership", tags=["leadership"])
# Native conversations (recruiter to candidate, and the BGV threads with an
# employer's HR contact). The REST routes and the one WebSocket live together
# because they authorise identically: the socket is a NOTIFICATION channel over
# the same rows, never a second write path.
app.include_router(
    conversations.router, prefix=f"{API_PREFIX}/conversations", tags=["conversations"]
)
# The candidate's own side, on the CANDIDATE audience. Two routers under one
# prefix rather than one router with a branch inside it: the recruiter's routes
# run on a session whose tenant Postgres is enforcing, and a candidate has no
# tenant to enforce. One handler serving both would be one function with two
# security models, and the weaker one would be invisible in the code.
app.include_router(
    conversations.candidate_router,
    prefix=f"{API_PREFIX}/conversations",
    tags=["conversations"],
)
app.include_router(
    support.provider_router,
    prefix=f"{API_PREFIX}/provider/support",
    tags=["provider-support"],
)
# Talent Intelligence dashboards (2026-09-05 spec): operational metrics only,
# behind view_intelligence_dashboards.
app.include_router(
    intelligence.router, prefix=f"{API_PREFIX}/intelligence", tags=["intelligence"]
)
# PUBLIC employer pages (2026-09-05 add-features spec). Unauthenticated by
# design, like GET /jobs/public/{id}; mounted at one path only, new in this
# release with no v1/v2 split to honour.
app.include_router(
    employer_pages.router, prefix=f"{API_PREFIX}/employers", tags=["employers"]
)
# Client-portal video access (2026-09-05 dashboard/Executive Profile/video
# spec, sections 15-19): metadata plus the audited preview/download URLs.
# Mounted at one path only, new in this release with no v1/v2 split to honour.
app.include_router(videos.router, prefix=f"{API_PREFIX}/videos", tags=["videos"])
# The PRISM Report, its PDF, its citations and the recruiter's transcript
# (PLAN-p5 WP5-F). Moved out of `api/assessments.py`, which is gone, under the
# SAME prefix, so every URL a report link in an inbox carries is unchanged.
app.include_router(
    assessment_reports.router, prefix="/api/v2/assessments", tags=["assessments-v2"]
)
# Job setup (Vivekium release): the setup checklist, the Skills step and the
# Job SWOT routes, moved out of `assessments` under the same prefix so every
# URL is unchanged.
app.include_router(job_setup.router, prefix="/api/v2/assessments", tags=["assessments-v2"])
# The candidate side and the recording routes were carved out of the same
# module on 2026-09-24 (PLAN-p3 WP0). Same prefix and tag, included in the
# order the routes used to be declared, so every URL is unchanged.
app.include_router(
    assessment_conversation.router, prefix="/api/v2/assessments", tags=["assessments-v2"]
)
app.include_router(
    assessment_recording.router, prefix="/api/v2/assessments", tags=["assessments-v2"]
)
# The coding question's Run and final-answer state (Phase 4 WP-4C). Beside the
# conversation it belongs to, on the candidate audience, v2 only: it is new in
# this release and has no v1 client.
app.include_router(
    assessment_coding.router, prefix="/api/v2/assessments", tags=["assessments-v2"]
)
# Proctoring (proctoring-spec-doc.md). Mounted beside the assessment it
# monitors, under v2 only: it is new in this release and has no v1 client.
app.include_router(proctoring.router, prefix="/api/v2/proctoring", tags=["proctoring-v2"])

# ── /api/v2 aliases (2026-07-27 build spec) ──────────────────────────────────
# The spec names its new routes under /api/v2 (e.g. GET /api/v2/jobs/{job_id},
# PATCH /api/v2/companies/me/profile). The established contract is v1 and
# claude.md says to evolve additively rather than replace it, so the SAME
# router objects are mounted under both prefixes. One set of handlers, one set
# of permission checks — there is no second implementation to drift, and a v1
# client keeps working untouched.
app.include_router(jobs.router, prefix="/api/v2/jobs", tags=["jobs-v2"])
app.include_router(companies.router, prefix="/api/v2/companies", tags=["companies-v2"])
app.include_router(matching.router, prefix="/api/v2/matching", tags=["matching-v2"])
app.include_router(emails.router, prefix="/api/v2/emails", tags=["emails-v2"])
app.include_router(pipeline.router, prefix="/api/v2/pipeline", tags=["pipeline-v2"])
# The Provider Portal spec names its routes under /api/v2/provider. Same
# router object as the v1 mount above — one implementation, two prefixes.
app.include_router(provider.router, prefix="/api/v2/provider", tags=["provider-v2"])
app.include_router(bd.router, prefix="/api/v2/bd", tags=["bd-v2"])


@app.get("/health/live")
async def health_live() -> dict:
    """LIVENESS. Is this process able to serve? No dependency is touched.

    WHY THIS EXISTS, AND WHY IT IS NOT A WEAKER `/health`.
    ------------------------------------------------------
    `/health` below is a READINESS probe: it fails when Redis or the database
    is unreachable, and the reasoning for that is sound and is written out in
    `app/api/health.py`. A task with no Redis answers every assessment turn
    with a 503, and promoting it on a deploy would ship a broken release.

    The mistake was using that one endpoint to answer a SECOND question it is
    wrong for. The ALB target group polls it every thirty seconds with
    `unhealthy_threshold = 3`, and ECS replaces a task that fails its load
    balancer check. Redis is a single shared ElastiCache, so an outage there
    does not make ONE task unhealthy, it makes EVERY task unhealthy at once:

      * the target group empties, so the ALB answers 503 for the whole
        product, including jobs, candidates, billing and reports, all of
        which would otherwise have kept working;
      * ECS then kills the tasks, and the replacements fail the same check
        because Redis is still down, so it becomes a restart loop that also
        throws away every warm connection pool;
      * and there is nothing to route around TO, because the dependency is
        shared. Removing the tasks buys nothing and costs everything.

    A degraded assessment surface is strictly better than that.

    SO THE DEPLOY GATE IS NOT LOST, IT MOVED. `scripts/smoke-test.sh` runs
    after the rollout, probes `/health` (the deep one), and FAILS THE DEPLOY
    when it is not 200. The property the authors of `/health` cared about,
    that a release with a broken dependency does not ship, still holds. What
    no longer holds is that a dependency blip can take the running product
    down with it.

    Nothing here may grow a dependency. The moment this touches the database,
    Redis, or anything over a socket, it stops being a liveness probe and the
    restart loop above comes back.
    """
    return {"status": "ok"}


@app.get("/health")
async def health() -> dict:
    """READINESS, and the deploy gate. Fails when a dependency is unreachable.

    NOT the ALB target group's ongoing health check any more; `/health/live`
    above is, and that docstring explains why. This one is what
    `scripts/smoke-test.sh` probes after a rollout, which is where a release
    with a broken dependency is now stopped.

    The probes live in `app/api/health.py`; read the module docstring there for
    why the broker is checked alongside the database. In short: an unreachable
    Redis does not raise on publish, it HANGS, so a task with a broken broker
    accepts requests and stops partway through them with no error anywhere. A
    database-only probe promotes that task.
    """
    from app.api.health import probe_dependencies

    return await probe_dependencies()
