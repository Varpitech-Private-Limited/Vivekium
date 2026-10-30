"""Starter-only top-up quoting, settlement and GST invoices.

New Starter purchases grant 75 completed assessments for ₹24,000 plus GST,
with no setup fee. Historical purchase rows retain their original invoice
amounts and validity terms. Settlement remains idempotent across Checkout
verification and webhook delivery.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import (
    CREDIT_PACK_LABELS,
    CREDIT_PACKS,
    CREDIT_VALIDITY_MONTHS,
    GST_RATE_PERCENT,
    PRICE_PER_CREDIT_INR,
    PURCHASE_CREATED,
    PURCHASE_PAID,
    STARTER_PACK_SLUG,
    STARTER_PACK_PRICE_INR,
    SUBUNITS_PER_CREDIT,
    BillingTransaction,
    CreditPurchase,
)
from app.models.tenant import Tenant
from app.services import credits, razorpay


def gst_inr(taxable_inr: int) -> int:
    """18% of the taxable amount, rounded half-up to whole rupees.

    The rounding also supports a future published bundle with a different price.
    """
    return (taxable_inr * GST_RATE_PERCENT + 50) // 100


@dataclass(frozen=True)
class PackQuote:
    """One line of the §7.2 purchase page: a pack priced for THIS tenant."""

    slug: str
    #: The customer-facing name. Resolved here rather than in the client so an
    #: invoice, an email and the billing page cannot call one pack three
    #: things.
    label: str
    credits: int
    bonus_credits: int
    #: What the customer receives as completed assessments.
    credits_total: int
    subtotal_inr: int
    setup_fee_inr: int
    setup_fee_waived: bool
    gst_inr: int
    total_inr: int
    available: bool
    trial: bool
    #: Months of validity on the credits this pack grants. Never None today:
    #: every NEW grant expires (change request 25). It is serialised so the
    #: purchase page states the term before the customer pays rather than
    #: after, on the invoice.
    validity_months: int


def _price(credit_count: int, setup_fee: int) -> tuple[int, int, int]:
    """Price the only currently offered top-up."""
    if credit_count != CREDIT_PACKS[STARTER_PACK_SLUG][0]:
        raise ValueError("Only the Starter top-up is available.")
    subtotal = STARTER_PACK_PRICE_INR
    tax = gst_inr(subtotal + setup_fee)
    return subtotal, tax, subtotal + tax + setup_fee


async def setup_fee_for(session: AsyncSession, tenant: Tenant) -> tuple[int, bool]:
    """The monthly pricing model has no account setup fee."""
    return 0, False


async def quote(session: AsyncSession, tenant: Tenant) -> list[PackQuote]:
    """Price the Starter top-up for this tenant."""
    setup_fee, waived = await setup_fee_for(session, tenant)
    quotes: list[PackQuote] = []
    for slug, (credit_count, bonus) in CREDIT_PACKS.items():
        subtotal, tax, total = _price(credit_count, setup_fee)
        quotes.append(
            PackQuote(
                slug=slug,
                label=CREDIT_PACK_LABELS[slug],
                credits=credit_count,
                bonus_credits=bonus,
                credits_total=credit_count + bonus,
                subtotal_inr=subtotal,
                setup_fee_inr=setup_fee,
                setup_fee_waived=waived,
                gst_inr=tax,
                total_inr=total,
                available=True,
                trial=False,
                validity_months=CREDIT_VALIDITY_MONTHS,
            )
        )
    return quotes


async def create_purchase(
    session: AsyncSession,
    tenant: Tenant,
    user_id: uuid.UUID | None,
    *,
    pack_slug: str | None = None,
    custom_credits: int | None = None,
) -> CreditPurchase:
    """Validate the purchase, create its Razorpay Order, store the row.

    Raises ValueError with a client-worthy message on any rule violation —
    the API maps it to a 422. The row is written with status `created` and
    grants NOTHING: credits arrive only through `settle_purchase`, on payment
    confirmation (§3.3 step 5, and the §9 payment-failed row).
    """
    if custom_credits is not None or pack_slug is None:
        raise ValueError("Only the Starter top-up is available.")

    if pack_slug is not None:
        if pack_slug not in CREDIT_PACKS:
            raise ValueError("Unknown credit pack.")
        credit_count, bonus = CREDIT_PACKS[pack_slug]
        slug = pack_slug

    setup_fee, waived = await setup_fee_for(session, tenant)
    subtotal, tax, total = _price(credit_count, setup_fee)

    purchase = CreditPurchase(
        # Assigned eagerly (the mixin's default only fires at flush) because
        # the id doubles as the Razorpay receipt below, before any flush.
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        created_by=user_id,
        pack_slug=slug,
        credits_purchased=credit_count,
        bonus_credits=bonus,
        subtotal_inr=subtotal,
        setup_fee_inr=setup_fee,
        setup_fee_waived=waived,
        gst_inr=tax,
        total_inr=total,
        status=PURCHASE_CREATED,
        credit_validity_months=CREDIT_VALIDITY_MONTHS,
    )
    # The receipt is our purchase id, so the Razorpay dashboard and our table
    # cross-reference by inspection. The order is created BEFORE the row is
    # flushed so a gateway failure leaves nothing behind to reconcile.
    order = await razorpay.create_order(
        amount_paise=total * razorpay.PAISE_PER_RUPEE,
        receipt=str(purchase.id),
        notes={"tenant_id": str(tenant.id), "pack_slug": slug},
    )
    purchase.razorpay_order_id = order["id"]
    session.add(purchase)
    await session.flush()
    return purchase


async def simulate_demo_purchase(
    session: AsyncSession,
    tenant: Tenant,
    created_by: uuid.UUID | None,
    pack_slug: str,
) -> CreditPurchase:
    """Insert a Starter purchase and settle it without calling Razorpay.

    The order id is assigned after the first flush so it can carry the row
    id, and the grant still happens only inside settle_purchase.
    """
    if tenant.public_demo is not True:
        raise ValueError("Demo workspaces do not use a payment gateway.")
    if pack_slug not in CREDIT_PACKS:
        raise ValueError("Unknown credit pack.")
    credit_count, bonus = CREDIT_PACKS[pack_slug]
    setup_fee, waived = await setup_fee_for(session, tenant)
    subtotal, tax, total = _price(credit_count, setup_fee)
    purchase = CreditPurchase(
        tenant_id=tenant.id,
        created_by=created_by,
        pack_slug=pack_slug,
        credits_purchased=credit_count,
        bonus_credits=bonus,
        subtotal_inr=subtotal,
        setup_fee_inr=setup_fee,
        setup_fee_waived=waived,
        gst_inr=tax,
        total_inr=total,
        status=PURCHASE_CREATED,
        credit_validity_months=CREDIT_VALIDITY_MONTHS,
    )
    session.add(purchase)
    await session.flush()
    purchase.razorpay_order_id = f"demo_{purchase.id}"
    await session.flush()
    settled = await settle_purchase(
        session, purchase, payment_id=f"demo-pay-{purchase.id}"
    )
    if settled is not True:
        raise RuntimeError("Demo purchase did not settle.")
    return purchase


async def settle_purchase(
    session: AsyncSession, purchase: CreditPurchase, payment_id: str | None
) -> bool:
    """Grant the credits and finalise the invoice. Idempotent; returns True
    only for the ONE call that settles.

    The browser's verify call and the webhook both land here — routinely for
    the same payment, and the webhook possibly more than once (§9 duplicate
    row). The winner is decided by the database: the status flip happens in
    the same UPDATE that checks it, so every other caller sees zero rows and
    returns False having written nothing.
    """
    won = (
        await session.execute(
            text(
                "UPDATE credit_purchases SET status = :paid "
                "WHERE id = :id AND status = :created RETURNING id"
            ),
            {
                "paid": PURCHASE_PAID,
                "created": PURCHASE_CREATED,
                "id": str(purchase.id),
            },
        )
    ).first()
    if won is None:
        return False

    # Belt and braces: the grant's idempotency key is derived from the order
    # id, so even a settle that somehow won twice could not double-grant.
    await credits.grant(
        session,
        tenant_id=purchase.tenant_id,
        subunits=(purchase.credits_purchased + purchase.bonus_credits)
        * SUBUNITS_PER_CREDIT,
        idempotency_key=f"credit-pack:{purchase.razorpay_order_id}",
        metadata={
            "pack_slug": purchase.pack_slug,
            "credits_purchased": purchase.credits_purchased,
            "bonus_credits": purchase.bonus_credits,
            "razorpay_order_id": purchase.razorpay_order_id,
        },
    )

    # Rule 1 + Rule 6 on the tenant, in one statement. trial_used goes TRUE on
    # any first settled purchase (trial or not: either way the trial window is
    # over). setup_fee_paid goes TRUE unconditionally — charged on this
    # invoice, waived on this invoice, or already TRUE from an earlier one.
    # Raw SQL because `tenants` is global and this runs from tenant-scoped and
    # webhook (public) sessions alike.
    await session.execute(
        text(
            "UPDATE tenants SET trial_used = TRUE, setup_fee_paid = TRUE, "
            "setup_fee_waived = setup_fee_waived OR :waived WHERE id = :tid"
        ),
        {"waived": purchase.setup_fee_waived, "tid": str(purchase.tenant_id)},
    )

    # Sequential GST invoice number (§7.3). nextval() is race-free under
    # concurrent settlements; the year makes the series human-auditable
    # against a filing period.
    now = datetime.now(timezone.utc)
    seq = (
        await session.execute(text("SELECT nextval('credit_invoice_seq')"))
    ).scalar_one()
    purchase.status = PURCHASE_PAID
    purchase.razorpay_payment_id = payment_id
    purchase.invoice_number = f"RP-{now.year}-{int(seq):06d}"
    purchase.paid_at = now

    session.add(
        BillingTransaction(
            tenant_id=purchase.tenant_id,
            razorpay_payment_id=payment_id,
            amount_inr=purchase.total_inr,
            status="success",
            transaction_type="credit_pack",
            notes=f"Credit pack {purchase.pack_slug}: "
            f"{purchase.credits_purchased} + {purchase.bonus_credits} bonus",
        )
    )
    await session.flush()

    # Both after the COMMIT (CLAUDE.md rule 4). The invoice email renders the
    # PDF from this purchase row and the release re-checks this grant's
    # balance, so a task started before the commit could read neither; and a
    # settlement whose transaction rolls back must announce nothing. They used
    # to be plain dispatches inside a broad `except`, which both fired before
    # the rows existed and hid a programming error as a log line.
    #
    # A lost invoke after the commit is logged by the dispatcher and never
    # fails the settlement. The held-report release is repaired by the hourly
    # `pickready.release_held_assessments` sweep (`workers/schedule.py`); the
    # invoice stays downloadable from the billing page whatever happens to
    # the email.
    from app.workers.dispatch import dispatch_after_commit

    dispatch_after_commit(
        session, "pickready.send_credit_invoice_email", args=[str(purchase.id)]
    )
    # A top-up releases whatever finalisation was held for want of credits.
    dispatch_after_commit(
        session,
        "pickready.release_held_assessments",
        args=[str(purchase.tenant_id)],
    )
    return True


# ── GST invoice PDF (§5.2 / §7.3) ────────────────────────────────────────────

#: Vivekium brand navy for the invoice header (Part 1's palette).
_NAVY = (0.06, 0.13, 0.28)


def _inr(amount: int) -> str:
    """Whole rupees with thousands separators. "Rs." rather than the rupee
    sign because the PDF's built-in Helvetica has no glyph for it, and a
    missing-glyph box on a tax document is worse than the abbreviation."""
    return f"Rs. {amount:,}"


def render_invoice_pdf(purchase: CreditPurchase, tenant: Tenant) -> bytes:
    """One-page GST-compliant invoice for a settled purchase (§5.2, §7.3).

    Everything printed comes from the STORED purchase row — nothing is
    recomputed here, so the PDF a customer downloads in two years matches the
    money that actually moved, whatever the constants say by then.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as pdf_canvas

    from app.core.config import get_settings

    settings = get_settings()
    buffer = BytesIO()
    page = pdf_canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    # Header band: brand navy, white wordmark.
    page.setFillColorRGB(*_NAVY)
    page.rect(0, height - 90, width, 90, stroke=0, fill=1)
    page.setFillColorRGB(1, 1, 1)
    page.setFont("Helvetica-Bold", 22)
    page.drawString(40, height - 55, "Vivekium")
    page.setFont("Helvetica", 11)
    page.drawRightString(width - 40, height - 55, "TAX INVOICE")

    y = height - 130
    page.setFillColorRGB(0, 0, 0)
    page.setFont("Helvetica-Bold", 11)
    page.drawString(40, y, f"Invoice No: {purchase.invoice_number or ''}")
    invoice_date = purchase.paid_at or purchase.created_at
    page.drawRightString(
        width - 40, y, f"Date: {invoice_date:%d %b %Y}" if invoice_date else "Date:"
    )
    y -= 18
    page.setFont("Helvetica", 10)
    if settings.readypick_gstin:
        page.drawString(40, y, f"Vivekium GSTIN: {settings.readypick_gstin}")
        y -= 14
    page.drawString(40, y, f"Billed to: {tenant.name}")
    y -= 14
    if tenant.gstin:
        page.drawString(40, y, f"Client GSTIN: {tenant.gstin}")
        y -= 14

    # Line items. Column layout: description, qty x rate, amount.
    y -= 20
    page.setFont("Helvetica-Bold", 10)
    page.drawString(40, y, "Description")
    page.drawRightString(width - 40, y, "Amount")
    y -= 6
    page.setLineWidth(0.5)
    page.line(40, y, width - 40, y)
    y -= 18

    page.setFont("Helvetica", 10)

    def line(label: str, amount_text: str) -> None:
        nonlocal y
        page.drawString(40, y, label)
        page.drawRightString(width - 40, y, amount_text)
        y -= 16

    if purchase.pack_slug == STARTER_PACK_SLUG and purchase.credits_purchased == 75:
        line("Starter top-up: 75 completed assessments", _inr(purchase.subtotal_inr))
    else:
        # Preserve the description of historical invoices as sold.
        line(
            f"Vivekium Intelligence Report Credits "
            f"({purchase.credits_purchased} x {_inr(PRICE_PER_CREDIT_INR)})",
            _inr(purchase.subtotal_inr),
        )
    if purchase.bonus_credits:
        # Rule 3: the bonus is a gift, never a discount — it appears at Rs. 0
        # and the purchased credits above stay at full price.
        line(
            f"Bonus Credits ({purchase.bonus_credits} credits, free)",
            _inr(0),
        )
    if purchase.setup_fee_inr:
        line("Account Setup Fee (one-time)", _inr(purchase.setup_fee_inr))
    elif purchase.setup_fee_waived:
        line("Account Setup Fee (waived, early client)", _inr(0))

    y -= 4
    page.line(300, y, width - 40, y)
    y -= 18
    line("Subtotal", _inr(purchase.subtotal_inr + purchase.setup_fee_inr))
    line(f"GST @ {GST_RATE_PERCENT}%", _inr(purchase.gst_inr))
    page.setFont("Helvetica-Bold", 11)
    line("Grand Total", _inr(purchase.total_inr))

    y -= 24
    page.setFont("Helvetica", 8)
    page.setFillColorRGB(0.35, 0.35, 0.35)
    # Read from the ROW, never from the constant. An invoice issued before
    # change request 25 carries NULL here and keeps saying what it said when
    # it was issued; printing today's three-month term over a purchase sold as
    # never-expiring would be a tax document contradicting itself.
    validity = purchase.credit_validity_months
    validity_sentence = (
        "Credits never expire."
        if validity is None
        else f"Credits are valid for {validity} months from the date of this invoice."
    )
    page.drawString(
        40,
        y,
        f"{validity_sentence} GST collected is remitted to the government. "
        "This is a system-generated invoice.",
    )
    page.showPage()
    page.save()
    return buffer.getvalue()
