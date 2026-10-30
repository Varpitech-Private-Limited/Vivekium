"""No credential may be shipped as a plain environment variable.

WHAT THIS PINNED BEFORE, AND WHY THE FILE SURVIVED THE AWS MIGRATION
---------------------------------------------------------------------
`DATABASE_URL` was once composed at deploy time and passed with
`--set-env-vars`, so the assembled DSN, password included, sat readable on every
revision to anyone holding `run.services.get`. The password itself was always in
Secret Manager and the composed value was never logged, so this was narrower
than the audit that found it claimed -- but it was still a credential
materialised where it did not need to be.

The GCP deploy script that carried that fix is gone (spec-doc5 §D.2). THE
GUARANTEE IS NOT. When the script went, six assertions started skipping with
"deploy script is not present in this checkout", which is the exact failure mode
this project has already been burned by: a check that reports SKIPPED reads
almost the same as one that reports PASSED in a summary line, and nothing was
enforcing secret hygiene any more.

So the assertions were ported rather than deleted, and they now read the AWS
artefacts:

    the Terraform          a credential is a `secrets` block on the container
                           definition, never an `environment` entry
    the workflow           no credential is echoed, and no AWS access key is
                           stored in the repository
    the per-service IAM    a service cannot read a secret it does not need

The last one is genuinely stronger than what the GCP version could assert. There
the check was "the deploy script does not print the DSN"; here it is "the
worker's IAM policy does not include the Firebase key at all", which is a
property of the infrastructure rather than of one script's care.

EXTENDED IN SPEC-DOC6 §13.4 TO THE TRAFFIC LAYER
--------------------------------------------------
"Keep the ported secret-hygiene assertions... and extend them to the new
modules." The four modules added in that phase (alb, acm, dns, waf) do not mount
application secrets, so extending the assertions meant asking what each of them
can leak instead of copying a check that would pass vacuously:

    alb   ACCESS LOGS AND THE UNAUTHENTICATED SURFACE. The log bucket must be
          private and TLS-only, and `public_path_patterns` is the enumerated
          list of paths that reach the API before the application is asked
          anything. That list is the traffic layer's equivalent of a secret
          scope, and it is asserted here AND against the running FastAPI
          application, because the two agreeing is the property that matters.

    acm   NO PRIVATE KEY ANYWHERE. ACM holds the key and never exports it, and
          this asserts that nothing in the tree tries to supply one.

    dns   NO ZONE CREATED, NO NAME LOOKUP. A created zone gets four new name
          servers the registrar does not delegate to, so every record in it is
          correct and unresolvable.

    waf   REDACTED LOGS. A WAF log records the request that matched, headers
          included, and this product's requests carry a session cookie and an
          Authorization header. Without redaction the log group becomes a
          credential store.

And two that cover the whole tree rather than one module: no account id, region
or domain is hardcoded anywhere (spec-doc6 §D5), and the offline planning
profile passes no credential as a Terraform variable, because a variable is
written into the plan file and the state file.
"""
from __future__ import annotations

import functools
import json
import pathlib
import re
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
STAGING = ROOT / "infra" / "environments" / "staging" / "main.tf"
PRODUCTION = ROOT / "infra" / "environments" / "production" / "main.tf"
SECRETS_VARS = ROOT / "infra" / "modules" / "secrets" / "variables.tf"
ECS_MAIN = ROOT / "infra" / "modules" / "ecs" / "main.tf"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
INFRA = ROOT / "infra"
ALB_MAIN = INFRA / "modules" / "alb" / "main.tf"
ALB_VARS = INFRA / "modules" / "alb" / "variables.tf"
ACM_MAIN = INFRA / "modules" / "acm" / "main.tf"
DNS_MAIN = INFRA / "modules" / "dns" / "main.tf"
WAF_MAIN = INFRA / "modules" / "waf" / "main.tf"

#: Every module and environment root, for the tree-wide assertions.
ALL_TERRAFORM = sorted(
    path
    for path in INFRA.rglob("*.tf")
    if ".terraform" not in path.parts
)

#: Names whose VALUE is a credential. A mount is the only acceptable delivery.
#:
#: `REDIS_URL` IS DELIBERATELY PRESENT NOW, and it was deliberately absent
#: before. On the old platform it was a host and port and carried no secret, so
#: making it a mount would have been cargo cult. On ElastiCache with transit
#: encryption and an AUTH token it carries the token, so it is a credential.
CREDENTIAL_NAMES = (
    "DATABASE_URL",
    "REDIS_URL",
    "JWT_SECRET",
    "ANTHROPIC_API_KEY",
    "VOYAGE_API_KEY",
    "SMTP_PASSWORD",
    "FIREBASE_SERVICE_ACCOUNT_JSON",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "LLM_KEY_ENCRYPTION_SECRET",
    "TAVILY_API_KEY",
    # The proctoring analysis service's Hugging Face read token. It is a
    # credential like any other and it is swept for the same reason: the image
    # build needs it to fetch gated weights, which is exactly the situation
    # where a token ends up in a build ARG and then in a layer.
    "HUGGINGFACE_TOKEN",
    # The inbound-mail relay's shared secret. It proves to the PUBLIC route
    # `POST /verification/inbound-email` that the call came from
    # `readypick-inbound-email`, so it is a credential in the only sense that
    # matters here: anyone who reads it can write into verification requests,
    # BGV threads and conversations. Swept like the rest, because the ECS task
    # definition is readable by anyone holding `ecs:DescribeTaskDefinition`.
    #
    # The RELAY's own copy is a plain Lambda environment variable and cannot be
    # anything else -- `lambda/inbound_email/handler.py` is standard library and
    # boto3 with no secret grant at all, deliberately, because it is the one
    # function anything on the open internet can reach. That copy is not in
    # scope here: this sweep reads `common_environment` in the ECS composition.
    "INBOUND_WEBHOOK_SECRET",
    # The code sandbox's API token. Anyone holding it can run arbitrary code
    # on the sandbox host, so it is mounted (pilot stage B: the API, the task
    # worker and the agent, from `module.code_sandbox`) and never a plain
    # environment value on any task definition.
    "JUDGE0_AUTH_TOKEN",
)

ENVIRONMENT_ROOTS = [STAGING, PRODUCTION]


def _code(path: pathlib.Path) -> str:
    """The Terraform with its comments removed.

    Several assertions below refuse a construct that the module's own docstring
    NAMES while explaining why it is absent: `modules/dns` discusses the
    `data "aws_route53_zone"` lookup it does not use, and `modules/alb`
    discusses the `authenticate_oidc` action it does not have. A substring
    search over the raw file matches the explanation and fails.

    Deleting the explanations to satisfy a test would be the wrong repair: the
    reasoning is the most valuable thing in those files. So the test reads the
    code and the prose separately.
    """
    source = _source(path)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)  # /** ... */
    source = re.sub(r"^\s*[#/]{1,2}.*$", "", source, flags=re.MULTILINE)  # # and //
    return re.sub(r"<<-?EOT.*?EOT", "", source, flags=re.DOTALL)  # heredoc prose


def _source(path: pathlib.Path) -> str:
    if not path.exists():
        pytest.fail(
            f"{path.relative_to(ROOT)} is missing. This file's assertions are the "
            f"only thing enforcing secret hygiene; if the artefact moved, port "
            f"them rather than letting them skip."
        )
    return path.read_text(encoding="utf-8")


