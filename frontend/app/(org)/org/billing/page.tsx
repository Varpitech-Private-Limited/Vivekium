"use client";

// Customer Portal billing for monthly plans and Starter top-ups.
//
// Four things this page has to get right, because each of them is a question a
// customer will otherwise ask by email:
//
//   * how many credits do I have, in CREDITS, not in the sub-units the ledger
//     stores (60 sub-units is one credit, and nobody should have to know that);
//   * where did this month's usage go, broken down by what caused it;
//   * why have my assessment invitations stopped, and what do I do about it;
//   * how do I buy more, and where are my GST invoices.
//
// Reading is gated on `view_billing`, which the three staff roles hold, so a
// recruiter can answer the third question for themselves. Buying credits
// needs `manage_billing`, which the Company Admin holds alone. There is no
// plan changes and cancellation use the manage_billing capability.

import * as React from "react";
import { AlertTriangle, Download } from "lucide-react";

import { API_BASE, ApiError, apiGet, apiPost } from "@/lib/api";
import { demoModeEnabled } from "@/lib/demo-mode";
import { useAuth } from "@/lib/auth-context";
import { CAP } from "@/lib/permissions";
import { usePermissions } from "@/lib/use-permissions";
import { openOrderCheckout, openSubscriptionCheckout } from "@/lib/razorpay";
import type {
  BillingOverview,
  CreditPack,
  CreditPacksResponse,
  CreditPurchaseRow,
  PurchaseCreateResponse,
} from "@/lib/types";
import { PageHeader } from "@/components/app-shell";
import {
  CreditStatement,
} from "@/components/billing/credit-statement";
import { CreditPackPicker, formatInr } from "@/components/billing/credit-pack-picker";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DetailItem,
  ErrorState,
  LoadingRows,
  Section,
} from "@/components/page-primitives";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useToast } from "@/components/ui/toast";


type MonthlyPlan = {
  slug: string; name: string; assessments: number; price_inr: number;
  gst_inr: number; total_inr: number; rollover_months: number;
};
type CurrentPlan = {
  plan_slug: string | null; pending_plan_slug: string | null;
  status: string | null; current_end: string | null;
};
type MonthlyCharge = {
  id: string; plan_slug: string; assessments_granted: number;
  subtotal_inr: number; gst_inr: number; amount_inr: number;
  invoice_number: string; created_at: string;
};

/** Plain-text purchase statuses (directive Part 5 section 7.3): typography,
 *  not colored pills, tells the reader where a purchase stands. */
const PURCHASE_STATUS_LABELS: Record<string, string> = {
  created: "Payment pending",
  pending: "Payment pending",
  paid: "Paid",
  failed: "Failed",
  refunded: "Refunded",
};

/**
 * Same-origin proxied invoice path (directive Part 5 section 7.3). A plain
 * <a download>, not an apiGet: the browser streams the PDF itself, carrying
 * its cookies through the same /api proxy every other call uses.
 */
function invoiceHref(purchaseId: string): string {
  return `${API_BASE}/billing/purchases/${purchaseId}/invoice`;
}

