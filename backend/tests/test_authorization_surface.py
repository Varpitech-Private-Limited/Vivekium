"""Every mutating endpoint is authorized at the API layer, by something.

WHY A SWEEP AND NOT MORE UNIT TESTS
-----------------------------------
The RBAC engine is well tested, and so is each capability. What no test covered
is the gap between them: a NEW route that is authenticated but never gated.
That is not a bug in any component, it is a missing line, and a missing line
produces no failure -- the route works, for everybody who is signed in.

Section 1 requires the HR Head / Recruiter / Hiring Manager matrix to be
enforced at the API layer and not merely hidden in the UI. This is the check
that the enforcement is actually attached.

WHAT COUNTS AS A GATE
---------------------
Five mechanisms, and all five are real authorization, not four plus an excuse:

  require_capability(...)      the RBAC engine, for tenant staff
  require_bd_capability(...)   the same idea for platform BD staff, who have
                               no tenant and so cannot use the tenant gate
  get_superadmin_db            owner audience only, and audits every request
  candidate session            the candidate portal audience
  a signed token in the path   the token IS the authorization: the caller
                               proved possession of something we signed

Anything a caller can mutate that has none of these is reported by path, so the
failure names the route rather than a count.

Ran on 2026-08-12 across every router. Every mutating route resolves to one of
these mechanisms, plus two named exemptions kept as EXPLICIT LISTS rather than
patterns: `PUBLIC_BY_DESIGN` (six routes, each with its reason -- the sign-in
endpoint cannot require a session, a webhook carries its own token) and
`SELF_SERVICE` (`/me`, where the session scope is the authorization because no
other row is reachable). No hole was found. The value is in the NEXT route:
adding an exemption is a line in a diff that somebody has to justify.
"""
from __future__ import annotations

import inspect

import pytest

from app.api import (
    admin,
    assessment_coding,
    assessment_conversation,
    assessment_recording,
    assessment_reports,
    auth,
    bd,
    billing,
    candidates,
    companies,
    company_onboarding,
    dashboard,
    emails,
    job_setup,
    jobs,
    matching,
    outreach,
    pipeline,
    portal,
    proctoring,
    provider,
    telemetry,
    verification,
)

ROUTERS = {
    "admin": admin,
    # The PRISM Report, its PDF, its citations and the transcript (PLAN-p5
    # WP5-F): what was left of `assessments` once everything else was carved
    # out of it, moved whole with every URL unchanged.
    "assessment_reports": assessment_reports,
    # Carved out of `assessments` on 2026-09-24 (PLAN-p3 WP0). Listed so the
    # moved routes stay under this sweep rather than leaving it with the move.
    "assessment_conversation": assessment_conversation,
    # Phase 4 WP-4C: the coding Run and final-answer state routes, candidate
    # audience. The Run is a write, so it belongs under this sweep.
    "assessment_coding": assessment_coding,
    "assessment_recording": assessment_recording,
    "auth": auth,
    "bd": bd,
    "billing": billing,
    "candidates": candidates,
    "companies": companies,
    # Company self-registration (2026-09-29). Every write here is made by
    # somebody with no account yet, so none asks for a session. Later steps
    # are authorized by a security code or the onboarding cookie that code
    # mints.
    "company_onboarding": company_onboarding,
    "dashboard": dashboard,
    "emails": emails,
    # The setup checklist, the Skills step and the SWOT routes (Vivekium
    # release), moved out of `assessments` and swept here for the same reason.
    "job_setup": job_setup,
    "jobs": jobs,
    "matching": matching,
    "outreach": outreach,
    "pipeline": pipeline,
    "portal": portal,
    "proctoring": proctoring,
    "provider": provider,
    "telemetry": telemetry,
    "verification": verification,
}

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

#: Dependencies that ARE authorization, spelled as they appear in a signature.
GATES = (
    "require_capability",
    # RBAC 3's full chain: tenant, the 24 ceiling, the grant, per-job
    # assignment scope and resource state, run BEFORE the handler. Added
    # 2026-08-29 with `rbac.require_authorized`, which is STRONGER than
    # `require_capability` rather than an alternative to it: the older gate
    # answers "may this role do this at all", which is still the right question
    # for a route with no resource in it.
    #
    # It is spelled as a qualname fragment because the dependency is a CLOSURE
    # (`require_authorized.<locals>.dependency`), so the signature string a
    # route renders says `Depends(dependency)` and names nothing. `_gate_for`
    # unwraps the parameter defaults for exactly that reason.
    "require_authorized",
    # `deps.require_organisation_wide` (the leadership release, 2026-09-29):
    # `require_capability` itself, plus a refusal for a department-scoped
    # account on a company-wide surface. A closure, spelled as a qualname
    # fragment for the reason above.
    "require_organisation_wide",
    "require_bd_capability",
    "get_superadmin_db",
    "get_current_candidate",
    "get_candidate_db",
    "get_current_any",
    # The onboarding cookie (company self-registration): a signed token with
    # its own audience, minted only by a right security code, naming one
    # registration. It is the authorization of every registration step after
    # the code, and it grants nothing a portal session grants.
    "require_onboarding_cookie",
)

