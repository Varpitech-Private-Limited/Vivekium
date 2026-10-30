"""What the auth hardening removed stays removed (auth spec 2.6, 9, 11.4).

* The browser no longer changes or resets a password. Firebase's own reset
  email (`sendPasswordResetEmail`) went around the CAPTCHA and the security
  code, and the browser-side change (`updatePassword` after
  `reauthenticateWithCredential`) needed a second call to revoke sessions,
  which is why the route that took a fresh Firebase token to revoke them
  existed. The server changes the password now and revokes in the same
  request, so all three are gone, route included.
* The session exchange takes captcha_purpose, and that field is the portal
  filter; the separate `requested_portal` field is gone.
* No company surface offers Google: the invitation page, and the company
  sign-in page, carry no Google button.

`tests/removal_sweep.py` is the one sweep, whitespace normalised.
"""
from __future__ import annotations

import re

from tests.removal_sweep import BACKEND, REPO, sweep

THIS_FILE = BACKEND / "tests" / "test_auth_hardening_removed.py"

GONE = re.compile(
    r"sendPasswordResetEmail|reauthenticateWithCredential|\bupdatePassword\b"
    r"|/password-changed\b|\bpassword_changed\s*\(|requested_portal|requestedPortal"
)


def test_the_retired_route_is_not_mounted() -> None:
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert not [path for path in paths if path.endswith("/password-changed")]
    for path in (
        "/api/v1/auth/password-reset/request",
        "/api/v1/auth/password-change/complete",
        "/api/v1/companies/invites/{token}/setup-password",
    ):
        assert path in paths, path


def test_nothing_names_what_was_removed() -> None:
    hits = sweep(GONE, exempt=(THIS_FILE,))
    assert not hits, hits


def test_no_company_surface_offers_google() -> None:
    for relative in (
        "frontend/components/join-flow.tsx",
        "frontend/app/(auth)/company/login/page.tsx",
    ):
        text = (REPO / relative).read_text(encoding="utf-8")
        assert "GoogleMark" not in text and "GoogleProvider" not in text, relative