# ── The credential must be a MOUNT, never an env var ─────────────────────────


@pytest.mark.parametrize("root", ENVIRONMENT_ROOTS, ids=lambda p: p.parent.name)
def test_common_environment_carries_no_credential(root: pathlib.Path) -> None:
    """`common_environment` becomes plain `environment` entries on every task.

    A credential there is a credential printed onto the task definition, which
    is readable by anyone holding `ecs:DescribeTaskDefinition` -- the same shape
    as the finding this file was originally written for.
    """
    source = _source(root)
    start = source.index("common_environment = {")
    end = source.index("\n  }", start)
    block = source[start:end]

    for name in CREDENTIAL_NAMES:
        assert name not in block, (
            f"{name} is in common_environment, which becomes a plain env var on "
            f"the task definition. Move it to the service's `secrets` map so ECS "
            f"injects it instead."
        )


@pytest.mark.parametrize("root", ENVIRONMENT_ROOTS, ids=lambda p: p.parent.name)
def test_every_credential_is_delivered_through_the_secrets_map(root: pathlib.Path) -> None:
    """The other half of the same change, and the one that was invisible in a
    diff on the old platform: excluded from the mounts AND absent from the env
    would mean the application never receives it at all."""
    source = _source(root)
    # Every `NAME = module.secrets.secret_arns["NAME"]` line.
    mounted = set(re.findall(r'(\w+)\s*=\s*module\.secrets\.secret_arns\["(\w+)"\]', source))

    assert mounted, "no secret is mounted at all, so no service can authenticate"
    for env_name, secret_name in mounted:
        # A mount whose env var name and secret name disagree is a mount that
        # silently delivers the wrong value.
        assert env_name == secret_name, (
            f"{env_name} is mounted from the secret {secret_name}. A mismatch "
            f"delivers the wrong value under a name that looks right."
        )

    api_secrets = {env for env, _ in mounted}
    assert "DATABASE_URL" in api_secrets, (
        "DATABASE_URL is not mounted anywhere, so no service would receive a "
        "database URL at all."
    )


def test_the_ecs_module_injects_secrets_rather_than_interpolating_them() -> None:
    """The value must never pass through a shell, a startup script or a log
    line on its way in."""
    source = _source(ECS_MAIN)
    assert 'secrets = [' in source
    assert "valueFrom" in source, (
        "the container definition does not use `valueFrom`, so ECS is not "
        "fetching the value -- something else is composing it"
    )


# ── Per-service scoping: stronger than the old script could assert ───────────


def _service_secrets() -> dict[str, list[str]]:
    """Parse `service_secrets` out of the secrets module's variables.

    Read from SOURCE rather than from a Terraform plan, because a plan needs an
    AWS account and this assertion has to hold in CI with no credentials at all.
    """
    source = _source(SECRETS_VARS)
    start = source.index("variable \"service_secrets\"")
    # BOUNDED AT THE NEXT VARIABLE (2026-09-24). This read to the end of the
    # file, so `service_secret_writers`, declared later with the same
    # `"migrate" = [...]` shape, silently REPLACED the migrate entry: every
    # assertion about what the migration job may READ was asserting what it
    # may WRITE. A grant added to the read map's migrate entry passed.
    end = source.find("\nvariable \"", start + 1)
    block = source[start:] if end == -1 else source[start:end]
    parsed: dict[str, list[str]] = {}
    # The keys are QUOTED, because HCL parses a bare `task-worker` as
    # subtraction. Matching both spellings would let a future unquoted key that
    # Terraform refuses still satisfy this parser, so only the quoted form is
    # accepted and an unparseable file fails the assertion below rather than
    # silently returning an empty map.
    for match in re.finditer(
        r'^\s{4}"([\w-]+)"\s*=\s*\[([^\]]*)\]', block, re.MULTILINE
    ):
        service = match.group(1)
        names = re.findall(r'"(\w+)"', match.group(2))
        parsed[service] = names
    return parsed


def test_no_service_holds_every_secret() -> None:
    """THE FINDING, restated as a property of the infrastructure.

    The GCP-phase problem was one runtime identity holding read access across
    the whole secret namespace: nothing was misconfigured, the grant was simply
    wider than the need, and a wildcard looks identical whether it is over-broad
    or exactly right.
    """
    services = _service_secrets()
    assert services, "service_secrets could not be parsed"

    every_secret = set()
    for names in services.values():
        every_secret.update(names)

    for service, names in services.items():
        assert set(names) != every_secret, (
            f"{service} can read every secret in the platform. That is the "
            f"shared-broad-role shape spec-doc5 §D.4 asks to be designed out."
        )


def test_the_scheduler_holds_no_secret_at_all() -> None:
    """A scheduler that could reach a model credential is a scheduler that could
    spend money.

    The Celery beat service held one secret, the broker, because it had to
    connect to a queue in order to publish. EventBridge Scheduler publishes
    nothing and connects to nothing: it invokes a function, and the function
    holds the credentials. So the answer moved from "one secret" to "none", and
    the assertion is that the scheduler does not appear here at all.
    """
    services = _service_secrets()
    assert "beat" not in services, "the Celery beat scheduler is gone"
    assert "scheduler" not in services, (
        "the scheduler has an entry in service_secrets; it invokes a function "
        "and holds no credential of its own, so an entry here is a grant "
        "nothing needs"
    )


def test_the_trigger_holds_no_secret_at_all() -> None:
    """The one thing in the account holding iam:PassRole.

    It calls ecs:RunTask and returns. It reads nothing, and an empty list would
    be worse than an absence: the module emits one IAM policy per entry, and a
    policy whose statement has an empty resource list is one AWS refuses.
    """
    services = _service_secrets()
    assert "trigger" not in services
    assert "assessment-trigger" not in services


def test_background_work_cannot_read_the_firebase_service_account() -> None:
    """A background task never authenticates a browser session, so it has no
    business being able to read the key that would let it.

    Both halves of the background work are checked: the short-work function and
    the on-demand agent that replaced the Celery worker between them do
    everything that worker did.
    """
    services = _service_secrets()
    for consumer in ("task-worker", "agent"):
        assert consumer in services, f"{consumer} has no entry in service_secrets"
        assert "FIREBASE_SERVICE_ACCOUNT_JSON" not in services[consumer]


def test_the_synchronous_agents_hold_no_delivery_or_payment_credential() -> None:
    """`jd-gen` and `company-profile` write a draft and send nothing.

    Neither drafts an email, neither takes a payment, and neither authenticates
    anybody, so SMTP, Razorpay and Firebase are all reach their work does not
    need.
    """
    services = _service_secrets()
    forbidden = {
        "SMTP_PASSWORD",
        "RAZORPAY_KEY_SECRET",
        "RAZORPAY_WEBHOOK_SECRET",
        "FIREBASE_SERVICE_ACCOUNT_JSON",
        "JWT_SECRET",
    }
    for consumer in ("jd-gen", "company-profile"):
        assert consumer in services, f"{consumer} has no entry in service_secrets"
        overreach = forbidden & set(services[consumer])
        assert not overreach, f"{consumer} can read {sorted(overreach)}"


def test_the_migration_job_holds_exactly_one_secret() -> None:
    """It connects, applies DDL and exits. Anything else it could read is reach
    its work does not need."""
    services = _service_secrets()
    assert set(services.get("migrate", [])) == {"DATABASE_URL"}


