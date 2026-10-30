import type { Metadata } from "next";
import { BrainCircuit, Handshake, History, UsersRound } from "lucide-react";

import { FadeIn, Stagger, StaggerItem } from "@/components/motion";
import { publicPageMetadata } from "@/lib/site";

export const metadata: Metadata = publicPageMetadata({
  path: "/about",
  title: "About",
  description:
    "The experience, philosophy and people behind Vivekium's evidence-led candidate profiles.",
});

/**
 * The three principles, each a claim about the product as it ships
 * (2026-09-28). "Human validation" promised a final check by somebody at
 * ReadyPick before every profile reached a customer; no such step exists, and
 * the human in the loop is the customer's own team, which the product
 * enforces (no flag rejects anybody, a person decides). "A flat job
 * subscription" was the pricing model before credits: a customer buys credits
 * and spends them per completed PRISM Report.
 */
const PRINCIPLES = [
  {
    icon: BrainCircuit,
    title: "AI for leverage",
    body: "Discovery, matching and assessment should remove repetitive work while keeping the evidence visible.",
  },
  {
    icon: UsersRound,
    title: "Human decisions",
    body: "The AI reads, assesses and explains. A person on your team decides, and no flag ever rejects a candidate on its own.",
  },
  {
    icon: Handshake,
    title: "Aligned commercial model",
    body: "Credits spent per completed PRISM Report keep our incentive on report quality, not on a percentage of compensation.",
  },
] as const;

export default function AboutPage() {
  return (
    <main id="main">
      <section className="relative overflow-hidden border-b border-border py-20 lg:py-28">
        <div aria-hidden="true" className="absolute -top-40 left-1/2 h-[32rem] w-[48rem] -translate-x-1/2 rounded-full bg-brand-600/15 blur-[120px]" />
        <FadeIn className="relative mx-auto max-w-4xl px-6 text-center lg:px-10">
          <p className="type-eyebrow text-teal-700">About Vivekium</p>
          <h1 className="mx-auto mt-5 type-display">
            Built from inside HR, for the decisions HR has to defend
          </h1>
          <p className="mx-auto mt-6 type-lead">
            Vivekium is the next chapter of a long operating journey: learning what teams need when sourcing, screening, validation and decision support have to work as one.
          </p>
        </FadeIn>
      </section>

      <section className="mx-auto max-w-6xl px-6 py-20 lg:px-10 lg:py-28">
        <div className="grid gap-12 lg:grid-cols-[.9fr_1.1fr] lg:items-start">
          <FadeIn className="rounded-3xl border border-border bg-surface p-8 shadow-card">
            <History className="h-8 w-8 text-brand-600" aria-hidden="true" />
            <p className="mt-7 type-eyebrow text-brand-600">The evolution</p>
            <h2 className="mt-3 text-2xl font-semibold">Built for its time. Rebuilt for this one.</h2>
          </FadeIn>
          <FadeIn delay={0.08} className="space-y-5 type-prose-lg">
            <p>
              Before Vivekium, Recruitrix.ai brought profiles, assessments, verification and delivery into one platform when many teams were still assembling those pieces separately. Its remote-ready model supported more than 60 customers across India, delivered more than 10,000 jobs and led to an acquisition.
            </p>
            <p>
              The market moved. AI matured, candidate expectations changed and people teams needed more control over how evidence becomes a decision. Vivekium takes the practical lessons from that journey and rebuilds the operating model from first principles.
            </p>
            <p>
              Vivekium turns the job&apos;s skills, the evidence in each resume and one proctored assessment into one clear assessment trail, so teams can spend interview time on the questions that matter.
            </p>
          </FadeIn>
        </div>
      </section>

      <section className="border-y border-border bg-surface/55 py-20">
        <div className="mx-auto max-w-6xl px-6 lg:px-10">
          <FadeIn className="max-w-2xl">
            <p className="type-eyebrow text-brand-600">How we work</p>
            <h2 className="mt-3 type-section-title">Lean by design, accountable by default</h2>
            <p className="mt-5 type-lead">
              A small, high-leverage team builds AI that reads, assesses and explains, and leaves every decision about a person with the customer&apos;s own team. The structure is deliberate: enough process for consistency, without layers that slow a customer down.
            </p>
          </FadeIn>
          <Stagger className="mt-10 grid gap-5 md:grid-cols-3">
            {PRINCIPLES.map((principle) => (
              <StaggerItem key={principle.title}>
                <article className="h-full rounded-2xl border border-border bg-canvas p-6 shadow-card">
                  <principle.icon className="h-6 w-6 text-brand-600" aria-hidden="true" />
                  <h3 className="mt-5 text-lg font-semibold">{principle.title}</h3>
                  <p className="mt-3 text-sm">{principle.body}</p>
                </article>
              </StaggerItem>
            ))}
          </Stagger>
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-6 py-20 lg:px-10 lg:py-28">
        <div className="grid items-center gap-10 lg:grid-cols-[.85fr_1.15fr]">
          <FadeIn className="relative aspect-[4/3] overflow-hidden rounded-3xl bg-black text-white shadow-pop">
            <div aria-hidden="true" className="absolute inset-0 bg-[radial-gradient(circle_at_70%_10%,hsl(var(--teal-600)/.38),transparent_45%)]" />
            <div className="absolute inset-0 grid place-items-center">
              <span className="grid h-36 w-36 place-items-center rounded-full border border-white/15 bg-white/[.06] text-7xl font-black text-teal-400">M</span>
            </div>
            <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/85 to-transparent p-7">
              <p className="font-semibold">Manjunath</p>
              <p className="mt-1 text-sm text-white/60">Founder &amp; CEO · HR StraTech Leader</p>
              <p className="mt-3 text-sm font-medium text-teal-100">Built by HR. For HR.</p>
            </div>
          </FadeIn>
          <FadeIn delay={0.08}>
            <p className="type-eyebrow text-brand-600">Founder</p>
            <h2 className="mt-3 type-section-title">The problem was lived before it was coded</h2>
            <div className="mt-6 space-y-5 text-pretty leading-8">
              <p>
                Manjunath spent more than 25 years in HR - as a practitioner, transformation leader and builder of the platform he believed the function was missing.
              </p>
              <p>
                He reviewed more than 600 HR technology platforms, advised over 50 companies on where their people processes break and mentored more than 100 HR professionals. Those conversations shaped a simple standard: technology should give time back, not add another system to manage.
              </p>
              <p>
                Vivekium is not a side project. It is the operating belief that teams deserve clear evidence, candidates deserve clarity and the final decision must stay human.
              </p>
            </div>
          </FadeIn>
        </div>
      </section>
    </main>
  );
}
