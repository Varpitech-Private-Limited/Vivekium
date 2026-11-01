"""Staff management + invitation rules — DB-free pure functions.

Covers role validation, the legacy Hiring Manager cap predicate, and the
invite token/lifecycle helpers that the /join flow depends on.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import role_hierarchy

from app.api.companies import (
    MAX_HIRING_MANAGERS,
    ROLE_LABELS,
    STAFF_ROLES,
    hiring_manager_cap_reached,
    validate_staff_role,
)
from app.models.enums import Role
from app.models.invite import (
    INVITE_ACCEPTED,
    INVITE_EXPIRED,
    INVITE_PENDING,
    INVITE_REVOKED,
    INVITE_TTL_DAYS,
    build_invite_link,
    generate_invite_token,
    hash_invite_token,
    invite_expiry,
    invite_state,
)


# ── Role validation (400 on anything but the 3 staff roles) ──────────────────

def test_the_manageable_staff_roles_are_accepted() -> None:
    assert validate_staff_role("recruitment_manager") == Role.recruitment_manager
    assert validate_staff_role("hr_manager") == Role.hr_manager
    assert validate_staff_role("recruiter") == Role.recruiter
    assert validate_staff_role("hiring_manager") == Role.hiring_manager


@pytest.mark.parametrize("forbidden", ["client", "super_admin", "candidate"])
def test_privileged_and_external_roles_are_rejected(forbidden: str) -> None:
    with pytest.raises(ValueError):
        validate_staff_role(forbidden)


@pytest.mark.parametrize("bogus", ["", "admin", "HR_MANAGER", "owner", "staff"])
def test_unknown_role_strings_are_rejected(bogus: str) -> None:
    with pytest.raises(ValueError):
        validate_staff_role(bogus)


def test_staff_roles_are_exactly_the_manageable_ones() -> None:
    """The customer's Super Admin seat is NOT creatable from the portal.

    It is minted at onboarding by the Provider. A portal that could mint another
    one would let a Recruitment Manager promote themselves past every rule in
    `services/role_hierarchy`.

    Interview Manager joined the set on 2026-08-29. RBAC_SPECIFICATION.md 7.3
    lists what the Super Admin may add, remove, activate and assign, and the
    list is verbatim: "HR Manager / Recruiter / Hiring Manager / Interview
    Manager". Recruitment Manager is this product's own tier and predates that
    document; it stays because real customers hold those accounts.

    `client` remains excluded and that is the sentence 7.1 is protecting: one
    active Super Admin per company, which a portal that could mint a second one
    would make unenforceable.
    """
    assert STAFF_ROLES == {
        Role.recruitment_manager,
        Role.hr_manager,
        Role.recruiter,
        Role.hiring_manager,
        Role.interview_manager,
        # The leadership roles (2026-09-29, spec 11.1): invited by the Super
        # Admin from the same Staff page, and by nobody else.
        Role.ceo,
        Role.md,
        Role.functional_head,
    }
    assert Role.client not in STAFF_ROLES


def test_every_role_in_the_hierarchy_has_a_human_label() -> None:
    """Including `client`, which is not creatable but IS displayed: the team
    screen lists the Super Admin alongside everyone else."""
    assert STAFF_ROLES < set(ROLE_LABELS)
    assert ROLE_LABELS[Role.hiring_manager] == "Hiring Manager"
    assert ROLE_LABELS[Role.client] == "Super Admin"
    assert ROLE_LABELS[Role.recruitment_manager] == "Recruitment Manager"


# ── The four-level hierarchy (spec §29) ──────────────────────────────────────

def test_the_hierarchy_is_super_admin_then_manager_then_recruiter_then_hm() -> None:
    assert role_hierarchy.rank(Role.client) < role_hierarchy.rank(
        Role.recruitment_manager
    )
    assert role_hierarchy.rank(Role.recruitment_manager) < role_hierarchy.rank(
        Role.recruiter
    )
    assert role_hierarchy.rank(Role.recruiter) < role_hierarchy.rank(
        Role.hiring_manager
    )


def test_the_legacy_hr_manager_ranks_with_the_recruitment_manager() -> None:
    """A role a customer already assigned must not silently change what its
    holder can do. Demoting every existing HR Manager to the bottom of a new
    ladder would do exactly that."""
    assert role_hierarchy.rank(Role.hr_manager) == role_hierarchy.rank(
        Role.recruitment_manager
    )


@pytest.mark.parametrize(
    "actor,target",
    [
        (Role.client, Role.recruitment_manager),
        (Role.client, Role.hiring_manager),
        (Role.recruitment_manager, Role.recruiter),
        (Role.recruiter, Role.hiring_manager),
    ],
)
def test_a_higher_role_manages_a_lower_one(actor, target) -> None:
    assert role_hierarchy.can_manage(actor, target) is True


@pytest.mark.parametrize(
    "actor,target",
    [
        # Upward: the whole point of the ladder.
        (Role.hiring_manager, Role.recruiter),
        (Role.recruiter, Role.client),
        # Sideways: two Recruiters editing each other would make the hierarchy
        # meaningless, because everyone at a level would hold everyone else's
        # permissions.
        (Role.recruiter, Role.recruiter),
        (Role.client, Role.client),
        (Role.recruitment_manager, Role.hr_manager),
        # Roles the hierarchy does not place must be refused, not crash.
        (Role.candidate, Role.recruiter),
        (Role.recruiter, Role.candidate),
        (None, Role.recruiter),
    ],
)
def test_a_peer_or_a_superior_is_refused(actor, target) -> None:
    assert role_hierarchy.can_manage(actor, target) is False


def test_a_manager_can_only_grant_what_they_hold() -> None:
    """Otherwise the hierarchy is a privilege-escalation ladder rather than a
    ceiling: a Recruiter grants a Hiring Manager billing access, then has that
    Hiring Manager grant it back."""
    mine = {"create_job", "view_databank"}
    assert role_hierarchy.grantable_capabilities(mine) == mine
    assert "manage_billing" not in role_hierarchy.grantable_capabilities(mine)


def test_subordinate_roles_are_offered_in_hierarchy_order() -> None:
    """The order is RBAC 6's authority hierarchy, not the enum's declaration
    order, and the assertion says so deliberately.

    RBAC 6 draws it as:

        Client Super Admin
                |
                +-- HR Manager
                |
                +-- Recruiter
                |
                +-- Hiring Manager
                |
                +-- Interview Manager

    Interview Manager is last because it is the least authoritative: 13.2 says
    they own neither the JD nor the hiring criteria and are not the designated
    publishers, and 13.5 lists eleven things they must not do. Nothing sits
    below them, so nothing is offered to them.
    """
    offered = role_hierarchy.subordinate_roles(Role.client)
    assert offered[0] in (Role.recruitment_manager, Role.hr_manager)
    # The leadership leaves follow the chain (2026-09-29): they sit directly
    # beneath the Super Admin and outside the chain, so the chain's order is
    # what is asserted, and Interview Manager is still its last tier.
    chain = [role for role in offered if role not in role_hierarchy.LEAF_ROLES]
    assert chain[-1] == Role.interview_manager
    assert offered[len(chain):] == list(role_hierarchy.LEAF_ROLES)
    assert offered.index(Role.recruiter) < offered.index(Role.hiring_manager)
    assert offered.index(Role.hiring_manager) < offered.index(Role.interview_manager)
    assert Role.client not in offered
    # An Interview Manager is the bottom of the ladder and creates nobody.
    assert role_hierarchy.subordinate_roles(Role.interview_manager) == []
    # A Hiring Manager may create only the tier beneath them.
    assert role_hierarchy.subordinate_roles(Role.hiring_manager) == [
        Role.interview_manager
    ]


def test_no_route_deletes_a_candidate_from_the_customer_portal() -> None:
    """Spec §29: the Super Admin has full access EXCEPT deleting a candidate.

    Enforced by ABSENCE, which is the strongest form: there is no delete route
    to gate. An application is ARCHIVED, and the shared candidate record
    survives because other tenants' applications point at it.
    """
    from app.api import candidates as candidates_api

    for route in candidates_api.router.routes:
        methods = getattr(route, "methods", set()) or set()
        if "DELETE" not in methods:
            continue
        # The one DELETE that exists archives an application; it does not
        # delete the candidate.
        assert route.path == "/links/{link_id}", route.path
        assert route.name == "archive_candidate_application"


# ── Hiring Manager cap (FR-2.2: max 5 ACTIVE; HR/Recruiter uncapped) ─────────

def test_cap_not_reached_below_five() -> None:
    for count in range(MAX_HIRING_MANAGERS):
        assert hiring_manager_cap_reached(count) is False


def test_cap_reached_at_exactly_five() -> None:
    # The 6th ACTIVE Hiring Manager must be refused (409 at the API layer).
    assert hiring_manager_cap_reached(MAX_HIRING_MANAGERS) is True


def test_cap_reached_above_five() -> None:
    assert hiring_manager_cap_reached(MAX_HIRING_MANAGERS + 3) is True


def test_disabled_hms_free_up_slots() -> None:
    # The caller counts only status != disabled — 5 total with 1 disabled
    # passes 4 into the predicate, which must allow another hire.
    active_after_one_disabled = MAX_HIRING_MANAGERS - 1
    assert hiring_manager_cap_reached(active_after_one_disabled) is False


def test_max_is_five() -> None:
    assert MAX_HIRING_MANAGERS == 5


# ── Invite tokens: unguessable, stored hashed, never recoverable ────────────

def test_tokens_are_unique_and_url_safe() -> None:
    tokens = {generate_invite_token() for _ in range(200)}
    assert len(tokens) == 200
    for token in tokens:
        assert len(token) >= 32
        assert all(ch.isalnum() or ch in "-_" for ch in token)


def test_hash_is_deterministic_sha256_hex() -> None:
    token = generate_invite_token()
    assert hash_invite_token(token) == hash_invite_token(token)
    assert len(hash_invite_token(token)) == 64
    assert hash_invite_token(token) != token  # never store the raw secret


def test_different_tokens_hash_differently() -> None:
    assert hash_invite_token("a") != hash_invite_token("b")


def test_link_points_at_the_join_page() -> None:
    link = build_invite_link("http://localhost:3000", "tok123")
    assert link == "http://localhost:3000/join?invite=tok123"


def test_link_tolerates_a_trailing_slash_on_the_base_url() -> None:
    # The host was `picready.com` here until 2026-08-29, missing the `k`. Only
    # a fixture, so it never shipped, but a misspelling in a test is where a
    # misspelling in a config default gets copied from.
    # RBAC_SPECIFICATION.md 15 gives the canonical public host as
    # `readypick.ai`. Correctly-spelled `pickready` identifiers elsewhere (the
    # Celery namespace, the cache-key prefix, the GCP and JWT names) are
    # deliberate per CLAUDE.md and are NOT the same thing.
    assert build_invite_link("https://readypick.ai/", "t") == (
        "https://readypick.ai/join?invite=t"
    )


@pytest.mark.parametrize("base", ["", "readypick.ai", "/join"])
def test_link_refuses_a_non_absolute_base_url(base: str) -> None:
    with pytest.raises(ValueError):
        build_invite_link(base, "t")


# ── Invite lifecycle ────────────────────────────────────────────────────────

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def test_expiry_is_seven_days_out() -> None:
    assert INVITE_TTL_DAYS == 7
    assert invite_expiry(NOW) == NOW + timedelta(days=7)


def test_fresh_invite_is_pending() -> None:
    assert invite_state(
        accepted_at=None, revoked_at=None, expires_at=invite_expiry(NOW), now=NOW
    ) == INVITE_PENDING


def test_past_expiry_is_expired() -> None:
    assert invite_state(
        accepted_at=None, revoked_at=None, expires_at=NOW - timedelta(seconds=1), now=NOW
    ) == INVITE_EXPIRED


def test_expiry_boundary_is_exclusive() -> None:
    # Exactly at expires_at the link is already dead — no grace second.
    assert invite_state(
        accepted_at=None, revoked_at=None, expires_at=NOW, now=NOW
    ) == INVITE_EXPIRED


def test_revoked_beats_expired() -> None:
    assert invite_state(
        accepted_at=None, revoked_at=NOW, expires_at=NOW - timedelta(days=1), now=NOW
    ) == INVITE_REVOKED


def test_accepted_beats_everything() -> None:
    # Single use: once burned, an accepted invite stays accepted forever.
    assert invite_state(
        accepted_at=NOW, revoked_at=NOW, expires_at=NOW - timedelta(days=99), now=NOW
    ) == INVITE_ACCEPTED


def test_naive_expiry_is_treated_as_utc() -> None:
    # Some drivers hand back naive datetimes; this must not raise.
    assert invite_state(
        accepted_at=None,
        revoked_at=None,
        expires_at=datetime(2026, 7, 26, 12, 0),
        now=NOW,
    ) == INVITE_PENDING