def _secret_names() -> list[str]:
    """Parse the `secret_names` default list out of the secrets module."""
    source = _source(SECRETS_VARS)
    start = source.index('variable "secret_names"')
    body = source[source.index("default = [", start):]
    body = body[: body.index("\n  ]")]
    code = "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))
    names = re.findall(r'"(\w+)"', code)
    assert names, "secret_names parsed empty; the parser no longer matches the module"
    return names


#: Held and injected into nothing. See `test_a_held_secret_is_granted_to_no_service`.
HELD_UNGRANTED = "LLM_KEY_ENCRYPTION_SECRET"
ALL_ENVIRONMENT_ROOTS = [ROOT / "infra" / "environments" / "pilot" / "main.tf", STAGING, PRODUCTION]


def test_a_held_secret_is_granted_to_no_service() -> None:
    """The retired key roster's encryption key is KEPT and reaches nothing.

    Two halves, and each one is the failure the other prevents. Removed from
    `secret_names`, Terraform destroys the key, which is irreversible once the
    recovery window passes and is the owner's decision (CONTRACT v2), not a
    side effect of a cleanup. Migration 0128 drops the roster's table only
    when it is empty, so the key opens no row on a migrated database; what it
    may still open is a backup taken before the roster was retired. Granted or mounted, it is a credential in every container for a
    setting nothing reads (2026-09-24: the setting is deleted), which is reach
    no service's work needs.
    """
    assert HELD_UNGRANTED in _secret_names()
    holders = sorted(s for s, names in _service_secrets().items() if HELD_UNGRANTED in names)
    assert not holders, f"{HELD_UNGRANTED} is granted to {holders}"
    for root in ALL_ENVIRONMENT_ROOTS:
        assert HELD_UNGRANTED not in _code(root), (
            f"{root.relative_to(ROOT)} mounts {HELD_UNGRANTED} into a container"
        )


def test_the_webhook_path_is_the_only_holder_of_the_webhook_secret() -> None:
    """It verifies `X-Razorpay-Signature` and nothing else does."""
    services = _service_secrets()
    holders = [s for s, names in services.items() if "RAZORPAY_WEBHOOK_SECRET" in names]
    # `api`, NOT `webhook`. This asserted `["webhook"]` and was green while the
    # secret was mounted on NOTHING: no environment has ever defined a service
    # called `webhook`, and `POST /api/v1/billing/webhook/razorpay` is served by
    # the api service from `api/billing.py`. The handler then treated the absent
    # secret as "development" and processed unsigned events, so credit issuance
    # was an anonymous POST on the live site. The phantom grant is deleted.
    assert holders == ["api"], f"the webhook secret is held by {holders}"


def test_the_secret_policy_enumerates_arns_rather_than_a_prefix() -> None:
    """A prefix grant re-creates the wildcard this whole design removes: it
    silently covers every secret added later, without anybody deciding it
    should."""
    source = _source(ROOT / "infra" / "modules" / "secrets" / "main.tf")
    start = source.index('data "aws_iam_policy_document" "service"')
    end = source.index('resource "aws_iam_policy" "service"', start)
    block = source[start:end]

    assert "for secret in each.value" in block, (
        "the policy no longer enumerates the service's own secrets"
    )
    # A `*` inside the secret statement's resources would be a prefix grant.
    resources = re.search(r"resources\s*=\s*\[([^\]]*)\]", block)
    assert resources and "*" not in resources.group(1), (
        "the secret policy contains a wildcard resource, which grants every "
        "secret added later without anybody deciding it should"
    )


# ── Nothing echoes a credential, and no key is stored in the repo ────────────


def test_the_workflow_stores_no_aws_access_key() -> None:
    """OIDC, the same posture Workload Identity Federation gave before. A
    long-lived key in a repository secret is a key that outlives the person who
    added it."""
    source = _source(WORKFLOW)
    assert "aws-actions/configure-aws-credentials" in source
    assert "id-token: write" in source, "OIDC is not requested, so it is not being used"
    for forbidden in ("AWS_SECRET_ACCESS_KEY", "aws-secret-access-key", "aws_access_key_id"):
        assert forbidden not in source, (
            f"the workflow references {forbidden}. Authentication is OIDC; a "
            f"stored key is a key that outlives whoever added it."
        )


def test_no_shell_script_echoes_a_credential() -> None:
    """A value that reaches a log has left the secret store's protection."""
    scripts = sorted((ROOT / "scripts").glob("*.sh"))
    assert scripts, "no deploy scripts found; this assertion would pass vacuously"

    pattern = re.compile(
        r"\$\{?(DATABASE_URL|JWT_SECRET|ANTHROPIC_API_KEY|VOYAGE_API_KEY|"
        r"SMTP_PASSWORD|pw|password|token)\b",
        re.IGNORECASE,
    )
    for script in scripts:
        for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith(("echo", "printf", "info", "warn")):
                continue
            # A pipe into a secret writer or a token minter is INPUT, not output.
            if "|" in stripped:
                continue
            match = pattern.search(stripped)
            assert match is None, (
                f"{script.name}:{number} can print a credential: {stripped}"
            )


def test_the_production_apply_is_gated_and_disabled() -> None:
    """spec-doc5 §D.1 and the §D acceptance list: no live deployment in this
    phase, and running one is a failure of scope rather than an accomplishment.

    TWO independent stops, asserted separately because they fail differently: a
    repository variable that is unset, and an environment whose required
    reviewer `scripts/verify-approval-gate.sh` checks on every run.
    """
    source = _source(WORKFLOW)
    start = source.index("apply-production:")
    block = source[start:]

    assert "vars.AWS_DEPLOY_ENABLED == 'true'" in block, (
        "the production apply is not behind the disable flag"
    )
    assert re.search(r"environment:\s*\n\s*name:\s*production", block), (
        "the production apply does not declare the gated environment"
    )
    assert "verify-approval-gate" in block, (
        "the production apply does not depend on the job that checks the gate "
        "exists -- and an environment with no required reviewer promotes "
        "instantly and silently, which is the finding §D.5 names"
    )


# ═════════════════════════════════════════════════════════════════════════════
# THE TRAFFIC LAYER (spec-doc6 §13.2, §13.4)
#
# Four modules that mount no application secret. Copying the mount assertions
# onto them would pass vacuously, which is the failure mode this whole file
# exists to prevent, so each assertion below asks what that module can actually
# leak.
# ═════════════════════════════════════════════════════════════════════════════

#: Dependencies that establish WHO is calling. A route whose full dependency
#: tree contains none of these is reachable by anybody who can reach the port.
#: Enumerated from `app/api/deps.py` rather than guessed. `get_optional_candidate`
#: is DELIBERATELY ABSENT: it returns None instead of raising, so a route
#: carrying only that one is reachable without a session and belongs in the
#: exception list below with its reason.
_AUTH_DEPENDENCIES = frozenset({
    "get_current_user",
    "get_current_candidate",
    "get_current_any",
    "get_tenant_db",
    "get_candidate_db",
    "get_superadmin_db",
})

