# Public demo mode

**Status:** current. **Reviewed:** 2026-10-30 against the owner statements of that date (register A-23 and A-24). The behavior below is specified. The code default for the flag is off. As of 2026-10-30 the pilot task definition for the API (`readypick-pilot-api:56`) sets `DEMO_MODE=true` and `DEMO_MODE_CONFIRM=public-demo`. The frontend image (`readypick-pilot-frontend:46`, tag `sha-cdf9b73d358a`) was built with `NEXT_PUBLIC_DEMO_MODE=true`. tfvars were not changed and must not say `demo_mode=true`. The CAPTCHA removal (A-24) is deployed on `readypick-pilot-api:56` and `readypick-pilot-frontend:46`. September 2026 account cost was about $116.60 unblended, including tax, measured from Cost Explorer for account 016617990245. October was still estimated and is not a bill.

A-23 amends A-05 only while `DEMO_MODE` is on. A-25 (2026-10-31) makes demo users persistent and demo mail real. A-24 removed the server-drawn CAPTCHA from every auth surface, including when the flag is off. Register: [../decisions/REGISTER.md](../decisions/REGISTER.md). What runs on AWS: [INFRA_TOPOLOGY.md](INFRA_TOPOLOGY.md). Image rollback: [DEPLOY_AWS.md](DEPLOY_AWS.md) section 7.

`public_demo` is a new column (migration head `0137_public_demo`). It is not `is_demo`. `is_demo` remains the C-07 billing-exemption flag.

## Enable

The code default is off unless an operator turns it on. The pilot task definition named in the status line carries the flag. Do not write `demo_mode=true` into tfvars.

