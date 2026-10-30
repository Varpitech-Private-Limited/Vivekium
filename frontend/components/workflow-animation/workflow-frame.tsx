"use client";

import type { ReactNode } from "react";
import { motion } from "framer-motion";
import { BarChart3, BriefcaseBusiness, UsersRound } from "lucide-react";

import { cn } from "@/lib/utils";

export const SPRING = { type: "spring", stiffness: 260, damping: 24 } as const;
export const EASE = [0.22, 1, 0.36, 1] as const;

export function DemoWindow({
  children,
  active = "dashboard",
  compact = false,
}: {
  children: ReactNode;
  active?: "dashboard" | "jobs" | "candidates";
  compact?: boolean;
}) {
  return (
    <div className="h-full overflow-hidden rounded-none border border-white/10 bg-black text-white">
      <div className="flex h-9 items-center gap-2 border-b border-white/10 bg-white/[0.035] px-4">
        <span className="h-2.5 w-2.5 rounded-full bg-rose-400/80" />
        <span className="h-2.5 w-2.5 rounded-full bg-amber-300/80" />
        <span className="h-2.5 w-2.5 rounded-full bg-emerald-400/80" />
        <div className="ml-3 h-4 w-32 rounded-full bg-white/[0.06] sm:w-48" />
        <span className="ml-auto text-[9px] font-semibold tracking-[.2em] text-white/35">
          READYPICK
        </span>
      </div>
      <div className="flex h-[calc(100%-2.25rem)]">
        {!compact ? (
          <aside className="hidden w-32 shrink-0 border-r border-white/10 bg-white/[0.025] p-3 sm:block lg:w-40">
            <div className="mb-5 flex items-center gap-2 px-2 text-[10px] font-bold tracking-[.15em] text-teal-400">
              {/* V and WORKSPACE. It read P and READY WORKSPACE, the
                  previous name's initial and half its wordmark. */}
              <span className="grid h-6 w-6 place-items-center rounded-md bg-teal-600/20">
                V
              </span>
              WORKSPACE
            </div>
            <DemoNav
              icon={BarChart3}
              label="Overview"
              active={active === "dashboard"}
            />
            <DemoNav
              icon={BriefcaseBusiness}
              label="Jobs"
              active={active === "jobs"}
            />
            <DemoNav
              icon={UsersRound}
              label="Candidates"
              active={active === "candidates"}
            />
          </aside>
        ) : null}
        <div className="min-w-0 flex-1 overflow-hidden bg-[radial-gradient(circle_at_88%_0%,hsl(var(--teal-600)/0.12),transparent_42%)]">
          {children}
        </div>
      </div>
    </div>
  );
}

function DemoNav({
  icon: Icon,
  label,
  active,
}: {
  icon: typeof BriefcaseBusiness;
  label: string;
  active?: boolean;
}) {
  return (
    <div
      className={cn(
        "mb-1 flex items-center gap-2 rounded-lg px-2 py-2 text-[10px] text-white/45",
        active && "bg-teal-600/15 font-semibold text-teal-100"
      )}
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
    </div>
  );
}

export function SceneHeader({
  eyebrow,
  title,
  action,
}: {
  eyebrow: string;
  title: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <p className="text-[9px] font-semibold uppercase tracking-[.18em] text-teal-400/80">
          {eyebrow}
        </p>
        <h3 className="mt-1 text-sm font-semibold sm:text-base">{title}</h3>
      </div>
      {action}
    </div>
  );
}

export function GlowButton({
  children,
  success = false,
}: {
  children: ReactNode;
  success?: boolean;
}) {
  return (
    <motion.div
      // No glow, ever: directive Part 1 section 6 bans glowing buttons even
      // inside an illustrative animation. The pulse is a scale breath alone.
      animate={{
        scale: success ? [1, 0.96, 1.02, 1] : [1, 1.02, 1],
      }}
      transition={{ duration: success ? 0.8 : 1.8, repeat: success ? 0 : Infinity }}
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-[10px] font-semibold",
        success
          ? "bg-emerald-500 text-white"
          // The frame is black in both themes, so the action is the dark-mode
          // primary: white fill, black label. A navy ramp would invert to a
          // white-to-blue wash once the theme tokens flip.
          : "bg-white text-black"
      )}
    >
      {children}
    </motion.div>
  );
}

export function TinyBadge({
  children,
  tone = "brand",
}: {
  children: ReactNode;
  // Named for the ROLE, not the hue. A prop called "violet" that renders
  // teal is worse than the colour was: the name lies about what it draws.
  tone?: "brand" | "green" | "amber";
}) {
  return (
    <span
      className={cn(
        "inline-flex rounded-none border px-2 py-0.5 text-[8px] font-semibold",
        tone === "brand" && "border-teal-400/25 bg-teal-400/10 text-teal-100",
        tone === "green" && "border-emerald-400/25 bg-emerald-400/10 text-emerald-200",
        tone === "amber" && "border-amber-300/25 bg-amber-300/10 text-amber-100"
      )}
    >
      {children}
    </span>
  );
}

export function MetricCard({
  label,
  value,
  delay = 0,
}: {
  label: string;
  value: string;
  delay?: number;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay, duration: 0.45, ease: EASE }}
      className="rounded-xl border border-white/10 bg-white/[0.045] p-3"
    >
      <p className="text-[9px] text-white/45">{label}</p>
      <p className="mt-1 text-lg font-semibold tracking-tight">{value}</p>
    </motion.div>
  );
}

export function AnimatedCursor({
  points,
}: {
  points: Array<{ x: number; y: number }>;
}) {
  return (
    <motion.div
      aria-hidden="true"
      initial={points[0]}
      animate={{
        x: points.map((point) => point.x),
        y: points.map((point) => point.y),
      }}
      transition={{ duration: 2.2, times: points.map((_, i) => i / (points.length - 1)), ease: EASE }}
      className="pointer-events-none absolute z-20 h-4 w-4"
    >
      <svg viewBox="0 0 16 16" className="h-full w-full drop-shadow">
        <path d="M1 1l12 5-5 2-2 5z" fill="white" className="stroke-navy-600" strokeWidth="1" />
      </svg>
    </motion.div>
  );
}
