"""`pickready.send_security_email`: one security email, platform sender, SES.

`workers/tasks.py` imports this module, and that import IS the registration
(`registry.resolve` imports `app.workers.tasks` and nothing else).

The rules live in `services/security_email`: fixed templates, a context
checked key by key, the platform sender only, SES only in production, and a
context (which holds the code) that is never logged, audited or returned.
Every dispatch is its own Lambda invoke, so this never waits behind bulk
mail (`workers/dispatch`).
"""
from __future__ import annotations

import logging

from app.services.delivery_errors import TransientDeliveryError
from app.workers.registry import Route, task
from app.workers.runtime import _run, worker_session as _worker_session

logger = logging.getLogger(__name__)


async def _record_delivery(
    to: str, template: str, status: str, message_id: str | None, failure: dict
) -> None:
    from app.services.audit import audit

    async with _worker_session() as session:
        await audit(
            session,
            tenant_id=None,
            actor_user_id=None,
            action="email.delivery",
            target_type="email",
            target_id=message_id,
            metadata={
                "to": to,
                "template": template,
                "status": status,
                "sender_path": "platform_security",
                **({"failure": failure} if failure else {}),
            },
        )
        await session.commit()


@task(
    name="pickready.send_security_email",
    route=Route.LAMBDA,
    rls="bypass",
    rls_reason=(
        "a security email belongs to no tenant: it is addressed to a person who "
        "may have no account yet, and it writes only its platform-level "
        "delivery audit row"
    ),
    # Seconds of work, and time-sensitive: a person is waiting for the code.
    # A transient SES failure is retried quickly; a permanent one, or a
    # refusal (unknown template, SMTP in production), is not retried at all.
    max_attempts=3,
    backoff_seconds=3.0,
    backoff_max_seconds=10.0,
    retry_on=(TransientDeliveryError,),
)
def send_security_email(to: str, template: str, context: dict):
    """Render the fixed template and send it from the platform sender."""
    from app.services import email_render, security_email
    from app.services.delivery_errors import DeliveryError

    async def _task():
        subject, body = security_email.render(template, context)
        html = email_render.text_to_html(body)
        status = "sent"
        message_id = None
        failure: dict = {}
        try:
            message_id = await security_email.deliver(
                to=to, subject=subject, html=html, text=body
            )
        except DeliveryError as err:
            status = "failed"
            # The provider's error NAME only: a provider message can quote the
            # message it refused, and the message carries the code.
            failure = {"class": type(err).__name__, "error_name": err.error_name}
            raise
        except security_email.SecurityEmailRefused:
            status = "refused"
            raise
        finally:
            # The delivery record names the recipient and the template, never
            # the context: the context is where the code is.
            try:
                await _record_delivery(to, template, status, message_id, failure)
            except Exception:  # noqa: BLE001 - never mask the delivery outcome
                logger.exception("security_email.audit_failed template=%s", template)
        logger.info("security_email.sent template=%s", template)
        return {"status": "sent"}

    return _run(_task())
