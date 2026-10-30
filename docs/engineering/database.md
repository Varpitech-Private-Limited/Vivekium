# Database rules

**Status:** current. **Reviewed:** 2026-10-14. Loaded on demand by `.claude/rules/database.md`.
**Scope:** `backend/app/models/`, `backend/alembic/versions/` (head `0137_public_demo`; linear chain, `alembic heads` is one; `0127` was never used), RLS, grants.

## Tenant isolation

- Every tenant-scoped query goes through the RLS-aware session. A hand-written `WHERE tenant_id = ...` is defence in depth, never the only protection. A cross-tenant read answers 404.
- Application role: `pickready_app` (own credential, NOINHERIT; only the migration job holds the owner role through `POSTGRES_MIGRATION_ROLE`). The app cannot run DDL and cannot UPDATE or DELETE `audit_log`.
- Workers: `tenant_worker_session(tenant_id)` for `rls="tenant"` tasks; `resolve_tenant_id(kind, id)` is the one bypass read of a tenant. Every cache key contains the tenant id (`test_cache_tenant_keying.py`).
- `set_config('app.bypass_rls','on',true)` is transaction-local. Probing with it outside a transaction reads zero rows and looks like an empty database. Pass `false` for a session-level setting.
- FORCE RLS binds the table owner too: a count outside the bypass scope reads zero over a full table. Prove a guard can see rows before trusting its count.

## Migrations

- Every schema change is an Alembic migration, checked in. No manual production edits. A new table exports from `models/__init__.py` and carries its RLS policy and grant. 0014's default privileges grant UPDATE on every new table to the app role: omitting UPDATE from a GRANT omits nothing; REVOKE it.
- A live-data migration converts in place; never delete and re-insert rows that other rows cascade from.
- Never an irreversible drop of a table that may hold customer rows. A table proven empty may be dropped by a migration that counts first and raises. See [../operations/LEGACY_TABLES.md](../operations/LEGACY_TABLES.md) for what was dropped and what is kept as history.
- A migration is a dated record. Do not edit an applied one.

## Design rules that have cost defects

- **Derive, never store**, what depends on facts written at different times: posting status, profile age, BGV status, the rank key, who is "new". A stored copy needs a re-derivation nobody remembers.
- **Insert-only where history matters.** `functional_skills_reports`, `report_dimensions`, `job_skill_snapshots`, `coding_question_keys`, leadership versions, `candidate_consent_events`, `bgv_contact_corrections` and `review_dispositions` refuse UPDATE (revoked grant plus trigger where noted). A foreign key onto an insert-only table is an UPDATE waiting for the day the parent is deleted: the trigger must name the referential action (0131 admits exactly one nested `ON DELETE SET NULL` of `locked_by_link_id`).
- **Soft delete under a UNIQUE constraint is a 500 with a date on it.** Revive the soft-deleted row; never re-insert. Adding a name already present is idempotent; a rename onto an occupied name is a 409 naming it. Move checks the target bucket first.
- **One row of any kind proves a generator landed.** "Emptied by a human" is not "never generated": ask the table without the `is_active` filter.
- A CHECK constraint must match its one writer's vocabulary (the `evaluations.confidence` CHECK once refused `moderate`). A column whose default lands at INSERT is `None` before flush.
- Consent history never lives in a table that survives erasure; `audit_log.candidate_id` has no foreign key on purpose and survives `cascade_erasure`.
- A table read by no code and written by no code is history. Say so in [../operations/LEGACY_TABLES.md](../operations/LEGACY_TABLES.md); do not leave it ambiguous.

## Verifying database changes

- Assert committed state from a second connection after the response; an answer of 200 can still roll back at commit.
- Real-Postgres tests for every `rls="tenant"` task and every policy. `scripts/test.sh` drops and recreates `readypick_test`: never run two suites against one database.