| Setting | Role |
|---|---|
| `DEMO_MODE` | Backend gate. Default off. |
| `NEXT_PUBLIC_DEMO_MODE` | Frontend gate. Default off. Next inlines it at image build, so the frontend image must be built with `--build-arg NEXT_PUBLIC_DEMO_MODE=true` (the remote builder's `--public-demo`). A task environment variable does not turn the UI on. An empty value is off. Set it in the same release as `DEMO_MODE`. |
| `DEMO_MODE_CONFIRM` | Required only when `DEMO_MODE` is on and `ENVIRONMENT=production`. The value must be `public-demo`. |

The live pilot sets `ENVIRONMENT=production`, even though the Terraform composition is named pilot. Turning the flag on there without the confirm refuses to boot (below). Other environments do not require the confirm. The code default is off. As of 2026-10-30 the pilot API task definition `readypick-pilot-api:56` sets `DEMO_MODE=true` and `DEMO_MODE_CONFIRM=public-demo`. The frontend image `readypick-pilot-frontend:46` (tag `sha-cdf9b73d358a`) was built with `NEXT_PUBLIC_DEMO_MODE=true`. tfvars were not changed and must not say `demo_mode=true`.

When the flag is off, existing Firebase and Razorpay routes stay unchanged. A-24 removed the CAPTCHA everywhere, including when the flag is off. The skips below do not run. The CAPTCHA removal is deployed on `readypick-pilot-api:56` and `readypick-pilot-frontend:46`.

## Confirm gate

On `ENVIRONMENT=production`, `DEMO_MODE` also requires `DEMO_MODE_CONFIRM=public-demo`. A missing confirm refuses to boot. A value other than `public-demo` does not satisfy the gate.

While `DEMO_MODE` is unset or false, production boots without this confirm. The pilot API task definition named above carries both `DEMO_MODE=true` and `DEMO_MODE_CONFIRM=public-demo`. Do not treat an arbitrary running task as proof the confirm is set. tfvars were not changed and must not say `demo_mode=true`.

## Who can be created or retrieved

Demo login (`POST /api/v1/auth/demo/session`) accepts any syntactically valid email and any nonempty password. It never binds a user who is not `public_demo`.

| Action | Rule |
|---|---|
| Company login | Retrieves only. Does not create: a company user cannot exist without a tenant, so a new email registers below. |
| Company registration (`POST /api/v1/company-onboarding/demo`) | Creates. |
| Candidate login | Creates or retrieves. Must not claim a sourced candidate (`email_verified` false). |
| BD login | Creates or retrieves a `bd` user with no tenant. Sees and edits only the leads it owns, has `use_ai_reach` switched off, and the owner audience gives it no route to the Super Admin console. |

The same email and portal always resolves to the same user, whatever password is typed. An advisory lock per email and portal makes parallel first logins create one row. The same email may hold one user per portal. The provider portal is never reachable here.
| Invite setup-password, `public_demo` branch | The existing `POST /api/v1/companies/invites/{token}/setup-password` handler (also mounted under `/api/v2`). The `public_demo` branch skips Firebase and Razorpay only when the flag is on. |

`GET /api/v1/auth/demo-status` reports the flag. It does not mint a session.

Only these paths skip Firebase and Razorpay, and only when the flag is on:

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

## Email

Demo mode withholds nothing. Mail to any address goes through the deployment's one transport (SES in production), with delivery and failure recorded by the existing email log. A failed send never blocks a non-email feature. Sender authorization is unchanged: SES refuses an unverified From identity and the sender flow checks eligibility without creating identities (register A-25).

## Persistence

Demo users, companies, candidates, assessments, reports, ledger rows and transactions persist. No application job expires or deletes them. A reset is a one-time, owner-confirmed operation and is never a scheduled task or part of a deploy.

## What stays real

AI agents, assessments, rankings, workers, row-level security and capabilities stay on the normal path. There are no mocked model responses. Candidates are still assessed against the locked contract snapshot. Words are still made server-side. Permissions are still capabilities.

## Limits

- The code default is off. As of 2026-10-30 the pilot task definition for the API (`readypick-pilot-api:56`) sets `DEMO_MODE=true` and `DEMO_MODE_CONFIRM=public-demo`. The frontend image (`readypick-pilot-frontend:46`, tag `sha-cdf9b73d358a`) was built with `NEXT_PUBLIC_DEMO_MODE=true`. tfvars were not changed and must not say `demo_mode=true`. The CAPTCHA removal (A-24) is deployed on `readypick-pilot-api:56` and `readypick-pilot-frontend:46`.
- Do not commit `demo_mode = true` in tfvars. `DEMO_*` can stay as application defaults so a later apply does not roll tasks just to set false. Enabling the flag is an operator action.
- September 2026 Cost Explorer (account 016617990245, unblended, estimated false, grouped by service) was about $116.60 including $17.78 tax. The largest lines were EC2 Other $35.22 (NAT and endpoints), RDS $26.45, ELB $15.05, and ElastiCache $12.56. October was still Estimated and is not reported as a bill. The live composition in [INFRA_TOPOLOGY.md](INFRA_TOPOLOGY.md) is Postgres on `db.t4g.medium`, Fargate services, Lambda and NAT. Judge0 was destroyed (S-20) and is not part of this mode.
- Fargate on-demand vCPU quota in ap-south-2 is 2 (L-3032A538). On 2026-10-30 the analysis service (2 vCPU) was scaled to desired count 0 with the AWS CLI so the API task could place. The ECS module rejects a standing service at 0, so pilot Terraform keeps `desired_count = local.service_count` and `ignore_changes` on desired count holds the CLI value. Do not raise the running count until the quota is raised. Proctoring analysis stays off while the count is 0. Lambda concurrent execution quota is 1000, not 10.

## Rollback

Set `DEMO_MODE=false` on the API and on every worker that sends mail, and roll those tasks. The UI goes off only when the frontend image is rebuilt without `--public-demo` (`NEXT_PUBLIC_DEMO_MODE=false`, the Dockerfile default). A task environment variable does not change the UI. Columns and demo rows stay.

Do not drop the `public_demo` columns on a database that may already hold demo rows unless a counted check shows that drop is safe. This page does not give that drop.

## Rollback of a bad deploy

Roll back to the previous image tag by [DEPLOY_AWS.md](DEPLOY_AWS.md) section 7. This page does not name a tag. A schema migration does not roll back with the image. Leave the columns in place, as in the flag rollback above.
