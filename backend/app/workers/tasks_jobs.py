"""The sweep over the job ledger: `pickready.sweep_background_jobs`.

Registered through `registry._load_tasks`. All of the logic is in
`workers/jobs.sweep`; this is only the task wrapper that opens the session and
supplies the real transport (`dispatch.redrive`).
"""
from __future__ import annotations

import logging

from app.workers import jobs
from app.workers.registry import Route, task
from app.workers.runtime import _run, worker_session

logger = logging.getLogger(__name__)


@task(
    name="pickready.sweep_background_jobs",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason="a sweep: one run reads every tenant's job rows",
)
def sweep_background_jobs():
    """Re-drive unstarted jobs, settle silent workers, purge old rows.

    Idempotent: a row is leased before it is worked, the worker's own claim
    makes a double delivery harmless, and every bound ends in a stated failure
    class rather than a pending row.
    """
    from app.workers.dispatch import redrive

    async def _task():
        async with worker_session() as session:
            report = await jobs.sweep(session, redrive)
            if report.redriven or report.failed or report.purged:
                logger.info(
                    "job.sweep redriven=%d accepted=%d capacity_blocked=%d "
                    "failed=%d worker_lost=%d purged=%d",
                    report.redriven, report.accepted, report.blocked,
                    report.failed, report.lost, report.purged,
                )

    _run(_task())
