import type { Metadata } from "next";
import type { LucideIcon } from "lucide-react";
import {
  Activity,
  ArrowRight,
  BadgeCheck,
  Binary,
  Bot,
  Boxes,
  BriefcaseBusiness,
  Building2,
  CheckCircle2,
  ChevronRight,
  CircleDollarSign,
  ClipboardCheck,
  Cloud,
  Code2,
  Database,
  FileCheck2,
  FileText,
  Fingerprint,
  Gauge,
  GitBranch,
  Globe2,
  HardDrive,
  KeyRound,
  Layers3,
  LockKeyhole,
  Network,
  Rocket,
  Search,
  ServerCog,
  ShieldCheck,
  Sparkles,
  Target,
  TimerReset,
  UsersRound,
  Workflow,
} from "lucide-react";

import { FadeIn, Stagger, StaggerItem } from "@/components/motion";
import { publicPageMetadata } from "@/lib/site";

export const metadata: Metadata = publicPageMetadata({
  path: "/docs",
  title: "Docs",
  description:
    "Implementation-aligned product and technical documentation for Vivekium.",
});

const CONTENTS = [
  { href: "#product", label: "Product overview" },
  { href: "#workspaces", label: "Four workspaces" },
  { href: "#hiring-flow", label: "Hiring workflow" },
  { href: "#assessment", label: "Assessment & report" },
  { href: "#value", label: "Measurable value" },
  { href: "#technical", label: "Technical overview" },
  { href: "#architecture", label: "Architecture" },
  { href: "#ai", label: "AI routing" },
  { href: "#security", label: "Security" },
  { href: "#production", label: "Production path" },
  { href: "#limitations", label: "Limitations" },
] as const;

const WORKSPACES: Array<{
  icon: LucideIcon;
  label: string;
  title: string;
  body: string;
  items: string[];
}> = [
  {
    icon: Building2,
    label: "Provider",
    title: "Operate the customer portfolio",
    body: "A cross-tenant, audited owner workspace for customer health, provisioning and portfolio billing.",
    items: [
      "Customer analytics and archive lifecycle",
      "BD account provisioning",
      "Read-only compliance and billing visibility",
    ],
  },
  {
    icon: BriefcaseBusiness,
    label: "Company",
    title: "Run hiring from one workspace",
    body: "Company administrators and hiring teams move from a structured job brief to an auditable hiring decision.",
    items: [
      "Jobs, matching and candidate review",
      "Staff, capabilities and compliance",
      "Assessments, reports, pipeline and billing",
    ],
  },
  {
    icon: UsersRound,
    label: "Candidate",
    title: "Apply with a reusable profile",
    body: "Candidates keep one main resume and structured profile while every application preserves its own snapshot.",
    items: [
      "Public job applications",
      "Five-day edit grace for existing applicants",
      "Invitation-only assessments",
    ],
  },
  {
    icon: Target,
    label: "Business development",
    title: "Turn outreach into customers",
    body: "Personal, social and AI-assisted reach share one progression model and one prospect conversion path.",
    items: [
      "Six tracked lead milestones",
      "Internal and internet AI Reach",
      "Signed agreement to reusable prospect tenant",
    ],
  },
];

const FLOW = [
  {
    icon: FileText,
    step: "01",
    title: "Draft the job",
    body: "Structured role inputs become a seven-section Markdown JD. The recruiter edits before publishing.",
  },
  {
    icon: Globe2,
    step: "02",
    title: "Open the window",
    body: "Publication creates a shareable link with 30 active days and a five-day applicant edit grace.",
  },
  {
    icon: Search,
    step: "03",
    title: "Build the candidate set",
    body: "Public applicants, sourced profiles and databank uploads enter the same job-scoped review flow.",
  },
  {
    icon: Binary,
    step: "04",
    title: "Match with evidence",
    body: "Vector and keyword retrieval feed an LLM reranker across four matching parameters, each judged on its own terms with no weighting between them.",
  },
  {
    icon: ClipboardCheck,
    step: "05",
    title: "Invite selectively",
    body: "Recruiters choose candidates. Applying alone never grants assessment access or spends a full credit.",
  },
  {
    icon: BadgeCheck,
    step: "06",
    title: "Decide with a report",
    body: "Technical and Tatva Assessment evidence converge, alongside the application's own validation fields, into an immutable, qualitative PRISM Report.",
  },
];

const PRODUCT_METRICS = [
  { value: "30 + 5", label: "active posting + grace days" },
  { value: "22–45", label: "questions, based on job grade" },
  { value: "3", label: "Tatva Assessment aspects: Must-have, Nice-to-have, Behavioural" },
  { value: "25", label: "resumes per databank batch" },
  { value: "200", label: "candidates per invitation batch" },
  { value: "5 min", label: "dashboard refresh cadence" },
] as const;

