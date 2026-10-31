"""Public demo gate.

Session login and company registration live with their routes. This module
only answers whether the demo is on. Demo users and their records are never
expired or deleted by the application; email to them is the normal path.
"""
from __future__ import annotations

from fastapi import HTTPException

from app.core.config import get_settings

PORTAL_TO_FILTER = {
    "candidate": "candidate",
    "company": "org",
    "provider": "owner",
    "bd": "bd",
}


def enabled() -> bool:
    settings = get_settings()
    if not settings.demo_mode:
        return False
    environment = (settings.environment or "").strip().lower()
    if environment in {"production", "pilot"}:
        return settings.demo_mode_confirm == "public-demo"
    return True


def require_enabled() -> None:
    if not enabled():
        raise HTTPException(status_code=404, detail="Not found.")