#: Routes that are unauthenticated ON PURPOSE, each with the reason.
#:
#: They are NOT in the load balancer's public band, and that is the distinction
#: worth keeping: the band is for paths a browser follows from outside, and
#: everything here is either an operational probe or a path whose authorization
#: is a signed single-use token in the URL rather than a session. Adding to this
#: list is adding to the product's unauthenticated surface.
_PUBLIC_BY_DESIGN: dict[str, str] = {
    "/health/live": (
        "the ALB target group's liveness probe. Unauthenticated by necessity: a "
        "load balancer cannot hold a credential, and the check runs before any "
        "session exists. It returns a static status word, touches no dependency "
        "and so discloses nothing. The DEEP probe is /health, which the listener "
        "rules deliberately do NOT route to the API, because whether the "
        "database and cache are up is not a fact this product owes the internet."
    ),
    "/health": (
        "the load balancer's health check. It returns a status word and no data."
    ),
    # Sign-in. A route that mints a session cannot require one.
    "/api/v1/auth/demo-status": (
        "reports whether public demo mode is on. It sets no cookie and "
        "returns one boolean. When the flag is off the answer is false."
    ),
    "/api/v1/auth/demo/session": (
        "opens a session for a public demo identity when DEMO_MODE is on. "
        "It is rate limited, never binds a real account, and answers 404 "
        "when the flag is off so the sign-in page uses Firebase."
    ),
    "/api/v1/auth/firebase/session": (
        "exchanges a verified Firebase ID token for this product's cookies. "
        "The Firebase token IS the authentication."
    ),
    "/api/v1/auth/password-reset/request": (
        "starts a forgotten-password reset for somebody who cannot sign in. "
        "It is rate limited and answers the same sentence whether or not the "
        "address has an account."
    ),
    "/api/v1/auth/password-reset/verify": (
        "exchanges the six-digit security code that went to the mailbox for "
        "a single-use reset ticket. The code is the authentication: HMAC in "
        "Redis, five attempts, ten minutes."
    ),
    "/api/v1/auth/password-reset/complete": (
        "sets the new password with the single-use ticket the verify step "
        "minted, then revokes every session of the account. The ticket is "
        "the authentication and it is consumed by this call."
    ),
    "/api/v1/companies/invites/{token}/setup-password": (
        "creates the invited person's password sign-in for EXACTLY the "
        "invited email, then opens their company session. The invite token "
        "names one pending invitation. The person has no account yet, so a "
        "session cannot be asked for. The route is rate limited."
    ),
    "/api/v2/companies/invites/{token}/setup-password": (
        "the same handler under the v2 prefix, where the companies router is "
        "mounted a second time."
    ),
    # Company self-registration (owner spec 2026-09-29, section 5). The
    # person registering has no account, so no route here can ask for a
    # session. Later steps are authorized by the security code or the
    # onboarding cookie that code mints.
    "/api/v1/company-onboarding/demo": (
        "creates one public demo company when DEMO_MODE is on, with a "
        "simulated registration charge and a real credit grant. It is rate "
        "limited and answers 404 when the flag is off."
    ),
    "/api/v1/company-onboarding/register": (
        "sends a security code (or, to an address that already has an "
        "account, a notice) and answers the same sentence either way. "
        "Rate limited."
    ),
    "/api/v1/company-onboarding/code/resend": (
        "a new security code for a registration already started, sent only "
        "to the mailbox that registration names; the resend window runs for "
        "every address alike, so the answer discloses nothing. Rate limited."
    ),
    "/api/v1/company-onboarding/code/verify": (
        "the six-digit security code from the mailbox is the authentication: "
        "HMAC in Redis, five attempts, ten minutes. A right code creates the "
        "onboarding company and sets the onboarding cookie."
    ),
    "/api/v1/company-onboarding/state": (
        "where this browser's registration stands, read from the signed "
        "onboarding cookie; with no cookie it answers the first step and "
        "discloses nothing."
    ),
    "/api/v1/company-onboarding/monthly/plans": (
        "authorized by the signed onboarding cookie; lists the paid Starter "
        "pilot for the one registration the cookie names."
    ),
    "/api/v1/company-onboarding/monthly/subscribe": (
        "authorized by the signed onboarding cookie; creates a Starter "
        "subscription for the verified registration."
    ),
    "/api/v1/company-onboarding/monthly/verify": (
        "authorized by the signed onboarding cookie and the Razorpay "
        "subscription signature; access follows only a captured full charge."
    ),
    "/api/v1/company-onboarding/activate": (
        "authorized by the signed onboarding cookie AND a paid monthly "
        "charge for its company; it creates the "
        "password sign-in for exactly the registered address and opens the "
        "company session."
    ),
    "/api/v1/auth/refresh": (
        "reads the refresh cookie itself and re-mints for the SAME audience. "
        "A missing or dead cookie returns a dead session rather than data."
    ),
    "/api/v1/auth/logout": "clears cookies. It reads nothing.",
    "/api/v1/auth/select-context": (
        "exchanges a single-use context_token, which is proof of a completed "
        "sign-in. The token is the authorization and it is consumed."
    ),
    # Signed, single-use tokens in the URL. The token is the authorization, and
    # every handler filters by the exact token row: see `get_public_db`.
    "/api/v1/companies/invites/{token}": "an invite token names one pending invitation.",
    "/api/v2/companies/invites/{token}": "the same handler under the v2 prefix.",
    "/api/v1/portal/consent/renew": (
        "the one-click link in the six-month consent renewal letter (feature "
        "8). The token is the authorization: single use, because renewal "
        "clears the stored hash; short lived, because the expiry is checked "
        "inside the lookup statement; bound to one candidate, because the "
        "lookup is by hash on that row; and stored only as a SHA-256 digest, "
        "so a reader of the table cannot replay it. It authorises exactly one "
        "act and is accepted nowhere else, it mints no session, and it reads "
        "nothing back. Requiring a cookie would defeat the purpose: the "
        "reader is somebody who has not signed in for six months and whose "
        "profile is about to be deleted."
    ),
    "/api/v1/bgv/form/{token}": (
        "the employer HR checkbox form (vivekium feature 4). The token is "
        "minted per verification, single-use, and expires in "
        "verification_link_ttl_days; the handler resolves only the row the "
        "token names, serves the seven fixed items and accepts only booleans "
        "over them. An HR contact at another company cannot hold a session "
        "here, which is the same reason the retiring "
        "/verification/form/{token} was public."
    ),
    "/api/v2/assessments/invitations/{token}": (
        "an assessment invitation token names one job_candidate_link."
    ),
    # Signature-verified inbound webhooks. They cannot carry a session because
    # the caller is a vendor, and each verifies a signature before it acts.
    "/api/v1/billing/webhook/razorpay": (
        "verifies X-Razorpay-Signature against RAZORPAY_WEBHOOK_SECRET, which "
        "is held by no other service."
    ),
    "/api/v1/verification/inbound-email": "the inbound-email parser's webhook.",
    "/api/v1/email-senders/events/ses": (
        "SES delivery/bounce/complaint notifications over SNS (2026-09-05 "
        "spec, Corporate Email System section 8). The handler verifies the "
        "SNS message signature against Amazon's pinned certificate host and "
        "refuses any TopicArn other than the configured one before touching "
        "a row; an empty setting refuses everything."
    ),
    # The public employer pages (2026-09-05 spec, Employer Page & Content).
    # The spec makes these the one product surface beside the published job
    # page that is deliberately reachable without an account: a candidate
    # searches a company, reads about it and applies from its careers list.
    # Both routes are rate-limited, serve a closed field allowlist with no
    # contacts, billing or applicant counts, and hide non-public tenants
    # behind the same 404 an unknown slug gets.
    "/api/v1/employers": "the public employer directory search.",
    # The published price list (owner spec 2026-09-29, section 4.2). The
    # public /pricing page renders it, so it is read before any account
    # exists. It reads no tenant and no table: the catalogue is code, the
    # figures every checkout quote and GST invoice use, and it carries no
    # account's setup-fee waiver or trial state. Rate limited.
    "/api/v1/billing/public/plans": (
        "the platform's published monthly plan list for the public pricing "
        "page, built from code constants; it reads no tenant and no row."
    ),
    "/api/v1/employers/{slug}": "one public employer page with its careers list.",
    # Genuinely public, and each returns something already public.
    "/api/v1/telemetry/landing-view": (
        "a rate-limited anonymous counter for the marketing page. It retains no "
        "visitor PII and returns 204."
    ),
}