const STACK = [
  {
    icon: Code2,
    title: "Experience",
    detail: "Next.js 16.2.12 · React 18.3 · TypeScript · Tailwind · Radix",
  },
  {
    icon: ServerCog,
    title: "Application",
    detail: "Python 3.12 · FastAPI · SQLAlchemy async · Pydantic · Gunicorn",
  },
  {
    icon: Database,
    title: "Data",
    detail: "PostgreSQL 16 · pgvector · Alembic · Redis 7",
  },
  {
    icon: Workflow,
    title: "Asynchronous work",
    detail: "On-demand compute · managed schedules · durable state · idempotent retries",
  },
  {
    icon: Bot,
    title: "AI and research",
    detail: "gpt-5.6-terra · gpt-5.6-luna · voyage-4 embeddings · LangGraph retry state machine · Tavily",
  },
  {
    icon: Boxes,
    title: "Integrations",
    detail: "Firebase · Razorpay · Gmail SMTP · private S3",
  },
] as const;

const AI_ROUTES = [
  ["Candidate conversation", "gpt-5.6-terra", "Dialogue quality is a product bar"],
  ["Job descriptions", "gpt-5.6-terra", "A document a person will publish"],
  ["Competency transformation", "gpt-5.6-terra", "Judgment-heavy role analysis"],
  ["Dimension evaluation", "gpt-5.6-terra", "This is the grade"],
  ["Report synthesis", "gpt-5.6-terra", "States the grades a client reads"],
  ["Project evidence", "gpt-5.6-terra", "Weighs claims against evidence"],
  ["Claim extraction", "gpt-5.6-luna", "Mechanical, and must not evaluate"],
  ["Candidate reranking", "gpt-5.6-luna", "Orders a list it does not grade"],
  ["Resume extraction", "gpt-5.6-luna", "Narrow, high volume"],
  ["Every embedding", "voyage-4", "One vector space, 1024 dimensions"],
] as const;

const LIMITATIONS = [
  {
    level: "Critical",
    title: "Secret hygiene",
    body: "Plaintext local secret material exists. Rotate credentials, remove files and history exposure, and move runtime secrets to AWS Secrets Manager.",
  },
  {
    level: "High",
    title: "Unexecuted deployment",
    body: "Terraform and the release pipeline are complete and plan cleanly offline, but no apply has run against a real AWS account. Creatability, quotas and IAM behaviour remain unproven.",
  },
  {
    level: "Medium",
    title: "Upload scanning",
    body: "Private documents live in a content-addressed S3 bucket behind authenticated routes. Uploaded bytes are never executed, but malware scanning on upload is not yet deployed.",
  },
  {
    level: "High",
    title: "Production observability",
    body: "Structured logs exist, but no formal SLO, APM, synthetic monitoring or business-integrity alert suite is implemented.",
  },
  {
    level: "Medium",
    title: "Operational completeness",
    body: "Inbound verification email, interview feedback, browser E2E coverage and standardized pagination need further work.",
  },
  {
    level: "Medium",
    title: "Legacy surface",
    body: "Dormant authentication, approval and question-bank paths remain for compatibility and should be retired after migration review.",
  },
] as const;

function SectionIntro({
  eyebrow,
  title,
  body,
}: {
  eyebrow: string;
  title: string;
  body: string;
}) {
  return (
    <FadeIn className="max-w-3xl">
      <p className="type-eyebrow text-brand-600">
        {eyebrow}
      </p>
      <h2 className="mt-3 type-section-title">
        {title}
      </h2>
      <p className="mt-5 type-lead">
        {body}
      </p>
    </FadeIn>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex rounded-full border border-brand-500/30 bg-brand-100 px-3 py-1 type-eyebrow text-brand-700">
      {children}
    </span>
  );
}