#: Routes authorized by possession of a signed token rather than by a session.
#: The token in the path is the credential, so there is no session to gate.
TOKEN_AUTHORIZED = (
    "{token}",
    "/webhook",
)

#: Routes that are UNAUTHENTICATED ON PURPOSE, each with the reason.
#:
#: A map rather than a pattern, so adding one is a deliberate, reviewable line
#: in a diff rather than a route that happens to match a wildcard. Every entry
#: was read and justified on 2026-08-12.
PUBLIC_BY_DESIGN: dict[str, str] = {
    # The authentication endpoint itself. It cannot require authorization: it
    # is what produces the session. Rate limited instead (services/rate_limit).
    "/firebase/session": "creates the session",
    # Public demo sign-in (A-23, 2026-10-30). 404 unless DEMO_MODE is on.
    # Rate limited. It never binds a real account.
    "/demo/session": "public demo session, 404 unless DEMO_MODE is on",
    # Public demo company registration. Same flag. No CAPTCHA, no Razorpay.
    "/demo": "public demo company registration, 404 unless DEMO_MODE is on",
    # Forgotten password. The person cannot sign in, which is the whole
    # point. The request is rate limited and answers the same sentence for
    # every address. The verify needs the six-digit code that went to the
    # mailbox. The complete needs the single-use ticket the verify minted.
    "/password-reset/request": "cannot sign in, rate limited, same sentence for every address",
    "/password-reset/verify": "security code from the mailbox",
    "/password-reset/complete": "single-use reset ticket",
    # Authorized by a single-use, short-lived context_token in the body, minted
    # by /firebase/session moments earlier.
    "/select-context": "single-use context token",
    # Authorized by the refresh cookie, which is path-scoped to /api/v1/auth
    # and is never sent to an ordinary API call.
    "/refresh": "refresh cookie",
    # Ends the caller's own session by clearing cookies. Reads nothing, writes
    # nothing; requiring a valid session to log out would strand anyone whose
    # session had already expired.
    "/logout": "clears the caller's own cookies, touches no row",
    # Anonymous landing-page telemetry. There is no caller to authorize.
    "/landing-view": "anonymous, pre-account",
    # Inbound mail webhook. Authorized by the signed verification token the
    # handler extracts from the recipient address or the quoted body; a message
    # carrying no valid token reaches nothing.
    "/inbound-email": "signed token inside the message",
    # The renewal link in the six-month consent letter (feature 8). The token
    # is single use (renewal clears the stored hash), short lived, stored only
    # as a SHA-256 hash, bound to one candidate, and accepted by this route
    # and NOTHING else: it mints no session, identifies the holder to nothing,
    # and its only effect is that a profile which was going to be deleted is
    # not. A session dependency would defeat it, because the reader is by
    # definition somebody who has not signed in for six months.
    "/consent/renew": "single-use renewal token, keeps a profile and nothing else",
    # Company self-registration (2026-09-29). The person has no account, so
    # these cannot ask for one. The register step is rate limited and answers
    # the same sentence for every address; the resend writes only to a
    # registration already started; the verify needs the six-digit code from
    # the mailbox.
    "/register": "rate limited, same sentence for every address",
    "/code/resend": "writes only to a registration already started, rate limited",
    "/code/verify": "security code from the mailbox",
}

#: Routes that mutate ONLY the caller's own record.
#:
#: The session scope IS the authorization: there is no target to choose and no
#: other row reachable. A capability here would be asking "may you edit
#: yourself?".
SELF_SERVICE = ("/me",)

#: Routes that mutate NOTHING because they exist to refuse.
#:
#: `PATCH/PUT/DELETE /reports/links/{id}` are registered handlers that always
#: answer 403. They are deliberate: without them FastAPI would answer 405, and
#: "method not allowed" reads as an oversight where "reports are immutable"
#: is the product rule (claude.md). They take no session and touch no row.
REFUSAL_ONLY = ("/reports/links/{link_id}",)