def _dependency_names(dependant: Any, seen: set[str]) -> set[str]:
    if dependant.call is not None:
        seen.add(getattr(dependant.call, "__name__", repr(dependant.call)))
    for sub in dependant.dependencies:
        _dependency_names(sub, seen)
    return seen


def _collect(
    routes: Any, prefix: str, inherited: frozenset[str], out: dict[str, frozenset[str]]
) -> None:
    """Walk a route tree, DESCENDING into included routers.

    WHY THIS RECURSES, which it did not have to before FastAPI 0.140.
    `include_router` used to flatten: every route of the included router was
    copied onto the application as an `APIRoute` with the prefix already
    applied, so a flat scan of `app.routes` saw everything. Newer FastAPI keeps
    the included router as one nested `_IncludedRouter` object instead, and a
    flat scan sees 26 opaque objects plus the single route main.py registers
    inline, `/health`.

    That is exactly what CI reported: a one-entry route table on a commit whose
    application serves 239 paths. The old scan did not fail loudly, either --
    `test_the_rest_of_the_api_is_not_unauthenticated` iterates the table it is
    given, so an empty table passes it vacuously. A security check that
    silently stops checking is worse than one that breaks, which is why
    `_api_routes` asserts the table is a plausible size below.

    Both shapes are handled: an `APIRoute` is taken directly, an included
    router is entered through its `include_context` (which carries the prefix
    and the router-level dependencies), and anything else exposing `.routes` is
    entered generically.
    """
    from fastapi.routing import APIRoute

    for route in routes:
        if isinstance(route, APIRoute):
            names = frozenset(_dependency_names(route.dependant, set())) | inherited
            path = prefix + route.path
            out[path] = names | out.get(path, frozenset())
            continue

        context = getattr(route, "include_context", None)
        if context is not None:
            # Router-level dependencies apply to every route underneath and are
            # not on the child's own dependant, so they are carried down.
            extra = set()
            for dependency in getattr(context, "dependencies", None) or []:
                call = getattr(dependency, "dependency", None)
                if call is not None:
                    extra.add(getattr(call, "__name__", repr(call)))
            _collect(
                context.included_router.routes,
                prefix + (getattr(context, "prefix", "") or ""),
                inherited | frozenset(extra),
                out,
            )
            continue

        nested = getattr(route, "routes", None)
        if nested:
            _collect(
                nested, prefix + (getattr(route, "prefix", "") or ""), inherited, out
            )


@functools.lru_cache(maxsize=1)
def _api_routes() -> dict[str, frozenset[str]]:
    """{route path -> every callable in its resolved dependency tree}.

    The REAL application object, not a list of paths someone maintained by
    hand. A route added without an auth dependency has to show up here, which
    is the whole point: a hand-maintained list is a list of the routes somebody
    remembered.
    """
    from app.main import app

    # The same handler is mounted under /api/v1 and /api/v2, so `_collect`
    # merges rather than overwrites: a path is authenticated only if every
    # mount of it is.
    routes: dict[str, frozenset[str]] = {}
    _collect(app.routes, "", frozenset(), routes)
    assert len(routes) > 100, (
        f"the application reported only {len(routes)} routes, which is not a "
        f"working route table. Every assertion in this file iterates it, so an "
        f"empty one passes them all without checking anything."
    )
    return routes


def _public_path_patterns() -> list[str]:
    """`public_path_patterns` as the staging root passes it to the alb module.

    Read from SOURCE rather than from a plan, for the same reason
    `_service_secrets` is: this must hold in CI with no AWS credentials at all.
    """
    source = _source(STAGING)
    start = source.index("public_path_patterns = [")
    end = source.index("]", start)
    return re.findall(r'"([^"]+)"', source[start:end])


# ── alb: the access log bucket ───────────────────────────────────────────────


def test_the_access_log_bucket_is_private_and_tls_only() -> None:
    """An access log line carries a client IP, a request path and a user agent.

    That is not resume data and it is not nothing, and a log bucket is the one
    people forget: the application bucket in `modules/s3` gets reviewed because
    everybody already knows what is in it.
    """
    source = _source(ALB_MAIN)

    assert 'resource "aws_s3_bucket_public_access_block" "logs"' in source, (
        "the access log bucket has no public access block"
    )
    for setting in (
        "block_public_acls",
        "block_public_policy",
        "ignore_public_acls",
        "restrict_public_buckets",
    ):
        assert re.search(rf"{setting}\s*=\s*true", source), (
            f"the access log bucket does not set `{setting} = true`"
        )

    assert "DenyInsecureTransport" in source, (
        "the access log bucket policy has no statement refusing plaintext "
        "access, so a log can be read over HTTP"
    )


def test_the_access_log_bucket_policy_names_a_source_account() -> None:
    """`s3:PutObject` for a service principal with no `aws:SourceAccount`
    condition lets ANY AWS customer's load balancer write into this bucket.

    It is the confused-deputy shape, and it is easy to miss because the policy
    reads as narrow: the principal is a named AWS service.
    """
    source = _source(ALB_MAIN)
    start = source.index('data "aws_iam_policy_document" "logs"')
    end = source.index('resource "aws_s3_bucket_policy" "logs"', start)
    block = source[start:end]

    assert "aws:SourceAccount" in block, (
        "the log delivery statement has no aws:SourceAccount condition, so any "
        "AWS account's load balancer could write into this bucket"
    )
    assert "logdelivery.elasticloadbalancing.amazonaws.com" in block


def test_the_access_log_bucket_is_not_the_application_bucket() -> None:
    """Two buckets, because they have genuinely different requirements.

    The application bucket is encrypted with the environment's customer-managed
    key because it holds resumes. The ELB log delivery service cannot write to a
    CMK-encrypted bucket and it fails SILENTLY: logging stops and the load
    balancer stays healthy. Merging them would either break logging or downgrade
    the encryption on the resumes.
    """
    source = _source(STAGING)
    assert "access_logs_bucket_name = var.access_logs_bucket_name" in source
    assert "bucket_name = var.storage_bucket_name" in source

    alb = _source(ALB_MAIN)
    assert 'sse_algorithm = "AES256"' in alb, (
        "the access log bucket is not SSE-S3. A customer-managed key makes "
        "every log write fail, and fail silently."
    )


# ── alb: the unauthenticated surface (RBAC §15 and §33) ──────────────────────


def test_the_public_listener_rules_carry_no_load_balancer_authentication() -> None:
    """THE LOAD BALANCER ROUTES; IT DOES NOT AUTHORIZE.

    RBAC §33: "Authorization must be enforced server-side". An
    `authenticate_oidc` action anywhere in this module would create a second
    authorization boundary alongside `require_capability`, and on the day the
    two disagreed the one nobody was reading would win.
    """
    source = _code(ALB_MAIN)
    for action in ("authenticate_oidc", "authenticate_cognito"):
        assert action not in source, (
            f"the alb module contains an `{action}` action. Authorization is "
            f"the application's, and a second boundary is one that will "
            f"eventually disagree with the first."
        )


