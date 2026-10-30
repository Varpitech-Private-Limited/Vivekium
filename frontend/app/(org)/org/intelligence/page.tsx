"use client";

// Talent Intelligence dashboards index (2026-09-05 spec, sections 2 and 5.2).
//
// The 18 dashboards, grouped under their four strategic tiers, plus the
// Alerts panel: current threshold breaches computed on read by the server.
// Every figure on these pages is an OPERATIONAL metric (latency, ratio,
// compliance percentage); candidate quality never appears here as a number.
// Health is a server-decided WORD rendered as a colored chip; this page
// never re-derives a band.

import * as React from "react";
import Link from "next/link";
import { AlertTriangle, ArrowRight } from "lucide-react";

import { apiGet } from "@/lib/api";
import type {
  IntelligenceAlert,
  IntelligenceAlerts,
  IntelligenceDashboardIndex,
  IntelligenceDashboardSummary,
} from "@/lib/types";
import { PageHeader } from "@/components/app-shell";
import { ErrorState, LoadingCards } from "@/components/page-primitives";

function AlertRow({ alert }: { alert: IntelligenceAlert }) {
  const tone =
    alert.severity === "red"
      ? "border-red-600 bg-red-50 text-red-950 dark:bg-red-950/40 dark:text-red-50"
      : "border-amber-600 bg-amber-50 text-amber-950 dark:bg-amber-950/40 dark:text-amber-50";
  const body = (
    <div className={`flex items-start gap-3 rounded-lg border p-3 ${tone}`}>
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <div className="min-w-0">
        <p className="text-sm font-semibold">{alert.title}</p>
        <p className="text-sm">{alert.detail}</p>
      </div>
    </div>
  );
  return alert.link_path ? (
    <Link href={alert.link_path} className="block">
      {body}
    </Link>
  ) : (
    body
  );
}

function AlertsPanel() {
  const [alerts, setAlerts] = React.useState<IntelligenceAlert[] | null>(null);
  const [failed, setFailed] = React.useState(false);

  React.useEffect(() => {
    let active = true;
    apiGet<IntelligenceAlerts>("/intelligence/alerts")
      .then((result) => {
        if (active) setAlerts(result.alerts);
      })
      .catch(() => {
        if (active) setFailed(true);
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <section aria-label="Alerts" className="mb-8">
      <h2 className="mb-3 type-eyebrow">
        Alerts
      </h2>
      {failed ? (
        <p className="text-sm">
          Alerts could not be loaded. A failed check never reads as a healthy
          pipeline; reload to retry.
        </p>
      ) : alerts === null ? (
        <p className="text-sm">Checking thresholds...</p>
      ) : alerts.length === 0 ? (
        <p className="rounded-lg border border-border p-3 text-sm">
          No thresholds are breaching right now.
        </p>
      ) : (
        <div className="space-y-2">
          {alerts.map((alert) => (
            <AlertRow key={alert.id} alert={alert} />
          ))}
        </div>
      )}
    </section>
  );
}

function DashboardCard({ dash }: { dash: IntelligenceDashboardSummary }) {
  const measurable = dash.metrics_measurable;
  const hint =
    measurable === dash.metrics_total
      ? "All metrics measurable"
      : measurable === 0
        ? "Awaiting data sources"
        : `${measurable} of ${dash.metrics_total} metrics measurable`;
  return (
    <Link
      href={`/org/intelligence/${dash.key}`}
      className="group flex flex-col rounded-xl border border-border bg-card p-4 transition-colors hover:border-teal-600"
    >
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-sm font-semibold leading-snug">{dash.title}</h3>
        <ArrowRight
          className="mt-0.5 h-4 w-4 shrink-0 text-navy-600 transition-transform group-hover:translate-x-0.5"
          aria-hidden="true"
        />
      </div>
      <p className="mt-2 flex-1 text-sm leading-snug">{dash.description}</p>
      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
        <span className="rounded-full border border-teal-700 px-2 py-0.5 font-medium text-teal-700 dark:border-teal-400 dark:text-teal-300">
          {hint}
        </span>
        <span className="font-medium">{dash.audience}</span>
      </div>
    </Link>
  );
}

export default function IntelligenceIndexPage() {
  const [index, setIndex] = React.useState<IntelligenceDashboardIndex | null>(
    null
  );
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let active = true;
    apiGet<IntelligenceDashboardIndex>("/intelligence/dashboards")
      .then((result) => {
        if (active) setIndex(result);
      })
      .catch(() => {
        if (active) setError("The dashboard registry could not be loaded.");
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div>
      <PageHeader
        title="Talent Intelligence"
        description="Eighteen operational dashboards across four tiers. Metrics without a data source say so plainly; nothing here is ever a fabricated zero."
      />
      <AlertsPanel />
      {error ? (
        <ErrorState description={error} />
      ) : index === null ? (
        <LoadingCards count={6} />
      ) : (
        index.tiers.map((tier) => (
          <section key={tier.tier} className="mb-8" aria-label={tier.title}>
            <h2 className="mb-3 type-eyebrow">
              Tier {tier.tier}: {tier.title}
            </h2>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {tier.dashboards.map((dash) => (
                <DashboardCard key={dash.key} dash={dash} />
              ))}
            </div>
          </section>
        ))
      )}
    </div>
  );
}
