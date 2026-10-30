"use client";

// One Talent Intelligence dashboard (2026-09-05 spec, sections 3 and 4).
//
// Widgets are metric cards: the value with its unit, the specification's
// formula, the health bands in words, and the server-decided health WORD as
// a colored chip. A metric with nothing to measure renders its plain-language
// reason ("No data yet: ...") instead of a fabricated zero. The client never
// re-derives a band, and no per-candidate score exists on this surface.

import * as React from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { useParams } from "next/navigation";

import { apiGet } from "@/lib/api";
import type {
  IntelligenceDashboardDetail,
  IntelligenceHealth,
  IntelligenceMetric,
} from "@/lib/types";
import { PageHeader } from "@/components/app-shell";
import { ErrorState, LoadingCards } from "@/components/page-primitives";

const HEALTH_CHIP: Record<IntelligenceHealth, { label: string; tone: string }> = {
  green: {
    label: "Green",
    tone:
      "border-emerald-700 bg-emerald-50 text-emerald-950 dark:border-emerald-400 dark:bg-emerald-950/40 dark:text-emerald-50",
  },
  amber: {
    label: "Amber",
    tone:
      "border-amber-600 bg-amber-50 text-amber-950 dark:border-amber-400 dark:bg-amber-950/40 dark:text-amber-50",
  },
  red: {
    label: "Red",
    tone:
      "border-red-600 bg-red-50 text-red-950 dark:border-red-400 dark:bg-red-950/40 dark:text-red-50",
  },
  no_data: {
    label: "No data",
    tone:
      "border-border bg-muted text-foreground",
  },
};

function formatValue(metric: IntelligenceMetric): string {
  if (metric.value === null) return "";
  const value = Number.isInteger(metric.value)
    ? metric.value.toString()
    : metric.value.toFixed(2);
  if (metric.unit === "%") return `${value}%`;
  return `${value} ${metric.unit}`;
}

function MetricCard({ metric }: { metric: IntelligenceMetric }) {
  const chip = HEALTH_CHIP[metric.status];
  return (
    <article className="flex flex-col rounded-xl border border-border bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-sm font-semibold leading-snug">{metric.title}</h3>
        <span
          className={`shrink-0 rounded-full border px-2 py-0.5 text-xs font-semibold ${chip.tone}`}
        >
          {chip.label}
        </span>
      </div>
      {metric.status === "no_data" ? (
        <p className="mt-3 text-sm leading-snug">
          No data yet: {metric.status_reason}
        </p>
      ) : (
        <p className="mt-3 text-2xl font-semibold tabular-nums">
          {formatValue(metric)}
        </p>
      )}
      <dl className="mt-3 space-y-2 text-sm leading-snug">
        <div>
          <dt className="font-semibold">Formula</dt>
          <dd className="font-mono text-xs">{metric.formula}</dd>
        </div>
        <div>
          <dt className="font-semibold">Health bands</dt>
          <dd>{metric.thresholds}</dd>
        </div>
        {metric.proxy_note ? (
          <div>
            <dt className="font-semibold">How it is measured here</dt>
            <dd>{metric.proxy_note}</dd>
          </div>
        ) : null}
      </dl>
    </article>
  );
}

export default function IntelligenceDashboardPage() {
  const params = useParams<{ key: string }>();
  const [detail, setDetail] = React.useState<IntelligenceDashboardDetail | null>(
    null
  );
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!params?.key) return;
    let active = true;
    apiGet<IntelligenceDashboardDetail>(
      `/intelligence/dashboards/${encodeURIComponent(params.key)}`
    )
      .then((result) => {
        if (active) setDetail(result);
      })
      .catch(() => {
        if (active) setError("This dashboard could not be loaded.");
      });
    return () => {
      active = false;
    };
  }, [params?.key]);

  return (
    <div>
      <Link
        href="/org/intelligence"
        className="mb-4 inline-flex items-center gap-1 text-sm font-medium text-foreground hover:text-teal-700 dark:hover:text-teal-300"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        All dashboards
      </Link>
      {error ? (
        <ErrorState description={error} />
      ) : detail === null ? (
        <LoadingCards count={3} />
      ) : (
        <>
          <PageHeader
            eyebrow={`Tier ${detail.tier}: ${detail.tier_title}`}
            title={detail.title}
            description={
              <>
                {detail.description}
                <span className="mt-1 block text-sm font-medium">
                  Audience: {detail.audience}
                </span>
              </>
            }
          />
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {detail.widgets.map((metric) => (
              <MetricCard key={metric.metric_id} metric={metric} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