def test_the_public_path_list_is_enumerated_and_not_a_catch_all() -> None:
    """A pattern like `/api/*` in the public band routes the entire
    authenticated surface through the rules whose whole purpose is to be
    exhaustively reviewable."""
    patterns = _public_path_patterns()
    assert patterns, "public_path_patterns could not be parsed from the staging root"

    for pattern in patterns:
        assert pattern not in ("/*", "*", "/api/*", "/api/v1/*", "/api/v2/*"), (
            f"{pattern} is a catch-all in the unauthenticated band"
        )
        assert "jobs/public" in pattern, (
            f"{pattern} is in the unauthenticated band and is not the public "
            f"job path. RBAC §15 makes the published job page the one product "
            f"surface reachable without authentication; anything else widens it."
        )


def test_the_module_refuses_a_catch_all_public_pattern() -> None:
    """The list above is correct today. This asserts it cannot QUIETLY stop
    being correct: the module's own variable validation refuses the shape, so a
    widening fails the plan rather than only this test."""
    source = _source(ALB_VARS)
    start = source.index('variable "public_path_patterns"')
    end = source.index('variable "routes"', start)
    block = source[start:end]

    assert "validation" in block, "public_path_patterns has no validation block"
    assert '"/api/*"' in block and '"/*"' in block, (
        "the validation does not refuse a catch-all pattern"
    )


def test_application_routes_priorities_cannot_shadow_the_public_band() -> None:
    """Listener rules are evaluated lowest priority first and the first match
    wins. A broad `/api/*` rule at priority 5 would swallow the public job path
    before its own rule was reached, and the symptom would be an authenticated
    404 on a link a candidate was sent."""
    source = _source(ALB_VARS)
    block = source[source.index('variable "routes"'):]

    assert "priority >= 100" in block, (
        "application routes are not held above the public band, so one could be "
        "given a priority that shadows a public path"
    )


@pytest.mark.parametrize("root", ENVIRONMENT_ROOTS, ids=lambda p: p.parent.name)
def test_both_environments_declare_the_same_public_surface(root: pathlib.Path) -> None:
    """Staging exists to predict production. A public path in one and not the
    other means the environment that was tested is not the one that ships."""
    source = _source(root)
    start = source.index("public_path_patterns = [")
    end = source.index("]", start)
    assert re.findall(r'"([^"]+)"', source[start:end]) == _public_path_patterns()


def test_the_public_job_path_is_unauthenticated_in_the_application_too() -> None:
    """THE OTHER LAYER (spec-doc6 §13.2: "assert this at the ALB/listener-rule
    level AND in application tests").

    The listener rule and the route handler have to agree, and neither alone is
    the property worth having. A listener rule pointing at a route that demands
    a session sends every candidate a link that 401s; a handler with no auth
    behind a rule nobody wrote is simply unreachable.

    So this walks the FastAPI application's real route table, resolves each
    route's full dependency tree, and asserts that every path the Terraform
    routes into the unauthenticated band resolves to a handler carrying no
    authentication dependency.
    """
    routes = _api_routes()
    for pattern in _public_path_patterns():
        prefix = pattern.rstrip("*")
        matching = {path: deps for path, deps in routes.items() if path.startswith(prefix)}
        assert matching, (
            f"the load balancer routes {pattern} into the unauthenticated band "
            f"and the application registers no route under {prefix}. The rule "
            f"points at nothing."
        )
        for path, deps in matching.items():
            offending = deps & _AUTH_DEPENDENCIES
            assert not offending, (
                f"{path} is in the load balancer's unauthenticated band but its "
                f"handler depends on {sorted(offending)}. Every candidate "
                f"following a job link would be refused."
            )


def test_the_rest_of_the_api_is_not_unauthenticated() -> None:
    """The converse, and the half that actually protects anything.

    RBAC §33: knowing an id "MUST NOT be sufficient to gain access", and
    "Obscurity is NOT authorization". Asserting only that the public path is
    open would pass just as happily if every route in the product were open.
    """
    routes = _api_routes()
    public_prefixes = tuple(pattern.rstrip("*") for pattern in _public_path_patterns())

    unauthenticated = sorted(
        path
        for path, deps in routes.items()
        if not (deps & _AUTH_DEPENDENCIES)
        and not path.startswith(public_prefixes)
        and path not in _PUBLIC_BY_DESIGN
    )

    assert not unauthenticated, (
        f"these routes carry no authentication dependency and are not on the "
        f"documented public list: {unauthenticated}. Either they are a hole, or "
        f"they belong in _PUBLIC_BY_DESIGN with the reason they are open."
    )


def test_every_documented_public_route_still_exists_and_carries_a_reason() -> None:
    """An exception list nobody prunes is how a check stops meaning anything.

    Two ways that happens, and both are caught here: an entry whose route was
    deleted leaves the list looking longer and more considered than it is, and
    an entry with an empty reason is a path somebody waved through.
    """
    routes = _api_routes()
    for path, reason in _PUBLIC_BY_DESIGN.items():
        assert path in routes, (
            f"{path} is on the public-by-design list and the application no "
            f"longer serves it. Remove the entry."
        )
        assert len(reason) > 20, (
            f"{path} is on the public-by-design list with no real reason given"
        )


def test_the_public_job_handler_returns_no_internal_field() -> None:
    """RBAC §33 restated as a property of the response shape.

    What makes the public job path safe is not that the id is a UUID. It is that
    the handler answers with a projection carrying no status, no creator, no
    compensation and no approval trail, so an id that leaks buys the holder
    exactly the job advert that was already posted publicly.
    """
    from app.schemas.jobs import PublicJobOut

    forbidden = {
        "status",
        "created_by",
        "compensation_json",
        "approvals",
        "tenant_id",
        "assessment_status",
        "framework_approved_at",
    }
    leaked = forbidden & set(PublicJobOut.model_fields)
    assert not leaked, (
        f"PublicJobOut exposes {sorted(leaked)} on the unauthenticated path"
    )


# ── acm, dns, waf ────────────────────────────────────────────────────────────


def test_no_private_key_is_supplied_to_acm() -> None:
    """ACM generates the key and never exports it, which is the reason to use
    it. `aws_acm_certificate` also accepts an IMPORTED certificate with a
    `private_key` argument, and a private key in Terraform is a private key in
    the state file."""
    source = _code(ACM_MAIN)
    for argument in ("private_key", "certificate_body", "certificate_chain"):
        assert argument not in source, (
            f"the acm module sets `{argument}`, which imports a certificate and "
            f"puts its private key into the Terraform state"
        )
    assert re.search(r'validation_method\s*=\s*"DNS"', source), (
        "the certificate is not DNS-validated, so renewal needs a person to "
        "click a link in an inbox every time"
    )


def test_the_certificate_arn_comes_from_the_validation_resource() -> None:
    """Reading `aws_acm_certificate.this.arn` returns an ARN the moment the
    request exists, which lets the HTTPS listener be created against a
    PENDING_VALIDATION certificate. AWS accepts that listener and then fails the
    TLS handshake for every visitor."""
    source = (INFRA / "modules" / "acm" / "outputs.tf").read_text(encoding="utf-8")
    assert "aws_acm_certificate_validation.this.certificate_arn" in source, (
        "certificate_arn is not read from the validation resource, so the "
        "listener can be built on an unissued certificate"
    )


