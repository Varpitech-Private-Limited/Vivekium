---
paths:
  - "backend/alembic/**"
  - "backend/app/models/**"
---
# Database (digest; full rules in docs/engineering/database.md)

- Head is `0137_public_demo`; one linear chain. `down_revision` names a revision id, never a filename. Never edit an applied migration.
- Every tenant table has an RLS policy and a grant; revoke UPDATE on an insert-only table. A foreign key onto an insert-only table needs the trigger to name the referential action.
- Convert live data in place; never delete and re-insert rows others cascade from. Soft delete under a UNIQUE: revive, never re-insert.
- Never an irreversible drop of a table that may hold customer rows; a guarded drop counts first and raises.
- Derive, do not store, anything built from facts written at different times.
- Prove SQL against real Postgres; assert committed state from a second connection.