def _routes():
    for name, module in ROUTERS.items():
        for route in module.router.routes:
            methods = getattr(route, "methods", set()) & MUTATING
            if methods:
                yield name, sorted(methods)[0], route


def _gate_for(route) -> str | None:
    """The mechanism authorizing this route, or None."""
    signature = str(inspect.signature(route.endpoint))
    for gate in GATES:
        if gate in signature:
            return gate
    for dependency in getattr(route, "dependencies", []):
        call = getattr(dependency, "dependency", None) or getattr(dependency, "call", None)
        qualname = getattr(call, "__qualname__", "")
        for gate in GATES:
            if gate in qualname:
                return gate
    # A gate passed as a PARAMETER DEFAULT rather than in `dependencies=[...]`.
    #
    # `Depends.__repr__` prints the callable's `__name__`, so a gate built by a
    # factory renders as `Depends(dependency)` and the signature scan above
    # sees nothing. That is not an edge case: it is how every route using
    # `rbac.require_authorized` is written, and before this branch existed the
    # sweep reported seven correctly-gated routes as authorized by nothing --
    # which is the direction that matters, because the next reader would have
    # started adding them to an exceptions list.
    for parameter in inspect.signature(route.endpoint).parameters.values():
        call = getattr(parameter.default, "dependency", None)
        qualname = getattr(call, "__qualname__", "")
        for gate in GATES:
            if gate in qualname:
                return gate
    if any(marker in route.path for marker in TOKEN_AUTHORIZED):
        return "signed token in path"
    if route.path in PUBLIC_BY_DESIGN:
        return f"public by design: {PUBLIC_BY_DESIGN[route.path]}"
    if route.path in SELF_SERVICE and (
        "get_current_user" in signature or "get_current_any" in signature
    ):
        # Still requires a SESSION, just not a capability.
        return "self-service, session-scoped"
    if route.path in REFUSAL_ONLY:
        source = inspect.getsource(route.endpoint)
        if "403" in source or "FORBIDDEN" in source:
            return "always refuses"
    return None


def test_every_mutating_route_is_authorized_by_something() -> None:
    """The sweep. A new POST with no gate fails here, named."""
    ungated = [
        f"{module}: {method} {route.path}"
        for module, method, route in _routes()
        if _gate_for(route) is None
    ]
    assert not ungated, (
        "these endpoints mutate state and are authorized by nothing at the API "
        f"layer: {ungated}"
    )


def test_the_sweep_actually_sees_the_routers() -> None:
    """A sweep over an empty list passes forever.

    Not hypothetical in this repository: `test_platform_audit` resolved its
    scan root to a path that does not exist in the container and reported six
    product rules green while reading zero files.
    """
    found = list(_routes())
    assert len(found) > 60, f"the sweep found only {len(found)} mutating routes"
    modules = {module for module, _, _ in found}
    assert len(modules) > 8, f"only {modules} were reached"


def test_the_gate_detector_recognises_a_real_gate_and_rejects_a_bare_one() -> None:
    """A guard on the guard. A detector that matched everything would make the
    sweep above pass on an entirely ungated API."""
    from fastapi import APIRouter, Depends

    from app.api.deps import require_capability

    router = APIRouter()

    @router.post("/gated")
    async def _gated(_user=Depends(require_capability("view_review_screen"))) -> dict:
        return {}

    @router.post("/bare")
    async def _bare(body: dict) -> dict:
        return body

    gates = {route.path: _gate_for(route) for route in router.routes}
    assert gates["/gated"] == "require_capability"
    assert gates["/bare"] is None


@pytest.mark.parametrize("path", REFUSAL_ONLY)
def test_the_immutable_report_routes_still_refuse(path: str) -> None:
    """They are exempt from the capability sweep only because they always 403.
    If one ever grew a body, the exemption would be hiding a write."""
    for _module, _method, route in _routes():
        if route.path != path:
            continue
        source = inspect.getsource(route.endpoint)
        assert "403" in source or "FORBIDDEN" in source, route.path
        assert "session" not in str(inspect.signature(route.endpoint)), (
            f"{route.path} took a database session; it is meant only to refuse"
        )


def test_bd_routes_use_the_platform_gate_not_the_tenant_one() -> None:
    """A `bd` user has NO tenant, and `require_capability` resolves permissions
    through a tenant row. Gating a BD route with it would either crash or fall
    through to deny, so the two gates are not interchangeable."""
    for module, _method, route in _routes():
        if module != "bd":
            continue
        gate = _gate_for(route)
        assert gate != "require_capability", (
            f"{route.path} uses the tenant gate; bd users have no tenant"
        )
        assert gate is not None, route.path
