"""`pickready.purge_public_demo`: daily retention for the public demo.

`workers/tasks.py` imports this module, and that import is the registration.
Permanent `is_demo` companies are not `public_demo` and are not deleted.
"""
from __future__ import annotations

import logging

from app.workers.registry import Route, task
from app.workers.runtime import _run, worker_session as _worker_session

logger = logging.getLogger(__name__)


@task(
    name="pickready.purge_public_demo",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "Deletes expired public_demo tenants and candidate users only. "
        "Permanent is_demo companies are not public_demo."
    ),
)
def purge_public_demo():
    """Delete public-demo rows older than the retention window."""
    from app.services.demo_access import purge_expired

    async def _task():
        async with _worker_session() as session:
            result = await purge_expired(session)
            # Savepoints land in this transaction. It commits once.
            await session.commit()
        logger.info(
            "demo.purge tenants_deleted=%s tenants_failed=%s "
            "candidates_deleted=%s candidates_failed=%s",
            result["tenants_deleted"],
            result["tenants_failed"],
            result["candidates_deleted"],
            result["candidates_failed"],
        )
        return result

    return _run(_task())