export default function DocsPage() {
  return (
    <main id="main">
      <section className="relative overflow-hidden border-b border-border">
        <div
          aria-hidden="true"
          className="absolute -left-36 top-8 h-80 w-80 rounded-full bg-brand-600/20 blur-[110px]"
        />
        <div
          aria-hidden="true"
          className="absolute -right-40 bottom-0 h-96 w-96 rounded-full bg-teal-400/15 blur-[130px]"
        />
        <div className="relative mx-auto grid max-w-6xl gap-12 px-6 py-20 lg:grid-cols-[1.08fr_.92fr] lg:items-center lg:px-10 lg:py-28">
          <FadeIn>
            <div className="flex flex-wrap items-center gap-3">
              <Label>Documented as built</Label>
            </div>
            <h1 className="mt-7 type-display">
              The product and the system,{" "}
              <span className="text-gradient-brand">documented as built.</span>
            </h1>
            <p className="mt-7 type-lead">
              A code-aligned guide to what Vivekium does, how each workspace
              fits together, and how the platform should mature from its
              current deployment into a production-scale service.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <a
                href="#product"
                className="inline-flex h-11 items-center gap-2 rounded-lg bg-brand-600 px-5 text-sm font-semibold text-white transition-colors hover:bg-brand-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                Read product docs
                <ArrowRight className="h-4 w-4" aria-hidden="true" />
              </a>
              <a
                href="#technical"
                className="inline-flex h-11 items-center gap-2 rounded-lg border border-border bg-surface px-5 text-sm font-semibold transition-colors hover:border-brand-500 hover:text-brand-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                Explore engineering
                <Code2 className="h-4 w-4" aria-hidden="true" />
              </a>
            </div>
          </FadeIn>

          <FadeIn delay={0.08}>
            <div className="relative rounded-3xl border border-white/10 bg-black p-5 text-white shadow-pop sm:p-7">
              <div className="absolute right-5 top-5 flex gap-1.5" aria-hidden="true">
                <span className="h-2.5 w-2.5 rounded-full bg-rose-400" />
                <span className="h-2.5 w-2.5 rounded-full bg-amber-300" />
                <span className="h-2.5 w-2.5 rounded-full bg-emerald-400" />
              </div>
              <p className="type-ref text-teal-400">
                vivekium / system-map
              </p>
              <div className="mt-8 grid gap-3">
                {[
                  ["01", "Job", "Markdown JD + posting lifecycle"],
                  ["02", "Candidate", "Profile snapshot + resume"],
                  ["03", "Evidence", "AI Match + Tatva Assessment"],
                  ["04", "Decision", "Report + pipeline + history"],
                ].map(([number, title, detail], index) => (
                  <div
                    key={number}
                    className="group grid grid-cols-[auto_1fr_auto] items-center gap-4 rounded-2xl border border-white/10 bg-white/[.055] px-4 py-4"
                  >
                    <span className="text-xs font-semibold tabular-nums text-teal-400">
                      {number}
                    </span>
                    <div>
                      <p className="font-semibold">{title}</p>
                      <p className="mt-1 text-xs text-white/65">
                        {detail}
                      </p>
                    </div>
                    {index < 3 ? (
                      <ChevronRight
                        className="h-4 w-4 text-white/35"
                        aria-hidden="true"
                      />
                    ) : (
                      <CheckCircle2
                        className="h-4 w-4 text-emerald-300"
                        aria-hidden="true"
                      />
                    )}
                  </div>
                ))}
              </div>
              <div className="mt-5 flex items-center justify-between border-t border-white/10 pt-5 text-xs text-white/55">
                <span>Four workspaces</span>
                <span>One evidence chain</span>
              </div>
            </div>
          </FadeIn>
        </div>
      </section>

      <div className="mx-auto grid max-w-6xl gap-12 px-6 py-16 lg:grid-cols-[220px_minmax(0,1fr)] lg:px-10 lg:py-24">
        <aside className="hidden lg:block">
          <nav
            aria-label="Documentation contents"
            className="sticky top-24 rounded-2xl border border-border bg-surface p-4 shadow-card"
          >
            <p className="px-2 type-eyebrow">
              On this page
            </p>
            <ul className="mt-3 space-y-1">
              {CONTENTS.map((item) => (
                <li key={item.href}>
                  <a
                    href={item.href}
                    className="block rounded-lg px-2 py-2 text-sm transition-colors hover:bg-brand-100 hover:text-brand-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {item.label}
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        </aside>

        <div className="min-w-0">
          <section id="product" className="scroll-mt-24">
            <SectionIntro
              eyebrow="Product documentation"
              title="Hiring operations built around evidence, not disconnected tools"
              body="Vivekium connects the work before and after a candidate appears: drafting the job, building the candidate set, comparing fit, inviting assessment, structuring evidence and carrying the decision through to offer and join."
            />

            <div className="mt-10 rounded-3xl border border-brand-500/25 bg-gradient-to-br from-brand-100 via-surface to-surface p-7 sm:p-9">
              <div className="grid gap-7 md:grid-cols-[auto_1fr] md:items-start">
                <div className="grid h-14 w-14 place-items-center rounded-2xl bg-brand-600 text-white shadow-card">
                  <Sparkles className="h-7 w-7" aria-hidden="true" />
                </div>
                <div>
                  <h3 className="text-2xl font-semibold">What makes it distinct</h3>
                  <p className="mt-3 leading-7">
                    The platform combines hybrid retrieval, grade-aware
                    assessment, behavioural evidence, qualitative reporting,
                    transparent fractional credits, reusable profile snapshots
                    and auditable workflow history. AI accelerates the work;
                    deterministic rules preserve the operation.
                  </p>
                  <div className="mt-6 grid gap-3 sm:grid-cols-2">
                    {[
                      "No assessment numbers shown to users",
                      "Every application keeps its own snapshot",
                      "Assessment spend begins with recruiter intent",
                      "Jobs retain candidates across renewal windows",
                    ].map((item) => (
                      <div key={item} className="flex gap-3 text-sm">
                        <CheckCircle2
                          className="mt-0.5 h-5 w-5 shrink-0 text-brand-600"
                          aria-hidden="true"
                        />
                        <span>{item}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section id="workspaces" className="scroll-mt-24 pt-24">
            <SectionIntro
              eyebrow="One platform, four views"
              title="Each workspace sees the part of the system it owns"
              body="Access is resolved from identity, workspace context, tenant, role defaults and individual capability overrides. The interfaces stay focused while the records remain connected."
            />
            <Stagger className="mt-10 grid gap-5 md:grid-cols-2">
              {WORKSPACES.map((workspace) => (
                <StaggerItem key={workspace.label}>
                  <article className="h-full rounded-2xl border border-border bg-surface p-6 shadow-card">
                    <div className="flex items-center justify-between">
                      <div className="grid h-11 w-11 place-items-center rounded-xl bg-brand-100 text-brand-700">
                        <workspace.icon className="h-5 w-5" aria-hidden="true" />
                      </div>
                      <Label>{workspace.label}</Label>
                    </div>
                    <h3 className="mt-6 text-xl font-semibold">{workspace.title}</h3>
                    <p className="mt-3 text-sm">{workspace.body}</p>
                    <ul className="mt-5 space-y-2 border-t border-border pt-5">
                      {workspace.items.map((item) => (
                        <li key={item} className="flex gap-2 text-sm">
                          <ChevronRight
                            className="mt-1 h-4 w-4 shrink-0 text-brand-600"
                            aria-hidden="true"
                          />
                          {item}
                        </li>
                      ))}
                    </ul>
                  </article>
                </StaggerItem>
              ))}
            </Stagger>
          </section>

          <section id="hiring-flow" className="scroll-mt-24 pt-24">
            <SectionIntro
              eyebrow="Implemented hiring flow"
              title="A traceable path from role definition to joined outcome"
              body="The current interface starts with an editable AI draft, publishes a time-bound job, unifies candidate sources, and makes assessment an explicit recruiter choice."
            />
            <div className="relative mt-10">
              <div
                aria-hidden="true"
                className="absolute bottom-8 left-[27px] top-8 w-px bg-gradient-to-b from-brand-600 via-teal-400 to-emerald-400 md:left-1/2"
              />
              <div className="space-y-5">
                {FLOW.map((item, index) => (
                  <FadeIn
                    key={item.step}
                    delay={index * 0.035}
                    className={`relative grid gap-5 md:grid-cols-2 ${
                      index % 2 ? "md:[&>article]:col-start-2" : ""
                    }`}
                  >
                    <article className="relative ml-14 rounded-2xl border border-border bg-surface p-6 shadow-card md:ml-0">
                      <div
                        className={`absolute top-7 grid h-10 w-10 place-items-center rounded-full bg-brand-600 text-white ring-8 ring-canvas ${
                          index % 2
                            ? "-left-[calc(50%+1.25rem)]"
                            : "left-[-3.35rem] md:-right-[calc(50%+1.25rem)] md:left-auto"
                        }`}
                      >
                        <item.icon className="h-4 w-4" aria-hidden="true" />
                      </div>
                      <p className="text-xs font-semibold tabular-nums text-brand-600">
                        {item.step}
                      </p>
                      <h3 className="mt-2 text-lg font-semibold">{item.title}</h3>
                      <p className="mt-2 text-sm">{item.body}</p>
                    </article>
                  </FadeIn>
                ))}
              </div>
            </div>
          </section>

          <section id="assessment" className="scroll-mt-24 pt-24">
            <SectionIntro
              eyebrow="Assessment and decision support"
              title="The assessment changes with grade; the report stays explainable"
              body="Every assessment blends a grade-sized Tatva question set with the job's technical bank. Validation facts are mandatory fields on the application form, never questions in the conversation."
            />
            <div className="mt-10 overflow-hidden rounded-2xl border border-border bg-surface shadow-card">
              <div className="grid grid-cols-[1.35fr_repeat(3,.8fr)] border-b border-border bg-brand-100 px-5 py-4 text-sm font-semibold">
                <span>Grade</span>
                <span className="text-right">Technical</span>
                <span className="text-right">Tatva</span>
                <span className="text-right">Total</span>
              </div>
              {[
                ["Non-managerial", "20", "25", "45"],
                ["Managerial", "17", "20", "37"],
                ["Leadership", "15", "15", "30"],
                ["CXO", "12", "10", "22"],
              ].map((row) => (
                <div
                  key={row[0]}
                  className="grid grid-cols-[1.35fr_repeat(3,.8fr)] border-b border-border px-5 py-4 text-sm last:border-b-0"
                >
                  <span className="font-semibold">{row[0]}</span>
                  {row.slice(1).map((value, valueIndex) => (
                    <span key={`${row[0]}-${valueIndex}`} className="text-right tabular-nums">
                      {value}
                    </span>
                  ))}
                </div>
              ))}
            </div>

            <div className="mt-6 grid gap-5 md:grid-cols-3">
              {[
                {
                  icon: Bot,
                  title: "Graded skill by skill",
                  body: "Each skill is graded on its own evidence; the application's validation fields flow through unscored. The report is written only once every grade is in.",
                },
                {
                  icon: FileCheck2,
                  title: "Immutable report",
                  body: "An AI Match, an Overall grade, Must-have, Nice-to-have and Behavioural skills, three radar charts and a Gap Analysis & Action Plan become one durable record.",
                },
                {
                  icon: LockKeyhole,
                  title: "One contract per assessment",
                  body: "Every candidate is assessed against the skills locked when their assessment began. Nothing is carried over from another job.",
                },
              ].map((item) => (
                <article
                  key={item.title}
                  className="rounded-2xl border border-border bg-surface p-5"
                >
                  <item.icon className="h-5 w-5 text-brand-600" aria-hidden="true" />
                  <h3 className="mt-2 text-lg font-semibold">{item.title}</h3>
                  <p className="mt-2 text-sm">{item.body}</p>
                </article>
              ))}
            </div>
          </section>

          <section id="value" className="scroll-mt-24 pt-24">
            <SectionIntro
              eyebrow="Measurable value"
              title="Operational facts are separated from unproven marketing claims"
              body="The implementation provides precise limits, timings and unit economics. It does not yet contain production analytics that prove hours saved, cost saved, accuracy or time-to-hire improvement."
            />
            <div className="mt-10 grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {PRODUCT_METRICS.map((metric) => (
                <div
                  key={metric.label}
                  className="rounded-2xl border border-border bg-surface p-5 shadow-card"
                >
                  <p className="text-2xl font-semibold tabular-nums text-brand-600">
                    {metric.value}
                  </p>
                  <p className="mt-2 text-sm">{metric.label}</p>
                </div>
              ))}
            </div>
            <div className="mt-6 rounded-2xl border border-amber-300/50 bg-amber-50 p-6 text-black dark:border-amber-300/20 dark:bg-amber-950 dark:text-white">
              <div className="flex gap-4">
                <Gauge className="mt-0.5 h-6 w-6 shrink-0" aria-hidden="true" />
                <div>
                  <h3 className="font-semibold">Evidence roadmap</h3>
                  <p className="mt-2 text-sm">
                    Production analytics should establish time-to-publish,
                    candidate review minutes, invite completion, report-to-
                    interview conversion, cost per completed assessment,
                    old-profile reuse and provider fallback rates before ROI
                    figures are presented as proven.
                  </p>
                </div>
              </div>
            </div>
          </section>

          <section id="technical" className="scroll-mt-24 pt-28">
            <div className="rounded-3xl bg-black p-7 text-white sm:p-10">
              <p className="type-eyebrow text-teal-400">
                Technical documentation
              </p>
              <h2 className="mt-4 max-w-3xl type-section-title">
                A modular web system with durable workflows and defense in depth
              </h2>
              <p className="mt-5 type-lead text-white/70">
                Next.js serves the public and portal experiences. FastAPI owns
                contracts and authorization. PostgreSQL stores domain state and
                enforces tenant isolation. Long-running work runs on compute
                that starts when the work arrives and stops when it finishes,
                while Redis coordinates shared counters and hot data.
              </p>
              <div className="mt-8 grid gap-3 sm:grid-cols-2">
                {[
                  ["29", "Alembic migrations"],
                  ["4", "authenticated workspaces"],
                  ["3", "current AI providers"],
                  ["2", "isolation layers: app + RLS"],
                ].map(([value, label]) => (
                  <div
                    key={label}
                    className="rounded-xl border border-white/10 bg-white/[.055] p-4"
                  >
                    <p className="text-xl font-semibold tabular-nums text-teal-100">
                      {value}
                    </p>
                    <p className="mt-1 text-xs text-white/60">{label}</p>
                  </div>
                ))}
              </div>
            </div>

            <Stagger className="mt-8 grid gap-4 md:grid-cols-2">
              {STACK.map((item) => (
                <StaggerItem key={item.title}>
                  <article className="flex h-full gap-4 rounded-2xl border border-border bg-surface p-5 shadow-card">
                    <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-brand-100 text-brand-700">
                      <item.icon className="h-5 w-5" aria-hidden="true" />
                    </div>
                    <div>
                      <h3 className="mt-2 text-lg font-semibold">{item.title}</h3>
                      <p className="mt-2 text-sm">{item.detail}</p>
                    </div>
                  </article>
                </StaggerItem>
              ))}
            </Stagger>
          </section>

          <section id="architecture" className="scroll-mt-24 pt-24">
            <SectionIntro
              eyebrow="System architecture"
              title="Request work stays responsive; heavy work becomes durable"
              body="The API writes workflow state before it delegates parsing, matching, scoring, research or email. Retries can resume from the database rather than reconstructing state from memory."
            />
            <div className="mt-10 overflow-hidden rounded-3xl border border-border bg-surface p-5 shadow-card sm:p-8">
              <div className="grid gap-4 md:grid-cols-[1fr_auto_1fr] md:items-center">
                <div className="rounded-2xl border border-brand-500/25 bg-brand-100 p-5">
                  <div className="flex items-center gap-3">
                    <Globe2 className="h-5 w-5 text-brand-700" aria-hidden="true" />
                    <p className="font-bold">Next.js experience</p>
                  </div>
                  <p className="mt-3 text-sm">
                    Public site, Provider, Company, Candidate and BD workspaces.
                  </p>
                </div>
                <div className="hidden items-center gap-2 md:flex" aria-hidden="true">
                  <span className="h-px w-5 bg-brand-500" />
                  <ChevronRight className="h-4 w-4 text-brand-600" />
                  <span className="h-px w-5 bg-brand-500" />
                </div>
                <div className="rounded-2xl border border-border p-5">
                  <div className="flex items-center gap-3">
                    <ServerCog className="h-5 w-5 text-brand-600" aria-hidden="true" />
                    <p className="font-bold">FastAPI application</p>
                  </div>
                  <p className="mt-3 text-sm">
                    Identity, capability checks, domain services and API contracts.
                  </p>
                </div>
              </div>

              <div className="mx-auto my-4 h-8 w-px bg-brand-500" aria-hidden="true" />

              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                {[
                  [Database, "PostgreSQL + pgvector", "State, RLS, vectors"],
                  [Workflow, "On-demand compute", "Dispatch, schedules, cache"],
                  [Bot, "AI providers", "Generate, score, synthesize"],
                  [Boxes, "External services", "Files, pay, mail, SMS"],
                ].map(([Icon, title, body]) => {
                  const TileIcon = Icon as LucideIcon;
                  return (
                    <div
                      key={title as string}
                      className="rounded-2xl border border-border bg-canvas p-4 text-center"
                    >
                      <TileIcon
                        className="mx-auto h-5 w-5 text-brand-600"
                        aria-hidden="true"
                      />
                      <p className="mt-3 text-sm font-bold">{title as string}</p>
                      <p className="mt-1 text-xs">{body as string}</p>
                    </div>
                  );
                })}
              </div>
            </div>

            <div className="mt-6 grid gap-5 md:grid-cols-3">
              {[
                {
                  icon: LockKeyhole,
                  title: "Tenant isolation",
                  body: "Application filtering is reinforced by PostgreSQL row-level policies. Provider bypass is explicit and audited.",
                },
                {
                  icon: GitBranch,
                  title: "Explicit state",
                  body: "Posting, invitation, assessment, pipeline, billing and outreach transitions persist as domain state.",
                },
                {
                  icon: TimerReset,
                  title: "Retry-safe effects",
                  body: "Webhook IDs, ledger keys and workflow guards prevent duplicate charges and completion.",
                },
              ].map((item) => (
                <article
                  key={item.title}
                  className="rounded-2xl border border-border bg-surface p-5"
                >
                  <item.icon className="h-5 w-5 text-brand-600" aria-hidden="true" />
                  <h3 className="mt-2 text-lg font-semibold">{item.title}</h3>
                  <p className="mt-2 text-sm">{item.body}</p>
                </article>
              ))}
            </div>
          </section>

          <section id="ai" className="scroll-mt-24 pt-24">
            <SectionIntro
              eyebrow="AI architecture"
              title="One vendor, two tiers, deterministic continuity"
              body="Every task resolves to exactly one model through a closed mapping. The split is judge-or-write against extract-or-classify, and it is a boundary rather than a preference: extraction must never form an opinion before the evaluators do. Per-task timeouts, a total wall-clock budget, bounded retries and a circuit breaker bound every call, and each generative path has a deterministic fallback so an outage costs quality and never workflow state."
            />
            <div className="mt-10 overflow-x-auto rounded-2xl border border-border bg-surface shadow-card">
              <table className="w-full min-w-[640px] text-left text-sm">
                <thead className="bg-brand-100">
                  <tr>
                    <th className="px-5 py-4 font-semibold">Task</th>
                    <th className="px-5 py-4 font-semibold">Model</th>
                    <th className="px-5 py-4 font-semibold">Why</th>
                  </tr>
                </thead>
                <tbody>
                  {AI_ROUTES.map((row) => (
                    <tr key={row[0]} className="border-t border-border">
                      {row.map((cell, index) => (
                        <td
                          key={cell}
                          className={`px-5 py-4 ${
                            index === 0
                              ? "font-semibold"
                              : index === 1
                                ? "type-ref"
                                : ""
                          }`}
                        >
                          {cell}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-6 rounded-2xl border border-brand-500/30 bg-brand-100 p-6">
              <div className="flex gap-4">
                <Sparkles className="mt-0.5 h-6 w-6 shrink-0 text-brand-700" aria-hidden="true" />
                <div>
                  <h3 className="font-semibold">Production provider direction</h3>
                  <p className="mt-2 text-sm">
                    Replace free-tier key rotation with a direct enterprise
                    primary vendor under contract, retain a
                    tested direct secondary, pin model versions, add quality
                    evaluations and accept provider data terms before processing
                    candidate PII.
                  </p>
                </div>
              </div>
            </div>
          </section>

          <section id="security" className="scroll-mt-24 pt-24">
            <SectionIntro
              eyebrow="Security and privacy"
              title="Identity, authorization and database policy protect the same boundary"
              body="Firebase proves identity; application sessions select the workspace; capability checks constrain actions; PostgreSQL RLS limits tenant rows. Sensitive documents remain behind authenticated routes."
            />
            <div className="mt-10 grid gap-4 sm:grid-cols-2">
              {[
                [Fingerprint, "Firebase identity", "Google, email/password and phone sign-in"],
                [KeyRound, "Secure sessions", "HTTP-only access, refresh and context cookies"],
                [ShieldCheck, "Capability overlays", "Role defaults with individual grants and revocations"],
                [Database, "Row-level security", "Tenant policies with narrow, audited provider bypass"],
                [CircleDollarSign, "Payment integrity", "Signed Razorpay webhooks and deduplicated events"],
                [HardDrive, "Private files", "Authenticated preview/download and sanitized DOCX HTML"],
              ].map(([Icon, title, body]) => {
                const TileIcon = Icon as LucideIcon;
                return (
                  <article
                    key={title as string}
                    className="flex gap-4 rounded-2xl border border-border bg-surface p-5"
                  >
                    <TileIcon
                      className="mt-0.5 h-5 w-5 shrink-0 text-brand-600"
                      aria-hidden="true"
                    />
                    <div>
                      <h3 className="font-semibold">{title as string}</h3>
                      <p className="mt-2 text-sm">{body as string}</p>
                    </div>
                  </article>
                );
              })}
            </div>
          </section>

          <section id="production" className="scroll-mt-24 pt-24">
            <SectionIntro
              eyebrow="Infrastructure and scale"
              title="Move from credit-funded deployment to a reproducible cloud platform"
              body="The repository is container-ready, but the exact current topology was manual and external: there was no checked-in infrastructure as code or CI/CD pipeline."
            />

            <div className="mt-10 rounded-3xl border border-border bg-black p-6 text-white shadow-pop sm:p-8">
              <div className="grid gap-4 lg:grid-cols-[.9fr_1.2fr_.9fr] lg:items-center">
                <div className="space-y-3">
                  {[
                    [Network, "Load balancer · CDN · WAF"],
                    [Globe2, "Next.js service"],
                    [ServerCog, "ECS Fargate API"],
                  ].map(([Icon, text]) => {
                    const TileIcon = Icon as LucideIcon;
                    return (
                      <div
                        key={text as string}
                        className="flex items-center gap-3 rounded-xl border border-white/10 bg-white/[.055] p-4"
                      >
                        <TileIcon className="h-5 w-5 text-teal-400" aria-hidden="true" />
                        <span className="text-sm font-semibold">{text as string}</span>
                      </div>
                    );
                  })}
                </div>
                <div className="rounded-2xl border border-teal-400/25 bg-teal-400/10 p-5">
                  <p className="type-eyebrow text-teal-100">
                    Managed core
                  </p>
                  <div className="mt-4 grid gap-3 sm:grid-cols-2">
                    {[
                      "RDS PostgreSQL HA + pgvector",
                      "ElastiCache Redis",
                      "Private Cloud Storage",
                      "Secret Manager",
                      "Artifact Registry",
                      "Logging · Monitoring · Trace",
                    ].map((item) => (
                      <div
                        key={item}
                        className="rounded-lg border border-white/10 bg-black/15 p-3 text-xs"
                      >
                        {item}
                      </div>
                    ))}
                  </div>
                </div>
                <div className="space-y-3">
                  {[
                    [Workflow, "Worker pool or GKE workers"],
                    [TimerReset, "Cloud Scheduler"],
                    [Rocket, "Progressive CI/CD"],
                  ].map(([Icon, text]) => {
                    const TileIcon = Icon as LucideIcon;
                    return (
                      <div
                        key={text as string}
                        className="flex items-center gap-3 rounded-xl border border-white/10 bg-white/[.055] p-4"
                      >
                        <TileIcon className="h-5 w-5 text-teal-400" aria-hidden="true" />
                        <span className="text-sm font-semibold">{text as string}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>

            <div className="mt-8 grid gap-5 md:grid-cols-3">
              {[
                {
                  icon: Cloud,
                  stage: "01",
                  title: "Harden",
                  body: "AWS Secrets Manager, private S3, RDS HA, ElastiCache, backups, SLOs and alerting.",
                },
                {
                  icon: Layers3,
                  stage: "02",
                  title: "Separate workloads",
                  body: "Independent queues and autoscaling for resume, matching, assessment, email and research.",
                },
                {
                  icon: Activity,
                  stage: "03",
                  title: "Build resilience",
                  body: "Progressive releases, recovery drills, enterprise AI capacity and regional data controls.",
                },
              ].map((item) => (
                <article
                  key={item.stage}
                  className="rounded-2xl border border-border bg-surface p-6 shadow-card"
                >
                  <div className="flex items-center justify-between">
                    <item.icon className="h-6 w-6 text-brand-600" aria-hidden="true" />
                    <span className="text-xs font-semibold tabular-nums text-brand-600">
                      {item.stage}
                    </span>
                  </div>
                  <h3 className="mt-2 text-lg font-semibold">{item.title}</h3>
                  <p className="mt-2 text-sm">{item.body}</p>
                </article>
              ))}
            </div>
          </section>

          <section id="limitations" className="scroll-mt-24 pt-24">
            <SectionIntro
              eyebrow="Current limitations"
              title="The next engineering work is clear and prioritized"
              body="These constraints are visible in the current repository. They are documented so a new engineering team can distinguish a deliberate design from unfinished production hardening."
            />
            <div className="mt-10 space-y-4">
              {LIMITATIONS.map((item) => (
                <article
                  key={item.title}
                  className="grid gap-4 rounded-2xl border border-border bg-surface p-5 shadow-card sm:grid-cols-[100px_1fr] sm:items-start"
                >
                  <span
                    className={`inline-flex w-fit rounded-full px-3 py-1 text-xs font-semibold ${
                      item.level === "Critical"
                        ? "bg-rose-100 text-rose-900 dark:bg-rose-950 dark:text-rose-100"
                        : item.level === "High"
                          ? "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-100"
                          : "bg-brand-100 text-brand-700"
                    }`}
                  >
                    {item.level}
                  </span>
                  <div>
                    <h3 className="mt-2 text-lg font-semibold">{item.title}</h3>
                    <p className="mt-2 text-sm">{item.body}</p>
                  </div>
                </article>
              ))}
            </div>
          </section>

          <section className="pt-24">
            <div className="overflow-hidden rounded-3xl bg-brand-600 p-8 text-white shadow-pop sm:p-10">
              <div className="grid gap-8 md:grid-cols-[1fr_auto] md:items-end">
                <div>
                  <p className="type-eyebrow text-teal-100">
                    Documentation contract
                  </p>
                  <h2 className="mt-4 max-w-2xl type-section-title">
                    The codebase remains the source of truth.
                  </h2>
                  <p className="mt-4 type-lead text-white/80">
                    Product claims, infrastructure diagrams and workflow rules
                    should change with implementation evidence. Historical
                    proposals must not silently return as current behavior.
                  </p>
                </div>
                <a
                  href="/login?initial_context=all"
                  className="inline-flex h-11 items-center justify-center gap-2 rounded-lg bg-white px-5 text-sm font-semibold text-black transition-transform hover:-translate-y-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white"
                >
                  Open Vivekium
                  <ArrowRight className="h-4 w-4" aria-hidden="true" />
                </a>
              </div>
            </div>
          </section>
        </div>
      </div>
    </main>
  );
}
