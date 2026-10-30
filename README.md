# Vivekium

Multi-tenant recruitment and hiring-intelligence platform for Varpitech LLP. Candidates apply, are ranked against the role, sit one proctored, server-timed AI assessment, and come out the other side as a **PRISM Report** a recruiter can act on, graded in words and never in numbers.

Next.js 16 frontend, FastAPI backend, Firebase identity, PostgreSQL with pgvector under row-level security, AWS Lambda and on-demand Fargate for every slow path, fully containerised, deployed to AWS.

| | |
|---|---|
| **All documentation** | [docs/README.md](docs/README.md), the index |
| What the product does | [docs/product/PRD.md](docs/product/PRD.md) |
| How it is built | [docs/architecture/ESD.md](docs/architecture/ESD.md) |
| Rules a change must not break (humans and agents) | [CLAUDE.md](CLAUDE.md), [AGENTS.md](AGENTS.md), [docs/engineering/](docs/engineering/) |
| Decisions, owed changes, open questions | [docs/decisions/](docs/decisions/) |
| Run it, test it | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Deploy it | [docs/operations/DEPLOY_AWS.md](docs/operations/DEPLOY_AWS.md) |
| Design system | [DESIGN.md](DESIGN.md), [PRODUCT.md](PRODUCT.md) |

---

## What it does

**Four portals, one platform.** The Provider Portal (`/admin`) is the Vivekium owner console. The Customer Portal (`/org`) is a client company workspace. The Candidate Portal (`/portal`) is where applicants live. The Business Development Portal (`/bd`) is where the Vivekium sales team works leads.

**The hiring flow.** A customer completes its Company Profile and saves a job as a draft with one Markdown JD. Sutra drafts the job's **Skills** (Must-have, Nice-to-have, Behavioural, at most five each) from the JD; the team edits and saves them; the Final Job Posting is previewed and the job is published. The first genuine application freezes the JD and Skills into the snapshot every candidate is assessed against. The job SWOT is internal hiring intelligence. Yukti reads each resume against the saved Skills and ranks every applicant; the recruiter selects who is invited to the assessment. (An HR approval step before publish is decided and not yet built: [docs/decisions/REGISTER.md](docs/decisions/REGISTER.md).)

**The assessment.** One proctored conversation per candidate: one question per skill (never fewer than the grade's floor), mostly prose, with multiple choice, fill-in-the-blank and, for coding roles, a code question. The server keeps the time, every item is asked in a fixed order, a spoken answer is transcribed and kept as text only. Miti grades every skill from the answers and is the sole grading authority.

**The output.** A PRISM Report (*Evidence-Based Role Intelligence & Suitability Mapping*) with a fixed section order, three number-free radar charts, and a citation chokepoint that withholds an uncited statement and routes the report to a person. It is insert-only in the database. Grades are four words only: Highly Matching, Matching, Moderately Matching, Not Matching. No number reaches a client, with no exception.

**Project Evidence Intelligence.** Candidates may submit projects (files or a public repository). The platform derives structured evidence and deletes the originals.

---

## Architecture

| Layer | Technology |
|---|---|
| Frontend | Next.js 16, React 18, TypeScript, Tailwind, shadcn/ui |
| Backend | FastAPI, Python 3.12, Pydantic v2, async SQLAlchemy |
| Database | PostgreSQL 16 + pgvector, row-level security per tenant |
| Shared state | Redis 7: rate limiting, auth sessions, the proctoring counter, background run status, caches |
| AI | One vendor, three endpoints: `gpt-5.6-terra` (judge and write), `gpt-5.6-luna` (extract and classify), `voyage-4` (embeddings), routed through `services/llm_router` with per-task timeouts, budgets and a circuit breaker |
| Auth | Firebase Authentication (Google or email and password) plus session cookies backed by a Redis session record |
| Payments | Razorpay: monthly plans and a Starter top-up today (per-credit pricing is decided, not built) |
| Email | Amazon SES or SMTP today, one transport per deployment, with corporate senders per tenant (SES only is decided, not built; `noreply@` for security codes, `contact@` for other platform mail) |
| Object storage | Private S3 bucket, content-addressed, served through authenticated routes |
| Deployment | AWS: ECS Fargate, Lambda, RDS PostgreSQL, ElastiCache, S3, ECR, Secrets Manager, Terraform in [infra/](infra/) |

One backend image runs the API service, the on-demand `agent` and `migrate` tasks, and (as a `-fn` sibling of the same bytes) the task-worker and drafting Lambdas. Background work is dispatched, never Celery. The frontend proxies `/api/*` to the backend through a same-origin route handler, which keeps auth cookies `SameSite=Strict`.

Two rules shape most of the code. **Every tenant-scoped query goes through the RLS-aware session**, because the Postgres policy is the boundary. **Permissions are data, never a role branch**: `require_capability(...)`.

---

## Local development

```bash
cp .env.example .env
docker compose -f infra/docker-compose.yml up --build
docker compose -f infra/docker-compose.yml exec backend alembic upgrade head
```

Frontend on http://localhost:3000, API docs on http://localhost:8000/docs. Detail, including the Firebase web config and seeded accounts: [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/operations/SETUP.md](docs/operations/SETUP.md).

## Tests

```bash
./scripts/test.sh
```

The script brings up `docker-compose.test.yml` (Postgres, Redis and MinIO on non-default ports), recreates the database, flushes the cache, migrates, and runs pytest. The suite runs with no model credentials set, deliberately. Every skip is declared in [docs/operations/SKIPS.md](docs/operations/SKIPS.md) and enforced by a test.

## Deployment

Pilot (`readypick-pilot`, `ap-south-2`) is the live site; `staging` and `production` are defined and never applied. Workloads: an API and a frontend service, an analysis service, on-demand `agent` and `migrate` Fargate tasks, and Lambda functions for short tasks. IAM is per service and enumerated. Procedures and what is and is not verified: [docs/operations/DEPLOY_AWS.md](docs/operations/DEPLOY_AWS.md); what runs where: [docs/operations/INFRA_TOPOLOGY.md](docs/operations/INFRA_TOPOLOGY.md).

## Security

- **Secrets** live in AWS Secrets Manager and are injected at runtime, never baked into an image and never committed.
- **Tenant isolation** is a Postgres row-level-security policy on every tenant-scoped table.
- **Credentials** are Firebase's; the application stores no password. Company registration, password reset and change use a server-side security code. No CAPTCHA challenge is drawn and no proof is checked (A-24). The removal is not deployed yet.
- **Candidate uploads are untrusted input.** Resumes and projects are validated and never executed; archives are inspected before extraction.
- **No number reaches a client.** Scores exist internally and become words server-side.