def test_the_dns_module_creates_no_hosted_zone_and_looks_none_up() -> None:
    """A created zone gets four name servers the registrar does not delegate to,
    so every record in it is correct and resolves for nobody while the real zone
    keeps serving the old answers. A name-based `data` lookup has the quieter
    version: it resolves to whichever zone matches, stale duplicates included,
    and it needs a live API call the offline plan cannot make."""
    source = _code(DNS_MAIN)
    assert 'resource "aws_route53_zone"' not in source, (
        "the dns module creates a hosted zone. spec-doc6 §13.2: referenced, "
        "never created blindly."
    )
    assert 'data "aws_route53_zone"' not in source, (
        "the dns module looks a zone up by name, which resolves to whichever "
        "zone matches and needs a live API call"
    )


def test_the_waf_redacts_credentials_from_its_logs() -> None:
    """A WAF log records the request that matched, headers included. This
    product's requests carry a session cookie and an Authorization header, so an
    unredacted security log is a credential store with a retention policy."""
    source = _source(WAF_MAIN)
    block = source[source.index('resource "aws_wafv2_web_acl_logging_configuration"'):]

    assert block.count("redacted_fields") >= 2, (
        "the WAF logging configuration redacts fewer than two headers"
    )
    for header in ("authorization", "cookie"):
        assert f'name = "{header}"' in block, (
            f"the WAF logs the {header} header unredacted"
        )


def test_the_waf_is_built_and_disabled() -> None:
    """spec-doc6 §13.2: "Build it; leave it disabled by variable so enabling is
    a one-line decision." Disabled means the module creates nothing, not a
    permissive web ACL that costs money and proves nothing."""
    source = _source(WAF_MAIN)
    assert "count = var.enabled ? 1 : 0" in source, (
        "the web ACL is not gated on `enabled`, so it is created regardless"
    )

    variables = (INFRA / "modules" / "waf" / "variables.tf").read_text(encoding="utf-8")
    start = variables.index('variable "enabled"')
    end = variables.index('variable "count_only"', start)
    assert re.search(r"default\s*=\s*false", variables[start:end]), (
        "the WAF defaults to enabled"
    )

    for root in ENVIRONMENT_ROOTS:
        environment = _source(root)
        block = environment[environment.index('module "waf"'):]
        block = block[: block.index("\n}")]
        assert re.search(r"enabled\s*=\s*false", block), (
            f"{root.parent.name} enables the WAF. Turning it on is a decision "
            f"that comes with a week of count-mode metrics against real resume "
            f"and interview traffic, not a checkbox."
        )


# ── Tree-wide: nothing account-specific is hardcoded (spec-doc6 §D5) ─────────


def test_no_account_id_region_or_domain_is_hardcoded() -> None:
    """spec-doc6 §D5: "Every account-specific value... is a declared Terraform
    variable with no default", and "Region assumption `ap-south-1` is removed as
    an assumption... Do not hardcode it anywhere."

    Comments are exempt and code is not: `docs/operations/DEPLOY_AWS.md` and several module
    docstrings DISCUSS ap-south-1 as the likely choice, which is the owner's
    decision to make. Naming it in an argument would make it already made.

    ONE EXEMPTION, AND IT IS NOT A CHOICE: a `backend` block. Terraform does not
    evaluate variables inside one at all, so the state bucket's region has to be
    a literal there or there is no remote state. The exemption is narrow on
    purpose, keyed on the file being nothing but a backend block, so it cannot
    be used to smuggle a hardcoded region into a resource.
    """
    account = re.compile(r'"[0-9]{12}"')
    region = re.compile(r'"(af|ap|ca|eu|il|me|sa|us)-[a-z]+-[0-9]"')

    offenders: list[str] = []
    for path in ALL_TERRAFORM:
        if path.name == "backend.tf":
            body = path.read_text(encoding="utf-8")
            assert 'backend "s3"' in body, (
                f"{path.relative_to(ROOT)} is exempt from the hardcoded-region "
                "rule only because a backend block cannot take a variable. It "
                "no longer contains one, so the exemption no longer applies."
            )
            # STRIP THE COMMENTS FIRST. This test's own docstring says
            # "comments are exempt and code is not", and the loop below honours
            # that; this branch did not, so it was asking whether the PROSE
            # contained the word "resource". A backend.tf explaining what an
            # empty-state apply costs ("Terraform would try to create every
            # resource from scratch") failed a check about executable blocks.
            # The same class of defect as the reachability grep that matched
            # comments and manufactured a phantom workstream.
            code = "\n".join(
                line
                for line in body.splitlines()
                if not line.strip().startswith(("#", "*", "/*", "*/"))
            )
            assert "resource " not in code and "module " not in code, (
                f"{path.relative_to(ROOT)} holds more than a backend block. The "
                "exemption covers the one construct Terraform will not let take "
                "a variable, not a file that happens to be called backend.tf."
            )
            continue
        in_heredoc = False
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if re.search(r"<<-?EOT", stripped):
                in_heredoc = True
                continue
            if in_heredoc:
                if stripped == "EOT":
                    in_heredoc = False
                continue
            if stripped.startswith(("#", "*", "/*")):
                continue
            if account.search(stripped) or region.search(stripped):
                offenders.append(f"{path.relative_to(ROOT)}:{number} {stripped}")

    assert not offenders, (
        "an account id or a region literal appears in executable Terraform:\n  "
        + "\n  ".join(offenders)
        + "\nBoth are variables with no default. See docs/operations/DEPLOY_AWS.md."
    )


@pytest.mark.parametrize("root", ENVIRONMENT_ROOTS, ids=lambda p: p.parent.name)
def test_every_account_specific_variable_has_no_default(root: pathlib.Path) -> None:
    """The deliverable spec-doc6 §D5 names: "The codebase must be complete
    except for those values." A default would be an invention, and the one that
    was there before this phase, `region = "ap-south-1"`, is removed by name."""
    source = (root.parent / "variables.tf").read_text(encoding="utf-8")

    for name in (
        "account_id",
        "region",
        "availability_zones",
        "domain_name",
        "hosted_zone_id",
        "storage_bucket_name",
        "access_logs_bucket_name",
    ):
        start = source.index(f'variable "{name}"')
        rest = source[start + 1:]
        end = start + 1 + rest.index('\nvariable "') if '\nvariable "' in rest else len(source)
        # A `default` inside a heredoc description is prose, not a default.
        code = re.sub(r"<<-?EOT.*?EOT", "", source[start:end], flags=re.DOTALL)
        assert not re.search(r"^\s*default\s*=", code, re.MULTILINE), (
            f"{name} has a default in {root.parent.name}. spec-doc6 §D5 makes "
            f"every account-specific value a variable with NO default; a "
            f"default here is a fact nobody supplied."
        )


