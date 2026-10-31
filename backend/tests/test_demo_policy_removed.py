"""The demo email allowlist and the demo retention sweep are gone (A-25, 2026-10-31).

Demo users are real users: their records are never expired by the application
and their mail goes through the normal transport. Nothing in live code
withholds a recipient or deletes a `public_demo` row on a schedule.
"""
from __future__ import annotations

import importlib
import re

import pytest

from app.core.config import Settings
from app.services import demo_access, security_email
from tests.removal_sweep import BACKEND, sweep

THIS_FILE = BACKEND / "tests" / "test_demo_policy_removed.py"

GONE = re.compile(
    r"demo_email_allowlist|DEMO_EMAIL_ALLOWLIST|demo_retention_days|DEMO_RETENTION_DAYS"
    r"|purge_public_demo|purge-public-demo|purge_expired|recipient_allowed"
    r"|ensure_withheld|DemoEmailWithheld|demo-withheld|tasks_demo"
)


def test_the_sweep_pattern_matches_a_sample() -> None:
    assert GONE.search("ensure_withheld(to)")


def test_the_module_and_settings_are_gone() -> None:
    assert not (BACKEND / "app" / "workers" / "tasks_demo.py").exists()
    with pytest.raises((ModuleNotFoundError, ImportError)):
        importlib.import_module("app.workers.tasks_demo")
    fields = set(Settings.model_fields)
    assert not {"demo_email_allowlist", "demo_retention_days"} & fields
    assert not hasattr(demo_access, "purge_expired")


def test_nothing_names_the_retired_policy() -> None:
    hits = sweep(GONE, exempt=(THIS_FILE,))
    assert not hits, hits


async def test_a_stranger_gets_mail_while_demo_mode_is_on(monkeypatch) -> None:
    settings = security_email.get_settings().model_copy(update={
        "demo_mode": True,
        "demo_mode_confirm": "public-demo",
        "email_transport": "ses",
    })
    monkeypatch.setattr(security_email, "get_settings", lambda: settings)
    sent: list[str] = []

    async def fake_send(**kwargs):
        sent.append(kwargs["to"])
        return "ses-message-id"

    from app.services import ses_service

    monkeypatch.setattr(ses_service, "send_email_async", fake_send)
    message_id = await security_email.deliver(
        to="stranger@example.net", subject="s", html="<p>h</p>", text="t"
    )
    assert message_id == "ses-message-id"
    assert sent == ["stranger@example.net"]
