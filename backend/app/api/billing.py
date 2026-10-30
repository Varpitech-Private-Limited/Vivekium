"""Monthly plans, Starter top-ups and the completed-assessment ledger API.

Route shape:

    GET  /billing/public/plans        anyone: published monthly prices
    GET  /billing/overview             customer: balance, usage, history
    GET  /billing/ledger               customer: paginated credit statement
    GET  /billing/credit-packs         customer: every pack priced for them
    POST /billing/purchase             customer: create a Razorpay Order
    POST /billing/purchase/verify      customer: verify the Checkout handler
    GET  /billing/purchases            customer: purchase history
    GET  /billing/purchases/{id}/invoice  customer: the GST invoice PDF
    POST /billing/webhook/razorpay     Razorpay: signature-verified, no session
    GET  /billing/provider/overview    Provider Portal: balances across customers

Monthly charges and Starter top-ups each have one idempotent settlement path.

The browser receives the Razorpay KEY ID on the response that opens Checkout
(`/purchase`) and on `/overview`. There is no separate public config route.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    get_public_db,
    get_superadmin_db,
    get_tenant_db,
    require_capability,
)
from app.models.billing import (
    CREDIT_VALIDITY_MONTHS,
    GST_RATE_PERCENT,
    PURCHASE_CREATED,
    PURCHASE_FAILED,
    PURCHASE_PAID,
    SUBUNITS_PER_CREDIT,
    BillingTransaction,
    CreditLedgerEntry,
    CreditPurchase,
    WebhookEvent,
)
from app.models.tenant import Tenant
from app.schemas.billing import (
    BillingOverviewOut,
    CreditLedgerEntryOut,
    CreditLotOut,
    CreditPackQuoteOut,
    CreditPacksOut,
    CreditPurchaseCreatedOut,
    CreditPurchaseIn,
    CreditPurchaseOut,
    CreditPurchaseVerifyIn,
    CreditSummaryOut,
    ProviderBillingRowOut,
    TransactionOut,
    UsageBreakdownOut,
)
from app.services import capabilities as caps
from app.services import credit_packs, credits, monthly_plans, razorpay
from app.services import demo_access
from app.services.audit import audit

log = logging.getLogger(__name__)

router = APIRouter()


class MonthlyPlanChoice(BaseModel):
    plan_slug: str


class MonthlyCheckoutProof(BaseModel):
    razorpay_subscription_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.get("/public/plans")
async def public_monthly_plans(response: Response) -> dict:
    response.headers["Cache-Control"] = "public, max-age=300"
    return {
        "plans": [
            {**plan.__dict__, "gst_inr": plan.gst_inr, "total_inr": plan.total_inr,
             "rollover_months": plan.rollover_months}
            for plan in monthly_plans.PLANS
        ],
        "gst_rate_percent": GST_RATE_PERCENT,
        "pilot_days": 30,
        "pilot_plan_slug": "starter",
        "topup_plan_slug": "starter",
    }


@router.get("/monthly/current")
async def current_monthly_plan(
    user: CurrentUser = Depends(require_capability(caps.VIEW_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> dict:
    tenant = await _tenant_or_404(session, user.tenant_id)
    return {
        "plan_slug": tenant.current_plan_slug,
        "pending_plan_slug": tenant.pending_plan_slug,
        "status": tenant.subscription_status,
        "current_end": tenant.subscription_current_end,
    }


@router.get("/monthly/charges")
async def monthly_charges(
    user: CurrentUser = Depends(require_capability(caps.VIEW_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> list[dict]:
    rows = (await session.execute(
        select(BillingTransaction)
        .where(BillingTransaction.tenant_id == user.tenant_id,
               BillingTransaction.transaction_type == "subscription_charge")
        .order_by(BillingTransaction.created_at.desc()).limit(100)
    )).scalars().all()
    return [
        {"id": row.id, "plan_slug": row.plan_slug,
         "assessments_granted": row.assessments_granted,
         "subtotal_inr": row.subtotal_inr, "gst_inr": row.gst_inr,
         "amount_inr": row.amount_inr, "invoice_number": row.invoice_number,
         "created_at": row.created_at}
        for row in rows
    ]


@router.get("/monthly/charges/{charge_id}/invoice")
async def download_monthly_invoice(
    charge_id: uuid.UUID,
    user: CurrentUser = Depends(require_capability(caps.VIEW_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> Response:
    charge = (await session.execute(
        select(BillingTransaction).where(
            BillingTransaction.id == charge_id,
            BillingTransaction.tenant_id == user.tenant_id,
            BillingTransaction.transaction_type == "subscription_charge",
            BillingTransaction.status == "success",
        )
    )).scalars().first()
    if charge is None:
        raise HTTPException(status_code=404, detail="No invoice for this charge")
    tenant = await _tenant_or_404(session, user.tenant_id)
    return Response(
        content=monthly_plans.render_charge_invoice_pdf(charge, tenant),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{charge.invoice_number}.pdf"'},
    )


@router.post("/monthly/subscribe")
async def subscribe_monthly(
    body: MonthlyPlanChoice,
    user: CurrentUser = Depends(require_capability(caps.MANAGE_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> dict:
    tenant = await _tenant_or_404(session, user.tenant_id)
    _refuse_public_demo_gateway(tenant)
    try:
        return await monthly_plans.begin_subscription(session, tenant, body.plan_slug)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (razorpay.RazorpayError, razorpay.RazorpayNotConfigured) as exc:
        raise _razorpay_or_503(exc) from exc


@router.post("/monthly/verify")
async def verify_monthly_checkout(
    body: MonthlyCheckoutProof,
    user: CurrentUser = Depends(require_capability(caps.MANAGE_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> dict:
    if not razorpay.verify_subscription_signature(
        subscription_id=body.razorpay_subscription_id,
        payment_id=body.razorpay_payment_id,
        signature=body.razorpay_signature,
    ):
        raise HTTPException(status_code=400, detail="Payment could not be verified")
    tenant = await _tenant_or_404(session, user.tenant_id)
    _refuse_public_demo_gateway(tenant)
    if tenant.razorpay_subscription_id != body.razorpay_subscription_id:
        raise HTTPException(status_code=403, detail="Payment belongs to another company")
    payment = await razorpay.fetch_payment(body.razorpay_payment_id)
    if payment.get("status") != "captured" or payment.get("currency") != "INR":
        raise HTTPException(status_code=409, detail="Payment has not been captured")
    plan = monthly_plans.BY_SLUG[tenant.current_plan_slug]
    amount_paise = int(payment["amount"])
    granted = False
    if amount_paise == plan.total_inr * razorpay.PAISE_PER_RUPEE:
        granted = await monthly_plans.settle_charge(
            session, tenant=tenant, subscription_id=body.razorpay_subscription_id,
            payment_id=body.razorpay_payment_id, amount_inr=plan.total_inr,
        )
    else:
        if tenant.subscription_status != "active":
            tenant.subscription_status = "pending"
    return {"granted": granted, "status": tenant.subscription_status}


@router.post("/monthly/change-plan")
async def change_monthly_plan(
    body: MonthlyPlanChoice,
    user: CurrentUser = Depends(require_capability(caps.MANAGE_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> dict:
    tenant = await _tenant_or_404(session, user.tenant_id)
    _refuse_public_demo_gateway(tenant)
    plan = monthly_plans.BY_SLUG.get(body.plan_slug)
    if not plan:
        raise HTTPException(status_code=404, detail="Unknown monthly plan")
    if not tenant.razorpay_subscription_id or tenant.subscription_status != "active":
        raise HTTPException(status_code=409, detail="No active subscription")
    if tenant.current_plan_slug == plan.slug:
        raise HTTPException(status_code=409, detail="Already on this plan")
    gateway_id = await monthly_plans.gateway_plan_id(session, plan)
    await razorpay.change_subscription_plan(tenant.razorpay_subscription_id, gateway_id)
    tenant.pending_plan_slug = plan.slug
    return {"plan_slug": tenant.current_plan_slug, "pending_plan_slug": plan.slug}


@router.post("/monthly/cancel")
async def cancel_monthly(
    user: CurrentUser = Depends(require_capability(caps.MANAGE_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> dict:
    tenant = await _tenant_or_404(session, user.tenant_id)
    _refuse_public_demo_gateway(tenant)
    if not tenant.razorpay_subscription_id or tenant.subscription_status != "active":
        raise HTTPException(status_code=409, detail="No active subscription")
    await razorpay.cancel_subscription(tenant.razorpay_subscription_id)
    tenant.subscription_status = "cancelling"
    return {"status": "cancelling", "current_end": tenant.subscription_current_end}

# ── Customer: overview ───────────────────────────────────────────────────────

_DEFICIT_MESSAGE = (
    "You are over your credit limit. New assessment invitations are paused "
    "until you buy more credits."
)

#: Shown when the pool reads zero. Names BOTH blocked actions, because a
#: recruiter who reads "assessments are paused" and then cannot create a job
#: has been told half the truth and will report it as a second bug.
_EXHAUSTED_MESSAGE = (
    "Your credit pool is exhausted. New jobs cannot be created and no further "
    "candidates can be moved into assessment. Purchase a credit bundle to "
    "continue. A conversation already in progress will finish, and its report "
    "is written as soon as credits are available."
)

def _warning_message(
    level: int, balance: Decimal, estimate: int, stem_active: bool
) -> str | None:
    """The Master Directive Part 5 §4.1 alert copy, tier by tier, with the
    §4.2 estimate folded in. Composed server-side so the banner, the email and
    the 402 refusal cannot describe one situation three different ways."""
    if level <= 0:
        return None
    stem_note = ""
    if level >= 2:
        return (
            f"Critical: Only {balance} credits remaining. Some assessments may "
            f"not complete. At current usage, this covers approximately "
            f"{estimate} more assessments.{stem_note} Top up immediately."
        )
    return (
        f"Credits running low. You have {balance} credits remaining. At current "
        f"usage, this covers approximately {estimate} more assessments."
        f"{stem_note} Top up now to keep your pipeline moving."
    )


async def _summary_out(session: AsyncSession, tenant_id: uuid.UUID) -> CreditSummaryOut:
    summary = await credits.summarize(session, tenant_id)
    average = await credits.average_credits_per_assessment(session, tenant_id)
    estimate = credits.estimated_assessments_remaining(
        summary.balance_subunits, average
    )
    stem_active = await credits.has_active_stem_jobs(session, tenant_id)
    return CreditSummaryOut(
        balance_subunits=summary.balance_subunits,
        balance_credits=summary.balance_credits,
        subunits_per_credit=SUBUNITS_PER_CREDIT,
        granted_subunits=summary.granted_subunits,
        consumed_subunits=summary.consumed_subunits,
        rollover_subunits=summary.rollover_subunits,
        rollover_credits=credits.credits_from_subunits(summary.rollover_subunits),
        expired_subunits=summary.expired_subunits,
        expired_credits=credits.credits_from_subunits(summary.expired_subunits),
        non_expiring_subunits=summary.non_expiring_subunits,
        non_expiring_credits=credits.credits_from_subunits(
            summary.non_expiring_subunits
        ),
        expiring_soon_subunits=summary.expiring_soon_subunits,
        expiring_soon_credits=credits.credits_from_subunits(
            summary.expiring_soon_subunits
        ),
        expiring_soon_days=credits.EXPIRING_SOON_DAYS,
        next_expiry_at=summary.next_expiry_at,
        credit_validity_months=CREDIT_VALIDITY_MONTHS,
        lots=[
            CreditLotOut(
                lot_id=lot.lot_id,
                issued_at=lot.issued_at,
                expires_at=lot.expires_at,
                remaining_subunits=lot.remaining_subunits,
                remaining_credits=credits.credits_from_subunits(
                    lot.remaining_subunits
                ),
            )
            for lot in summary.lots
        ],
        usage_this_month_subunits=UsageBreakdownOut(**summary.month_by_event),
        in_deficit=summary.in_deficit,
        deficit_message=_DEFICIT_MESSAGE if summary.in_deficit else None,
        exhausted=summary.exhausted,
        low_balance=summary.low_balance,
        balance_fraction=summary.balance_fraction,
        low_balance_threshold=credits.LOW_BALANCE_FRACTION,
        warning_level=summary.warning_level,
        warning_1_threshold_credits=credits.WARNING_1_CREDITS,
        warning_2_threshold_credits=credits.WARNING_2_CREDITS,
        estimated_assessments_remaining=estimate,
        average_credits_per_assessment=float(average),
        alert_message=(
            _EXHAUSTED_MESSAGE
            if summary.exhausted
            else _warning_message(
                summary.warning_level, summary.balance_credits, estimate, stem_active
            )
        ),
        unlimited=summary.unlimited,
    )


def _refuse_public_demo_gateway(tenant: Tenant) -> None:
    if tenant.public_demo:
        raise HTTPException(
            status_code=409,
            detail="Demo workspaces do not use a payment gateway.",
        )


async def _tenant_or_404(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalars().first()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return tenant


@router.get("/overview", response_model=BillingOverviewOut)
async def billing_overview(
    user: CurrentUser = Depends(require_capability(caps.VIEW_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> BillingOverviewOut:
    await _tenant_or_404(session, user.tenant_id)
    ledger = (
        await session.execute(
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.tenant_id == user.tenant_id)
            .order_by(CreditLedgerEntry.created_at.desc())
            .limit(25)
        )
    ).scalars().all()
    transactions = (
        await session.execute(
            select(BillingTransaction)
            .where(BillingTransaction.tenant_id == user.tenant_id)
            .order_by(BillingTransaction.created_at.desc())
            .limit(25)
        )
    ).scalars().all()

    return BillingOverviewOut(
        credits=await _summary_out(session, user.tenant_id),
        razorpay_key_id=razorpay.config().key_id or None,
        recent_ledger=[
            CreditLedgerEntryOut(
                id=row.id,
                event_type=row.event_type,
                subunits_delta=row.subunits_delta,
                credits_delta=credits.credits_from_subunits(row.subunits_delta),
                created_at=row.created_at,
                job_candidate_link_id=row.job_candidate_link_id,
            )
            for row in ledger
        ],
        transactions=[TransactionOut.model_validate(row) for row in transactions],
    )


@router.get("/ledger", response_model=list[CreditLedgerEntryOut])
async def billing_ledger(
    skip: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    user: CurrentUser = Depends(require_capability(caps.VIEW_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> list[CreditLedgerEntryOut]:
    rows = (
        await session.execute(
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.tenant_id == user.tenant_id)
            .order_by(CreditLedgerEntry.created_at.desc(), CreditLedgerEntry.id)
            .offset(skip)
            .limit(limit)
        )
    ).scalars().all()
    return [
        CreditLedgerEntryOut(
            id=row.id,
            event_type=row.event_type,
            subunits_delta=row.subunits_delta,
            credits_delta=credits.credits_from_subunits(row.subunits_delta),
            created_at=row.created_at,
            job_candidate_link_id=row.job_candidate_link_id,
        )
        for row in rows
    ]


# ── Razorpay errors ──────────────────────────────────────────────────────────

def _razorpay_or_503(exc: Exception) -> HTTPException:
    if isinstance(exc, razorpay.RazorpayNotConfigured):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payments are not configured on this server yet.",
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


# ── The published catalogue (the public /pricing page) ───────────────────────

@router.get("/credit-packs", response_model=CreditPacksOut)
async def credit_pack_quotes(
    user: CurrentUser = Depends(require_capability(caps.VIEW_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> CreditPacksOut:
    """The Starter top-up quote, computed on the server before checkout."""
    tenant = await _tenant_or_404(session, user.tenant_id)
    quotes = await credit_packs.quote(session, tenant)
    return CreditPacksOut(
        packs=[CreditPackQuoteOut(**q.__dict__) for q in quotes],
        gst_rate_percent=GST_RATE_PERCENT,
    )


@router.post("/purchase", response_model=CreditPurchaseCreatedOut)
async def create_credit_purchase(
    body: CreditPurchaseIn,
    user: CurrentUser = Depends(require_capability(caps.MANAGE_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> CreditPurchaseCreatedOut:
    """Validate the purchase and mint its Razorpay Order.

    Credits are NOT granted here: a created Order is an intent to pay, and
    the grant happens on payment confirmation (§3.3 step 5). The rule
    violations (trial reuse, sub-50 amounts) are 422s with the service's own
    message, so the form can show the reason verbatim.
    """
    tenant = await _tenant_or_404(session, user.tenant_id)
    _refuse_public_demo_gateway(tenant)
    try:
        purchase = await credit_packs.create_purchase(
            session,
            tenant,
            user.user_id,
            pack_slug=body.pack_slug,
            custom_credits=body.custom_credits,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (razorpay.RazorpayError, razorpay.RazorpayNotConfigured) as exc:
        raise _razorpay_or_503(exc) from exc

    await audit(
        session, tenant_id=tenant.id, actor_user_id=user.user_id,
        action="credit_purchase_created", target_type="credit_purchase",
        target_id=purchase.id,
        metadata={
            "pack_slug": purchase.pack_slug,
            "credits": purchase.credits_purchased,
            "total_inr": purchase.total_inr,
            "razorpay_order_id": purchase.razorpay_order_id,
        },
    )
    return CreditPurchaseCreatedOut(
        purchase_id=purchase.id,
        razorpay_order_id=purchase.razorpay_order_id,
        razorpay_key_id=razorpay.config().key_id,
        total_inr=purchase.total_inr,
        credits=purchase.credits_purchased,
        bonus_credits=purchase.bonus_credits,
        subtotal_inr=purchase.subtotal_inr,
        setup_fee_inr=purchase.setup_fee_inr,
        gst_inr=purchase.gst_inr,
    )


@router.post("/demo/purchase", response_model=BillingOverviewOut)
async def demo_credit_purchase(
    body: CreditPurchaseIn,
    user: CurrentUser = Depends(require_capability(caps.MANAGE_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> BillingOverviewOut:
    """Settle a catalogue pack without calling Razorpay.

    404 while demo mode is off. 409 for any tenant that is not a public
    demo workspace. The grant is the same settle_purchase path as a paid pack.
    """
    demo_access.require_enabled()
    tenant = await _tenant_or_404(session, user.tenant_id)
    if tenant.public_demo is not True:
        raise HTTPException(
            status_code=409,
            detail="Demo workspaces do not use a payment gateway.",
        )
    if not body.pack_slug:
        raise HTTPException(status_code=422, detail="Unknown credit pack.")
    try:
        purchase = await credit_packs.simulate_demo_purchase(
            session, tenant, user.user_id, body.pack_slug
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit(
        session, tenant_id=tenant.id, actor_user_id=user.user_id,
        action="credit_purchase_demo_settled", target_type="credit_purchase",
        target_id=purchase.id,
        metadata={
            "pack_slug": purchase.pack_slug,
            "credits": purchase.credits_purchased,
            "payment": "simulated",
        },
    )
    return await billing_overview(user=user, session=session)


@router.post("/purchase/verify", response_model=BillingOverviewOut)
async def verify_credit_purchase(
    body: CreditPurchaseVerifyIn,
    user: CurrentUser = Depends(require_capability(caps.MANAGE_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> BillingOverviewOut:
    """Verify the Checkout handler payload for an Order and settle.

    The customer sees their credits the moment Checkout closes instead of
    staring at the old balance until the webhook lands. Settlement is
    idempotent, so whichever of this and the webhook runs second is a no-op.
    """
    tenant = await _tenant_or_404(session, user.tenant_id)
    _refuse_public_demo_gateway(tenant)
    if not razorpay.verify_order_signature(
        order_id=body.razorpay_order_id,
        payment_id=body.razorpay_payment_id,
        signature=body.razorpay_signature,
    ):
        raise HTTPException(status_code=400, detail="Payment could not be verified")

    purchase = (
        await session.execute(
            select(CreditPurchase).where(
                CreditPurchase.razorpay_order_id == body.razorpay_order_id
            )
        )
    ).scalars().first()
    # The tenant-scoped session's RLS already filters to the caller's rows,
    # but the ownership check is stated explicitly: the signature proves
    # Razorpay issued the payment, not that the order is the caller's.
    if purchase is None or purchase.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="No such purchase")

    settled = await credit_packs.settle_purchase(
        session, purchase, body.razorpay_payment_id
    )
    await audit(
        session, tenant_id=user.tenant_id, actor_user_id=user.user_id,
        action="credit_purchase_verified", target_type="credit_purchase",
        target_id=purchase.id,
        metadata={"settled": settled, "payment_id": body.razorpay_payment_id},
    )
    return await billing_overview(user=user, session=session)


@router.get("/purchases", response_model=list[CreditPurchaseOut])
async def list_credit_purchases(
    limit: int = Query(default=100, ge=1, le=500),
    user: CurrentUser = Depends(require_capability(caps.VIEW_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> list[CreditPurchaseOut]:
    """The tenant's purchase history, newest first — §7.4's transaction view,
    and where the §7.3 invoice download links come from. Bounded like every
    other list route: the newest hundred purchases cover years of buying at
    any plausible cadence, and the ceiling keeps the page alive past that."""
    rows = (
        await session.execute(
            select(CreditPurchase)
            .where(CreditPurchase.tenant_id == user.tenant_id)
            .order_by(CreditPurchase.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [CreditPurchaseOut.model_validate(row) for row in rows]


@router.get("/purchases/{purchase_id}/invoice")
async def download_credit_invoice(
    purchase_id: uuid.UUID,
    user: CurrentUser = Depends(require_capability(caps.VIEW_BILLING)),
    session: AsyncSession = Depends(get_tenant_db),
) -> Response:
    """The GST invoice PDF, downloadable at any time (§7.3).

    404 for anything that is not the caller's own PAID purchase: an unpaid
    purchase has no invoice (§9: no invoice on a failed payment), and a
    non-existent one and another tenant's must be indistinguishable.
    """
    purchase = (
        await session.execute(
            select(CreditPurchase).where(CreditPurchase.id == purchase_id)
        )
    ).scalars().first()
    if (
        purchase is None
        or purchase.tenant_id != user.tenant_id
        or purchase.status != PURCHASE_PAID
    ):
        raise HTTPException(status_code=404, detail="No invoice for this purchase")
    tenant = await _tenant_or_404(session, user.tenant_id)
    pdf = credit_packs.render_invoice_pdf(purchase, tenant)
    filename = f"{purchase.invoice_number or purchase.id}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Razorpay webhook ─────────────────────────────────────────────────────────

#: The Orders events a credit purchase needs, and nothing else. Every other
#: event Razorpay may deliver is recorded (dedupe row) and answered "ignored".
_HANDLED_EVENTS = {
    "payment.captured",
    "order.paid",
    "payment.failed",
    "subscription.charged",
    "subscription.cancelled",
    "subscription.halted",
}


@router.post("/webhook/razorpay", status_code=status.HTTP_200_OK)
async def razorpay_webhook(
    request: Request, session: AsyncSession = Depends(get_public_db)
) -> dict:
    """Signature-verified credit-purchase payment events.

    Answers 200 for anything it has authenticated and HANDLED, including
    events it deliberately does not act on and redeliveries it has already
    recorded: a non-2xx makes Razorpay retry, and retrying an event we have
    deliberately ignored just fills the retry queue forever. A failure to
    RECORD the event is the opposite case and answers 5xx, because that retry
    is the only thing that gets a paid purchase settled.
    """
    raw = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    # THE SIGNATURE IS CHECKED UNCONDITIONALLY. THERE IS NO DEVELOPMENT BYPASS.
    #
    # There used to be one, and it was open on the live site. It read
    # `if settings.is_production or razorpay.config().webhook_secret: raise`,
    # and fell through to PROCESS the event otherwise. Both halves were false in
    # the deployment serving readypick.ai:
    #
    #   * `is_production` is `environment == "production"` and the live
    #     environment sets `ENVIRONMENT=pilot`, so it is False in production;
    #   * `RAZORPAY_WEBHOOK_SECRET` was never mounted on the api service, so
    #     `webhook_secret` was empty.
    #
    # So an anonymous forged payment event was accepted and granted credits,
    # repeatably, because the attacker mints the idempotency
    # keys too. Unauthenticated, remote, and it issues the thing this product
    # sells.
    #
    # AN ABSENT SECRET NOW REFUSES, and that is deliberately the OPPOSITE of the
    # inbound-email relay, where an unset secret leaves the route open. The two
    # differ because the cost of being wrong differs. Refusing an employer's
    # reply loses a message the product can ask for again; accepting an unsigned
    # payment event grants money and cannot be taken back. A signature check
    # that cannot be performed has failed, not passed.
    #
    # 503 rather than 400 when the secret is missing: that is our
    # misconfiguration, not the caller's bad request, and Razorpay retries a
    # 5xx, so a genuine event survives the window while the secret is wired.
    if not razorpay.config().webhook_secret:
        log.error(
            "billing.webhook_secret_missing, refusing every webhook. "
            "RAZORPAY_WEBHOOK_SECRET must be configured for this environment."
        )
        raise HTTPException(
            status_code=503, detail="Webhook verification is not configured"
        )
    if not razorpay.verify_webhook_signature(raw_body=raw, signature=signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed webhook payload") from exc

    event_type = str(body.get("event") or "")
    # Razorpay's delivery id header is the stable per-delivery identity; the
    # payload carries no event id of its own.
    event_id = request.headers.get("X-Razorpay-Event-Id") or f"{event_type}:{body.get('created_at')}"

    # Dedupe FIRST. Razorpay delivers at least once, and a replayed event must
    # do nothing the first delivery has not already done.
    #
    # ON CONFLICT ON THE ONE CONSTRAINT, NEVER A CAUGHT EXCEPTION. This used to
    # be `except Exception` around the flush, answered 200 "duplicate". So a
    # DataError (an event type wider than its column), a lost connection or
    # any other failure of this INSERT told Razorpay the event was handled,
    # Razorpay never retried it, and a paid purchase was settled by nothing,
    # silently. Stating the no-op in SQL means the database absorbs
    # the duplicate and nothing else; every other failure propagates, the
    # request answers 5xx, and Razorpay retries, which is exactly what a retry
    # queue is for. The same shape `conversations` uses for its participants.
    inserted = (
        await session.execute(
            pg_insert(WebhookEvent)
            .values(
                id=uuid.uuid4(),
                provider="razorpay",
                event_id=event_id,
                event_type=event_type,
                payload_json=body,
            )
            .on_conflict_do_nothing(constraint="uq_webhook_events_provider_id")
            .returning(WebhookEvent.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        return {"status": "duplicate"}

    if event_type not in _HANDLED_EVENTS:
        return {"status": "ignored"}

    payload = body.get("payload") or {}
    payment_entity = ((payload.get("payment") or {}).get("entity")) or {}
    order_entity = ((payload.get("order") or {}).get("entity")) or {}
    subscription_entity = ((payload.get("subscription") or {}).get("entity")) or {}

    if event_type.startswith("subscription."):
        subscription_id = subscription_entity.get("id")
        tenant = (
            await session.execute(
                select(Tenant).where(Tenant.razorpay_subscription_id == subscription_id)
            )
        ).scalars().first() if subscription_id else None
        if tenant is None:
            log.warning("billing.subscription_unmatched id=%s", subscription_id or "")
            if event_type == "subscription.charged":
                raise HTTPException(status_code=503, detail="Subscription is not recorded yet")
            return {"status": "unmatched"}
        if event_type == "subscription.charged":
            if (payment_entity.get("status") != "captured"
                    or payment_entity.get("currency") != "INR"
                    or not payment_entity.get("id")
                    or int(payment_entity.get("amount") or 0) % razorpay.PAISE_PER_RUPEE):
                raise HTTPException(status_code=503, detail="Charge is not captured")
            await monthly_plans.settle_charge(
                session, tenant=tenant, subscription_id=subscription_id,
                payment_id=payment_entity["id"],
                amount_inr=int(payment_entity["amount"]) // razorpay.PAISE_PER_RUPEE,
                current_end=subscription_entity.get("current_end"),
                gateway_plan_id=subscription_entity.get("plan_id"),
            )
        elif event_type == "subscription.cancelled":
            tenant.subscription_status = "cancelled"
        elif event_type == "subscription.halted":
            tenant.subscription_status = "halted"
        await session.execute(
            text("UPDATE webhook_events SET processed_at = now() "
                 "WHERE provider = 'razorpay' AND event_id = :eid"),
            {"eid": event_id},
        )
        return {"status": "ok"}

    # ── Credit-pack purchases first (Master Directive Part 5 §3.3 step 5) ────
    # A payment.captured / order.paid / payment.failed whose order id matches
    # a credit_purchases row belongs to the pack flow, whatever else the event
    # carries. Resolved by order id, not by tenant: the WebhookEvent dedupe
    # above plus settle_purchase's own status-flip idempotency give the §9
    # duplicate-webhook guarantee twice over.
    order_id = payment_entity.get("order_id") or order_entity.get("id")
    if order_id:
        purchase = (
            await session.execute(
                select(CreditPurchase).where(
                    CreditPurchase.razorpay_order_id == order_id
                )
            )
        ).scalars().first()
        if purchase is not None:
            if event_type in {"payment.captured", "order.paid"}:
                await credit_packs.settle_purchase(
                    session, purchase, payment_entity.get("id")
                )
            elif event_type == "payment.failed":
                # §9: no credits, no invoice. Marked failed only while still
                # `created` — a purchase the settle path already won stays
                # paid, and the client retries with a NEW purchase.
                await session.execute(
                    text(
                        "UPDATE credit_purchases SET status = :failed "
                        "WHERE id = :id AND status = :created"
                    ),
                    {
                        "failed": PURCHASE_FAILED,
                        "created": PURCHASE_CREATED,
                        "id": str(purchase.id),
                    },
                )
            await session.execute(
                text(
                    "UPDATE webhook_events SET processed_at = now() "
                    "WHERE provider = 'razorpay' AND event_id = :eid"
                ),
                {"eid": event_id},
            )
            return {"status": "ok"}
    # A payment event naming no credit purchase of ours: an order created
    # outside this product, or one whose purchase row was never written.
    # Recorded (the dedupe row above) and answered 200, because a retry would
    # find the same nothing; logged with identifiers only so it can be chased.
    log.warning(
        "billing.webhook_unmatched event=%s order=%s", event_type, order_id or ""
    )
    return {"status": "unmatched"}


# ── Provider Portal: billing overview across customers ───────────────────────

@router.get("/provider/overview", response_model=list[ProviderBillingRowOut])
async def provider_billing_overview(
    skip: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    session: AsyncSession = Depends(get_superadmin_db),
) -> list[ProviderBillingRowOut]:
    """Every customer's credit balance.

    One query with a LEFT JOIN and a grouped ledger sum, not a per-customer
    balance lookup: 30 customers must not become 31 round trips.
    """
    balances = (
        select(
            CreditLedgerEntry.tenant_id.label("tenant_id"),
            func.coalesce(func.sum(CreditLedgerEntry.subunits_delta), 0).label("balance"),
        )
        .group_by(CreditLedgerEntry.tenant_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                Tenant.id,
                Tenant.name,
                func.coalesce(balances.c.balance, 0),
            )
            .select_from(Tenant)
            .outerjoin(balances, balances.c.tenant_id == Tenant.id)
            .order_by(Tenant.name)
            .offset(skip)
            .limit(limit)
        )
    ).all()
    return [
        ProviderBillingRowOut(
            tenant_id=tenant_id,
            customer_name=name,
            balance_subunits=int(balance),
            balance_credits=credits.credits_from_subunits(int(balance)),
            in_deficit=int(balance) < 0,
        )
        for tenant_id, name, balance in rows
    ]
