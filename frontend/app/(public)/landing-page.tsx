import Link from "next/link";

import { Badge, ratingVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { REQUEST_ACCESS_HREF } from "@/lib/site";

import { LandingView } from "./landing-view";
import { PricingCatalogue } from "./pricing/pricing-catalogue";
import { SiteFooter } from "./site-footer";
import { SiteHeader } from "./site-header";

/**
 * The public landing page explains the evidence-led hiring flow, shows the
 * brief a hiring team receives, and keeps the live price list beside the
 * company registration action.
 */
export function LandingPage() {
  return (
    <div className="flex min-h-dvh flex-col bg-canvas text-ink">
      <LandingView />
      <SiteHeader />

      <main id="main" className="flex-1">
        <section className="px-6 pb-20 pt-28 lg:px-10 lg:pt-36">
          <div className="mx-auto grid min-h-[34rem] max-w-6xl items-center gap-16 lg:grid-cols-[minmax(0,1fr)_25rem]">
            <div>
              <p className="type-eyebrow text-teal-700">
                For in-house hiring leaders
              </p>
              <h1 className="mt-4 type-display">
                Know them before you contact them.
              </h1>
              <p className="mt-6 type-lead">
                Vivekium reads every person against the skills your team
                actually cares about, then hands you a one-page brief in
                language a hiring manager will finish. No number to argue
                about. A case you can take into the room.
              </p>
              <div className="mt-9 flex flex-col gap-3 sm:flex-row">
                <Button asChild size="lg">
                  <a href={REQUEST_ACCESS_HREF}>Request demo</a>
                </Button>
                <Button asChild size="lg" variant="outline">
                  <Link href="#brief">Read a brief</Link>
                </Button>
              </div>
              <p className="mt-5 text-sm text-muted-foreground">
                Nothing is published until someone on your team has locked the
                role.
              </p>
            </div>

            <aside className="overflow-hidden border border-border bg-surface shadow-hero">
              <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
                <div>
                  <p className="font-semibold text-ink">Senior Data Engineer</p>
                  <p className="text-sm text-muted-foreground">
                    Applicants read by evidence
                  </p>
                </div>
                <span className="text-xs font-medium text-teal-700">Live</span>
              </div>
              <ul className="divide-y divide-border" aria-label="Sample candidate matches">
                <li className="grid grid-cols-[2.25rem_minmax(0,1fr)_auto] items-center gap-3 px-5 py-4">
                  <span className="grid h-9 w-9 place-items-center rounded-full bg-brand-100 text-sm font-semibold text-ink">
                    P
                  </span>
                  <div className="min-w-0">
                    <p className="font-medium text-ink">Priya N.</p>
                    <p className="truncate text-xs text-muted-foreground">
                      Spark, Airflow, data modelling
                    </p>
                  </div>
                  <Badge variant={ratingVariant("Highly Matching")}>
                    Highly Matching
                  </Badge>
                </li>
                <li className="grid grid-cols-[2.25rem_minmax(0,1fr)_auto] items-center gap-3 px-5 py-4">
                  <span className="grid h-9 w-9 place-items-center rounded-full bg-brand-100 text-sm font-semibold text-ink">
                    D
                  </span>
                  <div className="min-w-0">
                    <p className="font-medium text-ink">Daniel A.</p>
                    <p className="truncate text-xs text-muted-foreground">
                      Kafka, SQL
                    </p>
                  </div>
                  <Badge variant={ratingVariant("Matching")}>Matching</Badge>
                </li>
                <li className="grid grid-cols-[2.25rem_minmax(0,1fr)_auto] items-center gap-3 px-5 py-4">
                  <span className="grid h-9 w-9 place-items-center rounded-full bg-brand-100 text-sm font-semibold text-ink">
                    S
                  </span>
                  <div className="min-w-0">
                    <p className="font-medium text-ink">Sofia R.</p>
                    <p className="truncate text-xs text-muted-foreground">
                      Airflow, Python
                    </p>
                  </div>
                  <Badge variant={ratingVariant("Matching")}>Matching</Badge>
                </li>
                <li className="grid grid-cols-[2.25rem_minmax(0,1fr)_auto] items-center gap-3 px-5 py-4">
                  <span className="grid h-9 w-9 place-items-center rounded-full bg-brand-100 text-sm font-semibold text-ink">
                    T
                  </span>
                  <div className="min-w-0">
                    <p className="font-medium text-ink">Tomas K.</p>
                    <p className="truncate text-xs text-muted-foreground">SQL</p>
                  </div>
                  <Badge variant={ratingVariant("Moderately Matching")}>
                    Moderately Matching
                  </Badge>
                </li>
              </ul>
              <p className="border-t border-border px-5 py-4 text-sm text-muted-foreground">
                Rated in words. Backed by evidence. Never a number.
              </p>
            </aside>
          </div>
        </section>

        <section
          id="brief"
          aria-labelledby="brief-title"
          className="border-y border-border bg-surface/45 px-6 py-20 lg:px-10 lg:py-28"
        >
          <div className="mx-auto max-w-6xl">
            <h2 id="brief-title" className="type-section-title">
              Other tools tell you who ranked higher. You still have to find
              out who they are.
            </h2>
            <div className="mt-10 grid gap-5 md:grid-cols-2">
              <article className="border border-border bg-canvas p-7">
                <p className="type-eyebrow text-muted-foreground">
                  What teams get today
                </p>
                <p className="mt-4 type-prose-lg">
                  A score. A generic test. A pile of CVs and a dashboard. A
                  meeting that starts from zero.
                </p>
              </article>
              <article className="border border-navy-900 bg-navy-900 p-7 text-white">
                <p className="type-eyebrow text-white/70">
                  What you walk in with
                </p>
                <p className="mt-4 max-w-[65ch] text-prose-lg text-white/90">
                  A sentence you can defend. Questions built from this role.
                  One brief per person you chose to meet.
                </p>
              </article>
            </div>
          </div>
        </section>

        <section
          id="audience"
          aria-labelledby="audience-title"
          className="px-6 py-20 lg:px-10 lg:py-28"
        >
          <div className="mx-auto max-w-6xl">
            <p className="type-eyebrow text-teal-700">Audience</p>
            <h2 id="audience-title" className="mt-4 type-section-title">
              Built for the person who has to defend the shortlist.
            </h2>
            <div className="mt-10 grid gap-5 md:grid-cols-3">
              <article className="border border-border bg-surface p-7">
                <h3 className="text-heading">First twenty</h3>
                <p className="mt-3 type-prose-lg">
                  Founders hiring the people who will shape the company.
                </p>
              </article>
              <article className="border border-border bg-surface p-7">
                <h3 className="text-heading">Growth pace</h3>
                <p className="mt-3 type-prose-lg">
                  Teams hiring at volume when the calendar is full and the
                  brief is thin.
                </p>
              </article>
              <article className="border border-border bg-surface p-7">
                <h3 className="text-heading">Enterprise rooms</h3>
                <p className="mt-3 type-prose-lg">
                  Leaders who need evidence that stands with a hiring manager
                  and with legal.
                </p>
              </article>
            </div>
          </div>
        </section>

        <section
          id="process"
          aria-labelledby="process-title"
          className="border-y border-border bg-surface/45 px-6 py-20 lg:px-10 lg:py-28"
        >
          <div className="mx-auto max-w-6xl">
            <p className="type-eyebrow text-teal-700">One role</p>
            <h2 id="process-title" className="mt-4 type-section-title">
              Tuesday&apos;s job. Thursday&apos;s brief.
            </h2>
            <ol className="mt-10 grid gap-5 md:grid-cols-3">
              <li className="border border-border bg-canvas p-7">
                <p className="type-eyebrow text-muted-foreground">First</p>
                <h3 className="mt-3 text-heading">Lock the role</h3>
                <p className="mt-3 type-prose-lg">
                  Draft the job. Name the skills. Publish one link. After the
                  first application, the skills do not drift.
                </p>
              </li>
              <li className="border border-border bg-canvas p-7">
                <p className="type-eyebrow text-muted-foreground">Then</p>
                <h3 className="mt-3 text-heading">See the field</h3>
                <p className="mt-3 type-prose-lg">
                  Every resume is read against those skills. Evidence and a
                  word. You choose who to invite.
                </p>
              </li>
              <li className="border border-border bg-canvas p-7">
                <p className="type-eyebrow text-muted-foreground">Finally</p>
                <h3 className="mt-3 text-heading">Walk in briefed</h3>
                <p className="mt-3 type-prose-lg">
                  One session. One page. Interview time spent only on what is
                  still uncertain.
                </p>
              </li>
            </ol>
            <p className="mx-auto mt-12 max-w-3xl text-center type-prose-lg">
              Candidate data stays in your workspace. Every action leaves a
              trail. A person decides, never a flag on its own.
            </p>
          </div>
        </section>

        <section className="px-6 py-20 lg:px-10 lg:py-28">
          <div className="mx-auto grid max-w-6xl gap-10 lg:grid-cols-[minmax(0,1fr)_20rem] lg:items-end">
            <blockquote>
              <p className="max-w-[24ch] font-editorial text-section-sm leading-tight sm:text-section-md">
                Twenty-five years in HR. One conviction: technology should
                give the team time back.
              </p>
              <footer className="mt-5 text-sm text-muted-foreground">
                Manjunath Sastry Madugula, Founder
              </footer>
            </blockquote>
            <p className="type-prose-lg">
              The previous company was successfully exited. Vivekium is the
              rebuild.
            </p>
          </div>
        </section>

        <section className="bg-navy-900 px-6 py-20 text-center text-white lg:px-10 lg:py-24">
          <div className="mx-auto flex max-w-3xl flex-col items-center">
            <h2 className="type-section-title text-white">
              Run one role. Read the briefs.
            </h2>
            <p className="mt-5 type-lead text-white/80">
              Thirty minutes. A job you are already hiring for. You leave
              knowing if this belongs in your stack.
            </p>
            <Button asChild size="lg" variant="outline" className="mt-8 border-white bg-white text-black hover:border-white hover:bg-white/90 hover:text-black">
              <a href={REQUEST_ACCESS_HREF}>Request demo</a>
            </Button>
          </div>
        </section>

        <section
          id="start"
          aria-labelledby="start-title"
          className="border-t border-border bg-surface/45 px-6 py-20 lg:px-10 lg:py-28"
        >
          <div className="mx-auto grid max-w-6xl gap-12 xl:grid-cols-[18rem_1fr]">
            <div>
              <p className="type-eyebrow text-teal-700">Start</p>
              <h2 id="start-title" className="mt-4 type-section-title">
                Open a role.
              </h2>
              <p className="mt-5 type-prose-lg">
                Start with a paid 30-day Starter pilot. Keep the monthly plan
                or cancel anytime.
              </p>
              <Button asChild size="lg" className="mt-8">
                <Link href="/company/register">Open a role</Link>
              </Button>
            </div>
            <PricingCatalogue />
          </div>
        </section>
      </main>

      <SiteFooter />
    </div>
  );
}