function formatDate(value: string | null): string {
  if (!value) return "Not scheduled";
  return new Date(value).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/**
 * Sub-units to a credit figure for DISPLAY.
 *
 * The server already sends the rounded string for the balance; this is for the
 * per-event usage numbers, which arrive as raw sub-units so the page can total
 * them without re-parsing decimals.
 */
function toCredits(subunits: number, perCredit: number): string {
  return (subunits / perCredit).toFixed(2);
}

export default function BillingPage() {
  const { user } = useAuth();
  const { can } = usePermissions();
  const { toast } = useToast();
  const [data, setData] = React.useState<BillingOverview | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [forbidden, setForbidden] = React.useState(false);

  // Credit pack purchase state (directive Part 5 sections 3 and 7.2). The two
  // pack calls fail SOFT: a backend that predates the credit-pack endpoints
  // must not blank the whole billing page, only its own section.
  const [packs, setPacks] = React.useState<CreditPacksResponse | null>(null);
  const [packsError, setPacksError] = React.useState<string | null>(null);
  const [purchases, setPurchases] = React.useState<CreditPurchaseRow[] | null>(
    null
  );
  const [selectedSlug, setSelectedSlug] = React.useState<string | null>(null);
  const [payBusy, setPayBusy] = React.useState(false);
  const [monthlyPlans, setMonthlyPlans] = React.useState<MonthlyPlan[] | null>(null);
  const [currentPlan, setCurrentPlan] = React.useState<CurrentPlan | null>(null);
  const [monthlyCharges, setMonthlyCharges] = React.useState<MonthlyCharge[] | null>(null);
  const [planBusy, setPlanBusy] = React.useState(false);

  const canManage = can(CAP.manageBilling);

  const load = React.useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setData(await apiGet<BillingOverview>("/billing/overview"));
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) {
        setForbidden(true);
      } else {
        setLoadError(
          error instanceof Error ? error.message : "Could not load billing."
        );
      }
    } finally {
      setLoading(false);
    }
  }, []);

  const loadPacks = React.useCallback(async () => {
    setPacksError(null);
    try {
      setPacks(await apiGet<CreditPacksResponse>("/billing/credit-packs"));
    } catch (error) {
      setPacks(null);
      setPacksError(
        error instanceof Error ? error.message : "Could not load credit packs."
      );
    }
  }, []);

  const loadPurchases = React.useCallback(async () => {
    try {
      setPurchases(await apiGet<CreditPurchaseRow[]>("/billing/purchases"));
    } catch {
      // No history section rather than a second error banner: the overview and
      // packs errors already say everything actionable.
      setPurchases(null);
    }
  }, []);

  const loadPlans = React.useCallback(async () => {
    const [catalogue, current, charges] = await Promise.all([
      apiGet<{ plans: MonthlyPlan[] }>("/billing/public/plans"),
      apiGet<CurrentPlan>("/billing/monthly/current"),
      apiGet<MonthlyCharge[]>("/billing/monthly/charges"),
    ]);
    setMonthlyPlans(catalogue.plans);
    setCurrentPlan(current);
    setMonthlyCharges(charges);
  }, []);

  React.useEffect(() => {
    void load();
    void loadPacks();
    void loadPurchases();
    void loadPlans().catch((error) => {
      setLoadError(error instanceof Error ? error.message : "Could not load monthly billing.");
    });
  }, [load, loadPacks, loadPurchases, loadPlans]);

  const selectPlan = React.useCallback(async (plan: MonthlyPlan) => {
    setPlanBusy(true);
    try {
      if (currentPlan?.status === "active") {
        await apiPost("/billing/monthly/change-plan", { plan_slug: plan.slug });
        toast({ title: "Plan change scheduled", description: `${plan.name} begins with your next monthly charge.` });
        await loadPlans();
        return;
      }
      const checkout = await apiPost<{ subscription_id: string; razorpay_key_id: string }>(
        "/billing/monthly/subscribe", { plan_slug: plan.slug }
      );
      const opened = await openSubscriptionCheckout({
        keyId: checkout.razorpay_key_id,
        subscriptionId: checkout.subscription_id,
        name: "Vivekium", description: `${plan.name} monthly plan`,
        prefill: { email: user?.email ?? undefined, name: user?.full_name ?? undefined },
        onSuccess: async (proof) => {
          try {
            const confirmation = await apiPost<{ granted: boolean; status: string }>("/billing/monthly/verify", proof);
            await Promise.all([load(), loadPlans()]);
            toast(confirmation.status === "active"
              ? { title: "Plan active", description: `${plan.assessments} completed assessments added to your pool.` }
              : { title: "Payment authorization received", description: "Your plan starts after the first full monthly charge. Refresh billing to check its status." });
          } catch (error) {
            toast({ variant: "destructive", title: "Payment could not be confirmed", description: error instanceof Error ? error.message : "Refresh billing to check its status." });
          } finally { setPlanBusy(false); }
        },
        onDismiss: () => setPlanBusy(false),
      });
      if (!opened) throw new Error("Checkout could not open. Try again.");
    } catch (error) {
      toast({ variant: "destructive", title: "Plan change failed", description: error instanceof Error ? error.message : "Try again." });
    } finally { if (currentPlan?.status === "active") setPlanBusy(false); }
  }, [currentPlan?.status, load, loadPlans, toast, user]);

  const cancelPlan = React.useCallback(async () => {
    setPlanBusy(true);
    try {
      await apiPost("/billing/monthly/cancel", {});
      await loadPlans();
      toast({ title: "Cancellation scheduled", description: "Your plan ends after the paid billing period. Existing credits retain their expiry dates." });
    } catch (error) {
      toast({ variant: "destructive", title: "Could not cancel", description: error instanceof Error ? error.message : "Try again." });
    } finally { setPlanBusy(false); }
  }, [loadPlans, toast]);

  /**
   * The credit pack purchase flow (directive Part 5 section 3.3): create the
   * order server-side, collect payment through Razorpay Checkout, then let the
   * SERVER verify the signature. Success is only ever claimed after verify
   * returns OK; the handler firing proves nothing by itself.
   */
  const buyPack = React.useCallback(
    async (pack: CreditPack) => {
      setPayBusy(true);
      try {
        if (demoModeEnabled()) {
          const overview = await apiPost<BillingOverview>(
            "/billing/demo/purchase",
            { pack_slug: pack.slug }
          );
          setData(overview);
          setSelectedSlug(null);
          toast({
            title: "Credits added",
            description: `Payment confirmed. Your new balance is ${overview.credits.balance_credits} credits. Your GST invoice is under Purchases and invoices below.`,
          });
          setPayBusy(false);
          await Promise.all([load(), loadPacks(), loadPurchases()]);
          return;
        }
        const order = await apiPost<PurchaseCreateResponse>(
          "/billing/purchase",
          { pack_slug: pack.slug }
        );
        const opened = await openOrderCheckout({
          keyId: order.razorpay_key_id,
          orderId: order.razorpay_order_id,
          amountInr: order.total_inr,
          name: "Vivekium",
          description: `${order.credits} Intelligence Report credits, one-time purchase`,
          prefill: {
            email: user?.email ?? undefined,
            name: user?.full_name ?? undefined,
          },
          onSuccess: async (payload) => {
            try {
              const overview = await apiPost<BillingOverview>(
                "/billing/purchase/verify",
                payload
              );
              setData(overview);
              setSelectedSlug(null);
              toast({
                title: "Credits added",
                description: `Payment confirmed. Your new balance is ${overview.credits.balance_credits} credits. Your GST invoice is under Purchases and invoices below.`,
              });
            } catch (error) {
              toast({
                variant: "destructive",
                title: "We could not confirm that payment",
                description:
                  error instanceof Error
                    ? error.message
                    : "Reload this page in a moment to check its status.",
              });
            }
            setPayBusy(false);
            // Refresh everything the purchase can have changed: balance,
            // trial availability and the invoice list.
            await Promise.all([load(), loadPacks(), loadPurchases()]);
          },
          onDismiss: () => setPayBusy(false),
        });
        if (!opened) {
          setPayBusy(false);
          toast({
            variant: "destructive",
            title: "Checkout could not open",
            description:
              "Check that your browser is not blocking payment scripts, then retry.",
          });
        }
      } catch (error) {
        setPayBusy(false);
        toast({
          variant: "destructive",
          title: "That did not go through",
          description:
            error instanceof Error ? error.message : "Try again in a moment.",
        });
      }
    },
    [load, loadPacks, loadPurchases, toast, user]
  );


  if (forbidden) {
    return (
      <>
        <PageHeader
          title="Billing"
          description="Your credit pool, what used it, and your purchases."
        />
        <ErrorState
          title="Billing is not part of your access"
          description="Ask your Company Admin to open this page, or to grant you billing visibility."
        />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Billing"
        description="Your credit pool, what used it, and your purchases."
      />

      {loading ? (
        <LoadingRows rows={5} label="Loading billing" />
      ) : loadError ? (
        <ErrorState
          title="Could not load billing"
          description={loadError}
          action={
            <Button variant="outline" onClick={() => void load()}>
              Retry
            </Button>
          }
        />
      ) : data ? (
        <div className="space-y-6">
          {data.credits.in_deficit ? (
            <div
              role="alert"
              className="flex items-start gap-3 rounded-xl border border-destructive/40 bg-destructive/5 p-5"
            >
              <AlertTriangle
                className="mt-0.5 h-5 w-5 shrink-0 text-destructive"
                aria-hidden="true"
              />
              <div className="min-w-0">
                <p className="font-semibold">New assessment invitations are paused</p>
                <p className="mt-1 text-pretty leading-7">
                  {data.credits.deficit_message}
                </p>
                <p className="mt-1 text-sm">
                  Assessments already in progress are unaffected, and every
                  candidate profile stays exactly where it is.
                </p>
              </div>
            </div>
          ) : null}

          {/* Two-tier balance warnings (directive Part 5 §4): LOW at 20
              credits, CRITICAL at 10, the critical tier is persistent and
              urgent, and both carry the top-up action directly. */}
          {!data.credits.in_deficit &&
          (data.credits.warning_level ?? 0) > 0 &&
          data.credits.alert_message ? (
            <div
              role="alert"
              className={
                (data.credits.warning_level ?? 0) >= 2
                  ? "flex items-start gap-3 border border-destructive/50 bg-destructive/10 p-5"
                  : "flex items-start gap-3 border border-warning/50 bg-warning/10 p-5"
              }
            >
              <AlertTriangle
                className={
                  (data.credits.warning_level ?? 0) >= 2
                    ? "mt-0.5 h-5 w-5 shrink-0 text-destructive"
                    : "mt-0.5 h-5 w-5 shrink-0 text-warning"
                }
                aria-hidden="true"
              />
              <div className="min-w-0 flex-1">
                <p className="font-semibold">
                  {(data.credits.warning_level ?? 0) >= 2
                    ? "Critical credit balance"
                    : "Credits running low"}
                </p>
                <p className="mt-1 text-pretty leading-7">
                  {data.credits.alert_message}
                </p>
              </div>
              <Button
                variant={(data.credits.warning_level ?? 0) >= 2 ? "default" : "outline"}
                onClick={() =>
                  document
                    .getElementById("billing-plans")
                    ?.scrollIntoView({ behavior: "smooth", block: "start" })
                }
              >
                {(data.credits.warning_level ?? 0) >= 2
                  ? "Top Up Immediately"
                  : "Top Up Credits"}
              </Button>
            </div>
          ) : null}

          {/* ── Balance ────────────────────────────────────────────────── */}
          <Section
            title="Credit balance"
            description="Your monthly allowance and any Starter top-ups share one pool across all open jobs."
          >
            <dl className="grid gap-6 sm:grid-cols-2">
              <DetailItem label="Credit balance">
                <span className="text-2xl font-semibold tabular-nums">
                  {data.credits.balance_credits}
                </span>{" "}
                credits
              </DetailItem>
              {Number(data.credits.balance_credits) > 0 ? (
                <DetailItem label="Completed assessments remaining">
                  {Math.floor(Number(data.credits.balance_credits))}
                </DetailItem>
              ) : null}
            </dl>
          </Section>

          {/* ── Usage this month ───────────────────────────────────────── */}
          <Section
            title="This month"
            description="Only completed assessments draw credits. Incomplete attempts and no-shows do not."
          >
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="rounded-xl border border-border bg-surface p-4">
                <p className="text-sm font-medium">Completed assessments</p>
                <p className="mt-2 text-2xl font-semibold tabular-nums">
                  {toCredits(data.credits.usage_this_month_subunits.completed_assessment ?? 0, data.credits.subunits_per_credit)}
                </p>
                <p className="mt-1 text-xs">credits used</p>
              </div>
            </div>
            <div className="mt-6 grid gap-6 sm:grid-cols-3">
              <DetailItem label="Carried over from last month">
                {data.credits.rollover_credits} credits
              </DetailItem>
              <DetailItem label="Granted to date">
                {toCredits(
                  data.credits.granted_subunits,
                  data.credits.subunits_per_credit
                )}{" "}
                credits
              </DetailItem>
              <DetailItem label="Used to date">
                {toCredits(
                  data.credits.consumed_subunits,
                  data.credits.subunits_per_credit
                )}{" "}
                credits
              </DetailItem>
            </div>
          </Section>

          <Section
            title="Monthly plan"
            description="One completed assessment uses one credit. Unused monthly credits roll over for three months, then expire. Every plan includes every feature, all 7 agents and BGV reconfirm. Credits pool across all open jobs."
          >
            {currentPlan ? (
              <p className="text-sm">
                {currentPlan.plan_slug ? `Current plan: ${currentPlan.plan_slug}. ` : "No plan yet. "}
                {currentPlan.status ? `Status: ${currentPlan.status}. ` : ""}
                {currentPlan.current_end ? `Current period ends ${formatDate(currentPlan.current_end)}. ` : ""}
                {currentPlan.pending_plan_slug ? `Next plan: ${currentPlan.pending_plan_slug}.` : ""}
              </p>
            ) : null}
            {monthlyPlans ? (
              <div className="mt-5 grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                {monthlyPlans.map((plan) => (
                  <div key={plan.slug} className="rounded-xl border border-border bg-surface p-4">
                    <h3 className="font-semibold">{plan.name}</h3>
                    <p className="mt-2 text-xl font-semibold tabular-nums">{formatInr(plan.price_inr)} / month</p>
                    <p className="mt-2 text-sm">{plan.assessments.toLocaleString("en-IN")} completed assessments</p>
                    <p className="mt-2 text-xs">GST {formatInr(plan.gst_inr)}. Total {formatInr(plan.total_inr)}.</p>
                    <p className="mt-3 text-sm font-medium">Unused credits roll over for {plan.rollover_months} months, then expire.</p>
                    <Button className="mt-4 w-full" variant="outline" disabled={!canManage || planBusy || ["created", "pending", "cancelling"].includes(currentPlan?.status ?? "") || (currentPlan?.status === "active" && currentPlan.plan_slug === plan.slug)} onClick={() => void selectPlan(plan)}>
                      {currentPlan?.status === "active" && currentPlan.plan_slug === plan.slug ? "Current plan" : currentPlan?.status === "active" ? "Switch next month" : "Choose plan"}
                    </Button>
                  </div>
                ))}
              </div>
            ) : <LoadingRows rows={2} label="Loading monthly plans" />}
            {currentPlan?.status === "active" && canManage ? (
              <Button variant="outline" className="mt-5" disabled={planBusy} onClick={() => void cancelPlan()}>Cancel at period end</Button>
            ) : null}
            <p className="mt-4 text-sm">Monthly billing, no annual commitment or lock-in. Cancel anytime. The paid pilot starts on Starter for 30 days, then you can keep it or stop.</p>
          </Section>

          {/* Starter top-up */}
          {/* This wrapper carries the #billing-plans anchor the warning
              banners scroll to: a top-up has to land on the packs. */}
          <div id="billing-plans" className="scroll-mt-24">
            <Section
              title="Starter top-up"
              description={
                canManage
                  ? `The only top-up is Starter: 75 completed assessments. Added credits remain valid for ${data.credits.credit_validity_months} months.`
                  : "Ask your Company Admin to buy a Starter top-up."
              }
            >
              {/* Balance shown BEFORE the choice (directive Part 5 §7.2). */}
              <p className="text-sm">
                Current balance:{" "}
                <span className="text-lg font-semibold tabular-nums">
                  {data.credits.balance_credits}
                </span>{" "}
                credits
              </p>

              {packsError ? (
                <div className="mt-4 flex flex-wrap items-center gap-3">
                  <p className="text-sm">
                    Could not load credit packs. {packsError}
                  </p>
                  <Button variant="outline" onClick={() => void loadPacks()}>
                    Retry
                  </Button>
                </div>
              ) : !packs ? (
                <div className="mt-4">
                  <LoadingRows rows={2} label="Loading credit packs" />
                </div>
              ) : (
                <>
                  <CreditPackPicker
                    packs={packs}
                    selectedSlug={selectedSlug}
                    onSelect={setSelectedSlug}
                    onBuy={(pack) => void buyPack(pack)}
                    busy={payBusy}
                    canBuy={canManage}
                    cannotBuyNote="Only your Company Admin can complete a purchase."
                  />
                </>
              )}
            </Section>
          </div>

          {/* ── Credit statement ───────────────────────────────────────── */}
          {/* Every credit movement, paged from GET /billing/ledger. The
              overview carries only the newest rows, so its newest id is the
              token that sends the statement back to page one when a purchase
              or a verified checkout moves the ledger. */}
          <Section
            title="Credit statement"
            description="Every credit movement on your account, newest first."
          >
            <CreditStatement
              refreshToken={data.recent_ledger[0]?.id ?? "empty"}
            />
          </Section>

          {monthlyCharges && monthlyCharges.length > 0 ? (
            <Section title="Monthly payments and invoices" description="Each paid month grants its completed assessment allowance.">
              <ul className="space-y-3">
                {monthlyCharges.map((charge) => (
                  <li key={charge.id} className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-surface p-4">
                    <div>
                      <p className="font-medium">{charge.plan_slug} · {charge.assessments_granted} completed assessments</p>
                      <p className="text-sm text-muted-foreground">{formatDate(charge.created_at)} · {formatInr(charge.subtotal_inr)} + {formatInr(charge.gst_inr)} GST</p>
                    </div>
                    <a className="inline-flex items-center gap-1 text-sm underline" href={`${API_BASE}/billing/monthly/charges/${charge.id}/invoice`} download>
                      <Download className="h-4 w-4" aria-hidden="true" />{charge.invoice_number ?? "Download invoice"}
                    </a>
                  </li>
                ))}
              </ul>
            </Section>
          ) : null}

          {/* ── Purchase history (directive Part 5 sections 7.3, 7.4) ──── */}
          {/* Hidden while the endpoint is unavailable; empty text otherwise,
              so a paid invoice is never silently absent from a loaded list. */}
          {purchases !== null ? (
            <Section
              title="Purchases and invoices"
              description="Every credit pack purchase, with its GST invoice."
            >
              {purchases.length === 0 ? (
                <p className="leading-7">
                  No purchases yet. Your first credit pack will appear here
                  with its downloadable GST invoice.
                </p>
              ) : (
                <>
                  <div className="hidden md:block">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Date</TableHead>
                          <TableHead>Credits</TableHead>
                          <TableHead>Status</TableHead>
                          <TableHead>Invoice</TableHead>
                          <TableHead className="text-right">Total</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {purchases.map((row) => (
                          <TableRow key={row.id}>
                            <TableCell>{formatDate(row.created_at)}</TableCell>
                            <TableCell>
                              {row.credits_purchased}
                              {row.bonus_credits > 0
                                ? ` + ${row.bonus_credits} bonus`
                                : ""}
                            </TableCell>
                            <TableCell>
                              {PURCHASE_STATUS_LABELS[row.status] ?? row.status}
                            </TableCell>
                            <TableCell>
                              {row.status === "paid" ? (
                                <a
                                  className="inline-flex items-center gap-1 underline"
                                  href={invoiceHref(row.id)}
                                  download
                                >
                                  <Download
                                    className="h-3.5 w-3.5"
                                    aria-hidden="true"
                                  />
                                  {row.invoice_number ?? "Download invoice"}
                                </a>
                              ) : (
                                (row.invoice_number ?? "Not issued")
                              )}
                            </TableCell>
                            <TableCell className="text-right font-medium">
                              {formatInr(row.total_inr)}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                  <ul className="space-y-3 md:hidden">
                    {purchases.map((row) => (
                      <li
                        key={row.id}
                        className="rounded-xl border border-border bg-surface p-4"
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <p className="text-sm font-medium">
                              {row.credits_purchased} credits
                              {row.bonus_credits > 0
                                ? ` + ${row.bonus_credits} bonus`
                                : ""}
                            </p>
                            <p className="mt-1 text-xs">
                              {formatDate(row.created_at)},{" "}
                              {PURCHASE_STATUS_LABELS[row.status] ?? row.status}
                            </p>
                          </div>
                          <span className="shrink-0 text-sm font-semibold">
                            {formatInr(row.total_inr)}
                          </span>
                        </div>
                        {row.status === "paid" ? (
                          <a
                            className="mt-2 inline-flex items-center gap-1 text-sm underline"
                            href={invoiceHref(row.id)}
                            download
                          >
                            <Download
                              className="h-3.5 w-3.5"
                              aria-hidden="true"
                            />
                            {row.invoice_number ?? "Download invoice"}
                          </a>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </Section>
          ) : null}

          {data.transactions.length > 0 ? (
            <Section title="Payments" description="What was charged, and when.">
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Date</TableHead>
                      <TableHead>Type</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead className="text-right">Amount</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.transactions.map((row) => (
                      <TableRow key={row.id}>
                        <TableCell>{formatDate(row.created_at)}</TableCell>
                        <TableCell>
                          {row.transaction_type === "credit_pack"
                            ? "Credit pack"
                            : row.transaction_type === "subscription_charge"
                              ? "Monthly plan"
                            : "Refund"}
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant={
                              row.status === "success" ? "brand" : "outline"
                            }
                          >
                            {row.status === "success"
                              ? "Paid"
                              : row.status === "failed"
                                ? "Failed"
                                : "Refunded"}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-right font-medium">
                          {formatInr(row.amount_inr)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </Section>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
