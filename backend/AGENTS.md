# backend/AGENTS.md

Scope: `backend/` (FastAPI, SQLAlchemy async, Alembic, tests, harness). Root [AGENTS.md](../AGENTS.md) applies first; this adds the domain pointers.

- Rules: [backend](../docs/engineering/backend.md), [database](../docs/engineering/database.md), [security](../docs/engineering/security.md), [AI runtime](../docs/engineering/ai-runtime.md), [testing](../docs/engineering/testing.md).
- Run: `./scripts/test.sh` from the repository root (Docker; fresh database). One module while developing. Never two suites on one database.
- Migration head `0137_public_demo`; never edit an applied migration.
- `backend/harness/` is never imported by `backend/app/`.
- Decided but unbuilt: approval flow, SES-only email, retrieval injection screen, static-checker coding (see [decisions register](../docs/decisions/REGISTER.md)). Do not describe them as working.