def test_the_planning_profile_passes_no_credential_as_a_variable() -> None:
    """spec-doc6 §13.3 asks for "dummy static credentials supplied from
    environment variables in CI", and the environment is where they must stay.

    A credential passed as a Terraform variable is written into the plan file
    and into the state file, which is the same class of finding this whole file
    was originally written for: a value materialised somewhere it did not need
    to be.
    """
    for path in ALL_TERRAFORM:
        source = path.read_text(encoding="utf-8")
        for forbidden in ("access_key", "secret_key"):
            assert not re.search(rf"^\s*{forbidden}\s*=", source, re.MULTILINE), (
                f"{path.relative_to(ROOT)} sets `{forbidden}`. Credentials reach "
                f"the provider through the environment, never through a variable."
            )

    tfvars = INFRA / "environments" / "offline-plan.tfvars"
    assert tfvars.exists(), "the offline plan inputs are missing"
    body = tfvars.read_text(encoding="utf-8")
    for forbidden in ("access_key", "secret_key", "session_token", "password"):
        assert forbidden not in body, (
            f"offline-plan.tfvars contains `{forbidden}`. It is committed, so "
            f"anything in it is in the repository."
        )


def test_the_planning_profile_is_off_by_default() -> None:
    """With the provider's pre-flight checks skipped, an apply against a
    misconfigured profile fails later and far less clearly than it otherwise
    would. The offline plan opts in; nothing else may inherit it."""
    for root in ENVIRONMENT_ROOTS:
        source = (root.parent / "variables.tf").read_text(encoding="utf-8")
        block = source[source.index('variable "planning_profile"'):]
        assert re.search(r"default\s*=\s*false", block), (
            f"{root.parent.name} defaults planning_profile to true"
        )

        environment = _source(root)
        for flag in (
            "skip_credentials_validation",
            "skip_requesting_account_id",
            "skip_region_validation",
            "skip_metadata_api_check",
        ):
            assert re.search(rf"{flag}\s*=\s*var\.planning_profile", environment), (
                f"{root.parent.name} does not wire {flag} to the planning "
                f"profile variable, so it is either always on or always off"
            )


# ═════════════════════════════════════════════════════════════════════════════
# NO GCP (spec-doc6 §13.5)
#
#   "Re-verify: zero GCP references in code, CI, IaC, docs or dependencies,
#    except deliberate legacy-URI handling, which must be explicitly commented
#    as such and covered by a test."
#
# THE DISTINCTION THIS ENFORCES, because a bare grep cannot make it: an
# EXECUTABLE reference ties the platform to Google Cloud; a COMMENT records why a
# control exists.
#
# The comments are not residue. `modules/secrets/variables.tf` explains its
# per-service scoping by naming the finding that produced it: one runtime
# identity holding every secret, where nothing was misconfigured and the grant
# was simply wider than the need. `scripts/verify-approval-gate.sh` exists
# because an environment with no required reviewer once auto-promoted. Deleting
# those sentences to satisfy a substring search would delete the reason the
# controls are shaped the way they are, and leave a checker that is happy about a
# codebase nobody can any longer explain.
#
# So this asserts the thing that actually matters: nothing INVOKES, IMPORTS or
# DEPENDS ON Google Cloud.
# ═════════════════════════════════════════════════════════════════════════════

#: The deploy surface. Not the whole repository: `docs/architecture/ESD.md` predates the
#: migration and `docs/operations/DATABASE_CREDENTIAL_MIGRATION.md` is a historical incident
#: record that opens by saying its commands will not work. Both are owned
#: elsewhere and both are honest about what they are.
_DEPLOY_SURFACE = (
    ROOT / "infra",
    ROOT / "scripts",
    ROOT / ".github",
    ROOT / "docs" / "DEPLOY_AWS.md",
)

#: Things whose presence would mean the platform still runs on, or reaches into,
#: Google Cloud. Each is matched against CODE with comments stripped.
_EXECUTABLE_GCP = (
    (r"\bgcloud\s+\w", "a gcloud CLI invocation"),
    (r"google-cloud-\w", "a google-cloud-* dependency"),
    (r"\.googleapis\.com", "a Google API endpoint"),
    (r'resource\s+"google_', "a Terraform google_* resource"),
    (r'source\s*=\s*"hashicorp/google"', "the Google Terraform provider"),
    (r"GOOGLE_APPLICATION_CREDENTIALS", "GCP application-default credentials"),
    (r"gcr\.io|pkg\.dev", "a Google container registry"),
)


def _strip_comments(text: str, path: pathlib.Path) -> str:
    """Remove comments so the assertion reads code rather than rationale."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    if path.suffix in {".py", ".sh", ".tf", ".yml", ".yaml", ".hcl"}:
        text = re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)
    if path.suffix in {".tf", ".hcl", ".ts", ".js"}:
        text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    if path.suffix == ".py":
        text = re.sub(r'"""[\s\S]*?"""', "", text)
    if path.suffix == ".md":
        # In a document, a fenced block is the executable part.
        return "\n".join(re.findall(r"```[a-z]*\n([\s\S]*?)```", text))
    return text


def _deploy_files() -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for target in _DEPLOY_SURFACE:
        if target.is_file():
            found.append(target)
            continue
        for path in target.rglob("*"):
            if not path.is_file() or ".terraform" in path.parts:
                continue
            if path.suffix in {".tf", ".hcl", ".sh", ".py", ".yml", ".yaml", ".md", ".json"}:
                found.append(path)
    return sorted(found)


def test_nothing_in_the_deploy_surface_invokes_or_depends_on_google_cloud() -> None:
    """spec-doc6 §13.5, asserted against CODE rather than against prose."""
    files = _deploy_files()
    assert len(files) > 20, "the deploy surface did not resolve; this would pass vacuously"

    offenders: list[str] = []
    for path in files:
        code = _strip_comments(path.read_text(encoding="utf-8", errors="replace"), path)
        for pattern, description in _EXECUTABLE_GCP:
            for match in re.finditer(pattern, code):
                offenders.append(
                    f"{path.relative_to(ROOT)} contains {description}: "
                    f"{match.group(0)!r}"
                )

    assert not offenders, "\n  ".join(["executable GCP references survive:"] + offenders)


def test_the_only_google_published_dependency_is_the_identity_provider() -> None:
    """`firebase-admin` is Google-published and is DELIBERATE, so it is named
    here rather than left to be re-discovered as a surprise.

    It is an identity provider, not a deployment dependency: CLAUDE.md rule 2
    makes Firebase the authentication mechanism for every role, and the backend
    verifies an ID token and then issues this product's own portal-scoped JWTs.
    Firebase remains authoritative for nothing but identity; roles and
    permissions live in this database. Moving the platform to AWS did not and
    should not change who verifies a Google sign-in.

    Everything else Google-published would be a deployment tie, so this pins the
    list at exactly one.
    """
    requirements = (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")
    google_published = {
        line.split("==")[0].split(">=")[0].strip()
        for line in requirements.splitlines()
        if line.strip() and not line.startswith("#")
        and re.search(r"^(google|firebase|gcloud)", line.strip(), re.IGNORECASE)
    }
    assert google_published == {"firebase-admin"}, (
        f"the Google-published dependencies are {sorted(google_published)}. "
        f"Only firebase-admin is deliberate, and it is the identity provider "
        f"rather than a deployment tie."
    )


def test_no_terraform_module_declares_a_google_provider() -> None:
    """The infrastructure is AWS-only. A `hashicorp/google` provider anywhere
    would mean an apply could reach into a Google project, which is a different
    claim from "we no longer deploy there"."""
    for path in ALL_TERRAFORM:
        source = path.read_text(encoding="utf-8")
        block = re.search(r"required_providers\s*\{([\s\S]*?)\n  \}", source)
        if block is None:
            continue
        assert "google" not in block.group(1), (
            f"{path.relative_to(ROOT)} declares a Google provider"
        )
