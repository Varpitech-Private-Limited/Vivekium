# Public demo mode

**Status:** current. **Reviewed:** 2026-10-30 against the owner statement of that date (register A-23). The behavior below is specified. The flag ships default off. Live activation is not done. This page does not put https://readypick.ai into demo mode. September 2026 account cost was about $116.60 unblended, including tax, measured from Cost Explorer for account 016617990245. October was still estimated and is not a bill.

A-23 amends A-02 and A-05 only while `DEMO_MODE` is on. Register: [../decisions/REGISTER.md](../decisions/REGISTER.md). What runs on AWS: [INFRA_TOPOLOGY.md](INFRA_TOPOLOGY.md). Image rollback: [DEPLOY_AWS.md](DEPLOY_AWS.md) section 7.

`public_demo` is a new column (migration head `0137_public_demo`). It is not `is_demo`. `is_demo` remains the C-07 billing-exemption flag.

## Enable

Public demo mode is off unless an operator turns it on.

| Setting | Role |
|---|---|
| `DEMO_MODE` | Backend gate. Default off. |
| `NEXT_PUBLIC_DEMO_MODE` | Frontend gate. Default off. Next inlines it at image build, so the frontend image must be built with `--build-arg NEXT_PUBLIC_DEMO_MODE=true` (the remote builder's `--public-demo`). A task environment variable does not turn the UI on. An empty value is off. Set it in the same release as `DEMO_MODE`. |
| `DEMO_MODE_CONFIRM` | Required only when `DEMO_MODE` is on and `ENVIRONMENT=production`. The value must be `public-demo`. |

The live pilot sets `ENVIRONMENT=production`, even though the Terraform composition is named pilot. Turning the flag on there without the confirm refuses to boot (below). Other environments do not require the confirm. The flag still defaults off everywhere.

When the flag is off, existing Firebase, CAPTCHA and Razorpay routes stay unchanged. The skips below do not run.

## Confirm gate

On `ENVIRONMENT=production`, `DEMO_MODE` also requires `DEMO_MODE_CONFIRM=public-demo`. A missing confirm refuses to boot. A value other than `public-demo` does not satisfy the gate.

While `DEMO_MODE` is unset or false, production boots without this confirm. Do not treat a running pilot as proof that the confirm is set, and do not treat the confirm as a switch that is already on.

## Who can be created or retrieved

Demo login (`POST /api/v1/auth/demo/session`) accepts any syntactically valid email and any nonempty password. It never binds a user who is not `public_demo`.

| Action | Rule |
|---|---|
| Company login | Retrieves only. Does not create. |
| Company registration (`POST /api/v1/company-onboarding/demo`) | Creates. |
| Candidate login | Creates or retrieves. Must not claim a sourced candidate (`email_verified` false). |
| Invite setup-password, `public_demo` branch | The existing `POST /api/v1/companies/invites/{token}/setup-password` handler (also mounted under `/api/v2`). The `public_demo` branch skips CAPTCHA, Firebase and Razorpay only when the flag is on. |

`GET /api/v1/auth/demo-status` reports the flag. It does not mint a session.

Only these paths skip CAPTCHA, Firebase and Razorpay, and only when the flag is on:

- `POST /api/v1/auth/demo/session`
- `POST /api/v1/company-onboarding/demo`
- `POST /api/v1/billing/demo/purchase`
- the `public_demo` branch of invite setup-password

## Who is refused

- Never the owner. Never `super_admin`. Never a user who is not `public_demo`.
- Never the permanent `is_demo` companies: Sarkar Corp, ACRM Corp, Specter and Co.
- If any user with that email is not `public_demo`, the email is refused with 409 so the frontend can fall through to Firebase. Same-email collision with a real user is 409.
- Demo users have no `firebase_uid`.

## Credits and simulated payments

Public demo companies are not billing-exempt. They get a real ledger grant of `DEMO_INITIAL_CREDITS` (default 25), real deductions and the normal credit gates.

Registration writes a `subscription_charge` of amount 0 and does not call Razorpay. Credit packs call the real `settle_purchase` path with a demo order id and do not call Razorpay. Public demo tenants get 409 on real payment routes.

No Razorpay call is not a fake balance. The ledger rows are real.

## Email allowlist

When demo mode is on, a recipient who is not on `DEMO_EMAIL_ALLOWLIST` and is not a platform sender is withheld. The skip is recorded (message id `demo-withheld`). The workflow does not fail. An empty allowlist withholds everyone except platform addresses.

This withhold runs only while demo mode is on.

## Cleanup sweep

Daily task `pickready.purge_public_demo`. Schedule name `readypick-purge-public-demo`. It deletes only `public_demo` rows older than `DEMO_RETENTION_DAYS` (default 14). It never deletes `is_demo`-only tenants.

The task belongs in `workers/schedule.py` and in every environment's scheduler. This page does not record a live scheduler change.

The sweep is safe while the flag is off because it only deletes `public_demo` rows.

## What stays real

AI agents, assessments, rankings, workers, row-level security and capabilities stay on the normal path. There are no mocked model responses. Candidates are still assessed against the locked contract snapshot. Words are still made server-side. Permissions are still capabilities.

## Limits

- The flag ships default off. Live activation is not done in this document. https://readypick.ai is not in demo mode.
- Do not commit `demo_mode = true` in tfvars. `DEMO_*` can stay as application defaults so a later apply does not roll tasks just to set false. Enabling the flag is an operator action.
- September 2026 Cost Explorer (account 016617990245, unblended, estimated false, grouped by service) was about $116.60 including $17.78 tax. The largest lines were EC2 Other $35.22 (NAT and endpoints), RDS $26.45, ELB $15.05, and ElastiCache $12.56. October was still Estimated and is not reported as a bill. The live composition in [INFRA_TOPOLOGY.md](INFRA_TOPOLOGY.md) is Postgres on `db.t4g.medium`, Fargate services, Lambda and NAT. Judge0 was destroyed (S-20) and is not part of this mode.
- Fargate on-demand vCPU quota in ap-south-2 is 2 (L-3032A538). On 2026-10-30 the analysis service (2 vCPU) was scaled to desired count 0 so the API task could place. Pilot Terraform pins that count at 0. Do not raise it until the quota is raised, or the next apply leaves the API unable to start. Proctoring analysis stays off while the count is 0. Lambda concurrent execution quota is 1000, not 10.

## Rollback

Set `DEMO_MODE=false` on the API and on every worker that sends mail, and roll those tasks. The UI goes off only when the frontend image is rebuilt without `--public-demo` (`NEXT_PUBLIC_DEMO_MODE=false`, the Dockerfile default). A task environment variable does not change the UI. Columns can stay. The sweep can stay: while the flag is off it only deletes `public_demo` rows.

Do not drop the `public_demo` columns on a database that may already hold demo rows unless a counted check shows that drop is safe. This page does not give that drop.

## Rollback of a bad deploy

Roll back to the previous image tag by [DEPLOY_AWS.md](DEPLOY_AWS.md) section 7. This page does not name a tag. A schema migration does not roll back with the image. Leave the columns in place, as in the flag rollback above.
