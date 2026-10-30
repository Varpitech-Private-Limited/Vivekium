"use client";

import * as React from "react";
import { AnimatePresence, motion, useInView, useReducedMotion } from "framer-motion";
import {
  ChevronLeft,
  ChevronRight,
  Pause,
  Play,
  RotateCcw,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { EASE } from "./workflow-frame";
import { WORKFLOW_SCENES } from "./step-scenes";

const STEPS = [
  { short: "Login", title: "Secure sign in", duration: 2600 },
  { short: "Workspace", title: "Workspace comes alive", duration: 2000 },
  { short: "Create", title: "Write the JD and its Skills", duration: 3400 },
  { short: "Publish", title: "Publish for 30 days", duration: 1900 },
  { short: "Profiles", title: "Candidate evidence arrives", duration: 2400 },
  { short: "Match", title: "AI Match settles", duration: 3000 },
  { short: "Assess", title: "Invite the strongest profiles", duration: 2800 },
  { short: "PRISM", title: "Read the PRISM Report", duration: 3500 },
  { short: "Decide", title: "Shortlist and communicate", duration: 2700 },
] as const;

export function WorkflowAnimation() {
  const reduceMotion = useReducedMotion();
  const viewportRef = React.useRef<HTMLDivElement>(null);
  const inView = useInView(viewportRef, { amount: 0.35 });
  const [step, setStep] = React.useState(0);
  const [playing, setPlaying] = React.useState(!reduceMotion);
  const [cycle, setCycle] = React.useState(0);
  const CurrentScene = WORKFLOW_SCENES[step];

  React.useEffect(() => {
    if (!playing || !inView || reduceMotion) return;
    const timer = window.setTimeout(() => {
      setStep((current) => (current + 1) % STEPS.length);
      setCycle((current) => current + 1);
    }, STEPS[step].duration);
    return () => window.clearTimeout(timer);
  }, [inView, playing, reduceMotion, step, cycle]);

  React.useEffect(() => {
    if (reduceMotion) setPlaying(false);
  }, [reduceMotion]);

  const move = (direction: -1 | 1) => {
    setStep((current) => (current + direction + STEPS.length) % STEPS.length);
    setCycle((current) => current + 1);
  };

  const choose = (next: number) => {
    setStep(next);
    setCycle((current) => current + 1);
  };

  return (
    <div ref={viewportRef} className="relative">
      <div className="overflow-hidden rounded-none border border-white/10 bg-black p-2 sm:p-3">
        {/* A FIXED height below `sm`, never an aspect ratio with a minimum
            height: `aspect-[16/10] min-h-[340px]` made the box 544px WIDE on
            a 390px phone (the minimum height drags the width up through the
            ratio), and the frame's overflow clip cut every scene's right
            third off. */}
        <div className="relative h-[420px] overflow-hidden rounded-none sm:aspect-[16/9] sm:h-auto">
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={`${step}-${cycle}`}
              initial={{ opacity: 0, scale: 0.985, filter: "blur(8px)" }}
              animate={{ opacity: 1, scale: 1, filter: "blur(0px)" }}
              exit={{ opacity: 0, scale: 1.01, filter: "blur(6px)" }}
              transition={{ duration: reduceMotion ? 0 : 0.42, ease: EASE }}
              className="absolute inset-0"
            >
              <CurrentScene />
            </motion.div>
          </AnimatePresence>
        </div>

        <div className="px-2 pb-2 pt-4 sm:px-3">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="min-w-0">
              <p className="text-[10px] font-semibold uppercase tracking-[.18em] text-teal-400">
                Step {step + 1} of {STEPS.length}
              </p>
              <p className="truncate text-sm font-medium text-white">{STEPS[step].title}</p>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <Control label="Previous step" onClick={() => move(-1)}>
                <ChevronLeft />
              </Control>
              <Control label={playing ? "Pause animation" : "Play animation"} onClick={() => setPlaying((value) => !value)}>
                {playing ? <Pause /> : <Play />}
              </Control>
              <Control label="Next step" onClick={() => move(1)}>
                <ChevronRight />
              </Control>
              <Control
                label="Restart animation"
                onClick={() => {
                  choose(0);
                  setPlaying(!reduceMotion);
                }}
              >
                <RotateCcw />
              </Control>
            </div>
          </div>

          <div className="grid grid-cols-9 gap-1" aria-label="Workflow steps">
            {STEPS.map((item, index) => (
              <button
                key={item.short}
                type="button"
                onClick={() => choose(index)}
                className="group min-w-0 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400"
                aria-label={`Show step ${index + 1}: ${item.title}`}
                aria-current={index === step ? "step" : undefined}
              >
                <span className="relative block h-1.5 overflow-hidden rounded-full bg-white/10">
                  {index < step ? <span className="absolute inset-0 bg-teal-400/65" /> : null}
                  {index === step ? (
                    <motion.span
                      key={`${step}-${cycle}-progress`}
                      initial={{ width: 0 }}
                      animate={{ width: playing && inView && !reduceMotion ? "100%" : "12%" }}
                      transition={{
                        duration: playing && inView && !reduceMotion ? item.duration / 1000 : 0.25,
                        ease: "linear",
                      }}
                      // Single hue. DESIGN.md §2 rule 2: no gradient between two hues -- the
                      // two-hue ramp it replaced is the exact tell being removed.
                      className="absolute inset-y-0 left-0 rounded-full bg-gradient-to-r from-teal-600 to-teal-400"
                    />
                  ) : null}
                </span>
                <span
                  className={cn(
                    "mt-1.5 hidden truncate text-[8px] font-medium text-white/30 transition-colors sm:block",
                    index === step && "text-white/80"
                  )}
                >
                  {item.short}
                </span>
              </button>
            ))}
          </div>
        </div>
      </div>

      <p className="sr-only" aria-live="polite">
        Showing step {step + 1}: {STEPS[step].title}
      </p>
    </div>
  );
}

function Control({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactElement<{ className?: string }>;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      // Same rule as the light-theme fields: visible AT REST, not only under
      // the pointer. `border-white/10` composites to 1.30:1 against this
      // section's near-black surface, so these playback controls were
      // effectively invisible until you happened to hover them. Measured by
      // scripts/visual-qa.mjs, which composites the alpha rather than
      // comparing the raw rgba strings.
      className="grid h-8 w-8 place-items-center rounded-lg border border-teal-400/70 bg-white/[0.045] text-white/80 transition hover:border-teal-400 hover:bg-teal-400/15 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400"
    >
      {React.cloneElement(children, { className: "h-3.5 w-3.5" })}
    </button>
  );
}
