"""Application settings, sourced from environment variables (see /.env.example)."""

#: The wall clock the product is operated on, and the one every rendered time is
#: shown in. India, because that is where the product is sold and where the
#: recruiter reading "10:32" is standing.
#:
#: It lives HERE rather than on whatever happens to schedule work, which is
#: where it used to live: it was `celery_app.conf.timezone`, and the proctoring
#: report read it off the Celery app object so the two could not drift. That
#: coupling was right and the anchor was wrong, because it made a rendering
#: decision a property of the job runner. The scheduler now reads it too
#: (`workers/schedule`), so the same single definition still holds both.
PLATFORM_TIMEZONE = "Asia/Kolkata"

#: What an unpopulated secret holds in Secrets Manager, and what this module
#: turns back into "" before anything reads it.
#:
#: WHY IT EXISTS AT ALL. ECS fetches every secret in a task definition before
#: the container starts, so a secret with no version stops the whole service
#: rather than degrading one feature. Secrets Manager will not accept an empty
#: string, so `infra/modules/secrets` seeds each one with this instead.
#:
#: WHY IT IS NORMALISED HERE AND NOWHERE ELSE. Every "is this configured"
#: check in the product tests for a falsy value: `missing_delivery_keys`,
#: `llm_router.key_for_model`, `checkout_ready`, the Tavily gate. Handing any of
#: them a non-empty sentinel would make an unconfigured credential look
#: configured and fail later, at a provider, with a 401 that reads like a
#: revoked key. Turning it into "" at the boundary means the code that runs is
#: exactly the code that runs when the variable was never set.
#:
#: It is NOT a credential and cannot authenticate to anything. Pinned against
#: the Terraform default by `tests/test_placeholder_secret.py`.
PLACEHOLDER_SECRET = "PLACEHOLDER_NOT_CONFIGURED"

#: The development JWT signing key. A module constant rather than a field, so a
#: production guard can refuse it BY IDENTITY rather than guessing at length or
#: entropy -- and so the default and the thing that rejects it cannot drift.
DEV_JWT_SECRET = "dev-only-secret-change-me"

#: The coding languages the product knows how to offer. A deployment chooses a
#: subset in `CODE_EXECUTION_LANGUAGES`; a key outside this set is refused at
#: boot. `services/code_execution/languages.py` holds one spec per key and a
#: test pins the two against each other.
CODE_EXECUTION_LANGUAGE_KEYS = ("python", "java", "cpp", "javascript")

#: The two real code-execution backends. See `Settings.code_execution_backend`.
CODE_EXECUTION_BACKENDS = ("judge0", "disabled")


def _parse_pairs(raw: str, setting: str) -> list[tuple[str, str]]:
    """Split a "key:value,key:value" setting, refusing a malformed entry.

    Empty input is an empty list. A duplicate key is refused rather than
    letting the later one win silently.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in (raw or "").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        key, sep, value = entry.partition(":")
        key, value = key.strip().lower(), value.strip()
        if not sep or not key or not value:
            raise ValueError(f"{setting} entry {entry!r} is not key:value")
        if key in seen:
            raise ValueError(f"{setting} names {key!r} twice")
        seen.add(key)
        pairs.append((key, value))
    return pairs

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict



#: S3's floor for every part of a multipart upload except the last, a fact of
#: the S3 API rather than a tunable (AWS, "Amazon S3 multipart upload limits":
#: part size 5 MiB to 5 GiB, the last part may be smaller). One definition,
#: read by the settings validator and by `services/video/storage`.
S3_MIN_PART_BYTES = 5 * 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://pickready:pickready@localhost:5432/pickready"
    postgres_rls_app_role: str = "pickready_app"
    # The role migrations SET ROLE to before running DDL, or empty to skip.
    #
    # WHY THIS EXISTS (2026-09-11). `DATABASE_URL` used to carry the RDS MASTER
    # credential, which `manage_master_user_password = true` hands to Secrets
    # Manager to ROTATE on a schedule. AWS rotated it seven days after the pilot
    # instance was created, the hand-composed DSN kept the old password, and
    # every database connection in the product failed at once: the API, the
    # health probe, and therefore sign-in. The DSN now carries the least
    # privileged application role (`postgres_rls_app_role`), which owns no
    # object and whose password nothing else rotates, exactly as
    # `infra/modules/rds` has documented the design from the start.
    #
    # That role deliberately has no DDL rights, so the migration job, and ONLY
    # the migration job, escalates to the object owner for the length of its
    # connection. It can, because the login role is a NOINHERIT member of the
    # owner: membership permits `SET ROLE` while NOINHERIT means an ordinary
    # application session holds none of the owner's privileges. Left unset in
    # every runtime that serves traffic, so the escalation is reachable from one
    # container role and not from the API.
    postgres_migration_role: str = ""

    # Connection pool (app/core/db.get_engine). The SQLAlchemy defaults (5 + 10)
    # are small enough that a few concurrent tabs queue for a connection and
    # every request in the queue reads as "slow".
    #
    # THE CEILING IS THE DATABASE'S, AND THE AUTOSCALER COULD EXCEED IT.
    # ------------------------------------------------------------------
    # `db.t4g.micro` has 1 GiB, and RDS derives `max_connections` as
    # LEAST(DBInstanceClassMemory/9531392, 5000), which is about 112, of which
    # three are reserved for the superuser: roughly 109 usable.
    #
    # At 20 + 10 the previous values, four API tasks alone want 120. The ECS
    # target-tracking policy scales to `local.service_count * 2` = 4 on CPU, so
    # the failure was reachable BY LOAD: the autoscaler's response to traffic
    # was what took the database out. And it does not fail as a pool timeout
    # that sheds one request -- Postgres answers `FATAL: sorry, too many
    # connections`, `/health` probes the database too, all four tasks leave the
    # target group, and the product is fully down.
    #
    # 12 + 3 gives 4 x 15 = 60 at the autoscaler's own maximum, leaving room
    # for the Lambda workers (one engine each, account concurrency 10) and the
    # on-demand Fargate agents, which are unbounded and take one engine each.
    #
    # These are the numbers for THIS instance class. Moving to a larger one is
    # the other half of the trade and raises them; the rule is that the product
    # of tasks and (pool_size + max_overflow) stays under the usable ceiling
    # with headroom for the workers, not that these two integers are sacred.
    db_pool_size: int = 12
    db_max_overflow: int = 3
    db_pool_timeout_seconds: int = 30
    db_pool_recycle_seconds: int = 1800

    # Redis. Cache, rate limiting, the proctoring warning counter and the
    # background run-status record. It is no longer a message broker: there is
    # no queue in it, and nothing consumes from it.
    redis_url: str = "redis://localhost:6379/0"
    #: Hours a job may sit with a Sutra skills draft nobody has saved before
    #: the people who can save it are reminded, once
    #: (`pickready.remind_unsaved_skills`). Renamed in the Vivekium release:
    #: the old name described the technical question bank deleted on
    #: 2026-08-06.
    skills_setup_reminder_hours: int = 48
    #: Minutes a Job SWOT generation may stay `generating` before a reader
    #: treats it as failed. Derived at read time, never written: a worker that
    #: never reported back must not leave a spinner on the SWOT tab for ever.
    swot_generation_stale_minutes: int = 5

    # -- Consent renewal and the inactivity rule (feature 8) ------------------
    #
    # The windows. `services/consent_lifecycle` REFUSES a non-positive value,
    # because a zero window puts every candidate past every threshold the
    # instant they register, and the thing on the other side of these numbers
    # is the permanent erasure of a real person's profile.
    consent_renewal_months: int = 6
    consent_grace_days: int = 15
    consent_inactivity_months: int = 24

    # THE SWEEP WRITES TO PEOPLE BY DEFAULT AND ERASES THEM ONLY WHEN THIS IS
    # ON, and the split is deliberate. Reminders are reversible and are the
    # candidate's own interest; deletion is neither.
    #
    # Same shape as `proctoring_event_retention_days`, which is zero by default
    # and whose sweep LOGS that it is deleting nothing so an operator can see
    # the policy in force rather than inferring it from silence. Off here means
    # the lifecycle runs, the letters go out, and the erasure step reports what
    # it WOULD have erased. Turning it on is an owner decision, and it should
    # not be taken until `last_engagement_at` has been recording for longer
    # than `consent_inactivity_months`: before that, every dormancy answer is
    # computed from registration, which is the safe direction for the
    # measurement and the wrong direction for the deletion.
    consent_auto_deletion_enabled: bool = False

    # Whether to install the per-request timing and query-count middleware.
    #
    # OPT IN, DEFAULT OFF, AND NOT DERIVED FROM `is_production`. It used to be
    # `if not get_settings().is_production`, and `instrumentation.py` still
    # said in place that the middleware was "unreachable in production because
    # [it] is not installed there". It was installed there. `is_production` is
    # `environment == "production"` and the live deployment runs
    # `ENVIRONMENT=pilot`, so the gate was open on the one environment it was
    # written to close. That is the same root cause as the unsigned-webhook
    # hole and the exposed API docs, which is why this one is not fixed by
    # swapping in another derived property.
    #
    # `serves_over_https` would be wrong too: it is true of staging, where
    # these diagnostics are wanted. So this is DEPLOYMENT DATA, the shape
    # `email_transport` and `task_dispatch_backend` already use, with the
    # default chosen so an environment nobody has thought about is safe.
    #
    # What it exposes when on: `Server-Timing` carrying an app and a SQL
    # duration, `X-Query-Count`, and an `X-Debug-SQL: 1` request header that
    # makes the server log every statement the request ran. The statement text
    # only, never its bound parameters, so no candidate value reaches the log;
    # the timing is the sharper half, because this codebase uses
    # `hmac.compare_digest` precisely to deny the measurement that a
    # server-computed `sql;dur` hands out with the network jitter removed.
    expose_request_diagnostics: bool = False

    # How long an employer's verification form link stays usable, counted from
    # when the request row was written (which is the same request that
    # dispatches the email, so it is the send date).
    #
    # IT HAD NO EXPIRY AT ALL. The link was single-use, and that is a different
    # property: a link nobody ever used stayed valid for ever, in a third
    # party's mailbox, in every mailbox that message was ever forwarded to, and
    # in whatever archive that mail system keeps. The same argument
    # `INBOUND_WEBHOOK_SECRET` already makes about thread tokens.
    #
    # THREE DAYS IS SAFE HERE ONLY BECAUSE THERE IS A DOCUMENTED WAY OUT.
    # `POST /verification/requests/{id}/override` is, in its own docstring,
    # "the only way a fresh candidate moves forward without an employer
    # response", and it takes a reason and writes an audit row. Without that an
    # expiry would convert a stale-credential risk into a dead end for a
    # candidate who did nothing wrong, which is the worse failure.
    verification_link_ttl_days: int = 3

    # -- A fresher's own verification documents (migration 0114) -------------
    #
    # The brief gives a fresher academic certificates and address proof in
    # place of employer BGV. Every ceiling on that upload is DATA here rather
    # than a literal in the pipeline, the rule `project_*` already follows:
    # a candidate-supplied file is hostile input, and a bound somebody can
    # only find by reading the parser is a bound nobody can change safely.
    bgv_document_max_bytes: int = 10 * 1024 * 1024
    #: Per candidate PER TYPE. A degree, a diploma and a consolidated marksheet
    #: are three legitimate academic certificates; thirty is an abuse surface.
    bgv_documents_max_per_type: int = 6

    # -- Background task dispatch --------------------------------------------
    #
    # Three real deployments, never a fallback chain (see workers/dispatch):
    #   aws     invoke Lambda, which runs short work directly and starts an
    #           on-demand Fargate task for long work. Staging and production.
    #   local   run the task in a thread in this process. The docker-compose
    #           stack, which has no Lambda.
    #   record  accept the dispatch, run nothing, remember it. The test suite,
    #           where executing a task would make every route test depend on a
    #           model provider. Refused outright in production.
    task_dispatch_backend: str = "aws"

    # How long a synchronous agent invoke may take before the CLIENT gives up.
    # Deliberately longer than the functions' own timeouts, so the function's
    # ceiling is what stops the work: a client that times out first abandons a
    # call that is still running and, with retries enabled, starts a second.
    agent_invoke_read_timeout_seconds: int = 660

    # Auth
    #: Refused in production by `_refuse_an_unconfigured_jwt_secret`.
    jwt_secret: str = DEV_JWT_SECRET
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 7
    firebase_service_account_json: str = ""

    # Auth-cookie attributes (app/api/deps.py is the single writer).
    #
    # "strict" is correct whenever the browser page and the API are same-site,
    # which covers local dev (localhost:3000 -> localhost:8000; cookies ignore
    # the port) and a production deployment on one registrable domain
    # (app.example.com + api.example.com). It is WRONG when the two live on
    # different registrable domains (a Vercel frontend calling a Railway API):
    # the browser then withholds the cookie on every call and the user can never
    # stay signed in. That deployment must set COOKIE_SAMESITE=none, which the
    # validator below forces to be paired with a secure (https) cookie.
    #
    # COOKIE_DOMAIN is normally empty (host-only cookie, the safest default).
    # Set it to ".example.com" only when the frontend and API are on different
    # subdomains of one domain.
    cookie_samesite: str = "strict"
    cookie_domain: str = ""

    # The three login-code settings (ttl, attempts, cooldown) were REMOVED with
    # the code-login flow on 2026-09-24, not left behind as dead settings. A
    # setting nothing reads is one an operator will eventually tune expecting
    # an effect. Firebase owns identity; `.env.example` parity is pinned by
    # `tests/test_env_example_parity.py`.

    # ── LLM and embeddings ──────────────────────────────────────────────────
    #
    # THREE CREDENTIALS PLATFORM-WIDE, ONE PER MODEL. This replaced a 21-slot
    # roster (7 keys each for Groq, Gemini and OpenRouter) that existed to route
    # around three free tiers' failure modes -- a retired model id, an exhausted
    # prepaid balance, an 8000-token-per-minute organisation pool, a withdrawn
    # free-tier model. One paid vendor removes that whole class of problem, so
    # it removes the roster with it.
    #
    # THE MODEL VENDOR CHANGED ON 2026-08-31, from Anthropic to OpenAI, by owner
    # decision. `ANTHROPIC_API_KEY` is gone rather than deprecated: a retained
    # credential is a credential something eventually reads. The embedding
    # VENDOR did not change; the variable its key is read from did.
    #
    # TWO KEYS FOR ONE MODEL VENDOR is unusual and is what the owner has: the
    # reasoning tier and the extraction tier are billed separately. Which model
    # is called with which is DATA in `config/llm_providers.SETTINGS_ATTR_FOR_MODEL`,
    # never a branch in the router, and an absent key for the model being called
    # raises rather than falling back to the other one.
    #
    # All three are mounted from AWS Secrets Manager in a deployed environment
    # and are never composed into a loggable env var, continuing the discipline
    # established when DATABASE_URL was hardened.
    openai_gpt_terra: str = ""
    openai_gpt_luna: str = ""

    # THE EMBEDDING CREDENTIAL IS NAMED AFTER THE MODEL IT UNLOCKS:
    # `VOYAGE_CONTEXT_4` for `voyage-4`. Same convention as the two
    # model keys above, and it replaces `VOYAGE_API_KEY` outright rather than
    # sitting beside it as an alias. One name per thing.
    #
    # READING THE WRONG NAME HERE WOULD NOT RAISE, which is why
    # `tests/test_embeddings.py` pins this exact variable as a literal.
    # `services/embeddings` falls back to deterministic pseudo-random unit
    # vectors when the key is absent, so a mistyped name leaves every retrieval
    # returning meaningless vectors of the right width, with no exception, no
    # log line and no empty result to notice.
    voyage_context_4: str = ""

    # `llm_key_encryption_secret` WAS DELETED ON 2026-09-24. Nothing in the tree
    # read it: the router that decrypted the multi-vendor key roster went with
    # that roster, and migration 0128 drops the roster's table (only when it is
    # empty). A setting kept "as key material" is a setting that reads as live.
    # The KEY MATERIAL is not here and never was: it is the
    # `LLM_KEY_ENCRYPTION_SECRET` container in Secrets Manager, which
    # `infra/modules/secrets` still creates and grants to no service, because
    # destroying a secret is irreversible once its recovery window passes and
    # is the owner's decision (CONTRACT v2), not a side effect of a cleanup.

    # Embedding output width. Pinned to 1024 because `profiles.embedding`,
    # `jobs.embedding` and `context_chunks.embedding` are vector(1024) columns
    # already holding vectors. Changing this is a re-embed of every row, not a
    # config change.
    embedding_dimensions: int = 1024

    # Advanced web search for the BD Portal's AI Reach agent
    # (services/web_research.py). OPTIONAL: with no key the "from the internet"
    # segment returns an empty list with status "unconfigured" and a plain
    # message, while "similar to our customers" keeps working.
    tavily_api_key: str = ""

    # Gmail SMTP only. Values come from the environment and are validated below.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "noreply@pickready.app"
    smtp_from_name: str = "Vivekium"
    smtp_starttls: bool = True
    smtp_ssl: bool = False

    # ── Where a reply comes back to ──────────────────────────────────────────
    #
    # The domain SES receives mail for, and the domain a conversation's
    # Reply-To is built on: `conversations+<thread_token>@<this>`. A SUBDOMAIN,
    # never the apex, because receiving mail means owning the MX record and the
    # apex's MX belongs to whatever mailbox the company actually reads.
    #
    # EMPTY MEANS NO REPLY-TO IS SET, and that is a real state rather than a
    # broken one: a deployment that has not provisioned inbound mail still
    # sends verification requests, and the employer's reply lands in the
    # sender's own mailbox instead of in the thread. The code SAYS so where it
    # matters rather than quietly producing a thread that can never receive
    # anything (`conversations.reply_address` returns None and the caller logs
    # the degradation once).
    inbound_email_domain: str = ""

    # THE SHARED SECRET BETWEEN THE INBOUND-MAIL LAMBDA AND THIS API.
    #
    # `POST /verification/inbound-email` is a PUBLIC route that writes into
    # verification requests, BGV threads and conversations. Its only protection
    # was that a caller had to know a per-thread token, which travels by email
    # and is therefore in every mailbox that ever received or forwarded one of
    # these threads. Nothing stopped a POST straight at the API, bypassing SES,
    # the DKIM and SPF checks, and the Lambda entirely.
    #
    # The sibling webhooks already do this properly: the SES event webhook
    # verifies an SNS RSA signature, and the Razorpay webhook verifies an HMAC.
    # This one is Lambda to API, so a shared secret is enough; it does not need
    # to prove SES sent the mail, only that OUR relay made the call.
    #
    # EMPTY IS A REAL STATE and is not silently equivalent to configured. With
    # no secret set the route stays open and SAYS SO in the log, which is the
    # same shape `inbound_email_domain` already uses: a deployment that has not
    # been given the value behaves as it did before rather than refusing every
    # genuine reply, and the operator can see which of the two they are in.
    inbound_webhook_secret: str = ""

    # HOW OFTEN A CANDIDATE IS TOLD ABOUT NEW MESSAGES (Phase 6 WP6-C).
    #
    # A recruiter writing to a candidate produces one email and one Updates
    # entry. Five messages in a burst produce ONE: the next notification waits
    # until this many minutes have passed since the last, and reading the
    # thread re-arms it immediately. Zero notifies on every message.
    candidate_message_notify_debounce_minutes: int = 10

    # ── Outbound email transport (Corporate Email System spec section 6) ────
    #
    # ONE transport per deployment, selected by DATA, never a fallback chain
    # (the same discipline the model layer keeps). "smtp" is the existing Gmail
    # path, unchanged; "ses" sends through Amazon SES via boto3 with the same
    # permanent/transient failure taxonomy. Owner decision 2026-09-05: this
    # narrows "Gmail SMTP is the only outbound mail path" to
    # transport-as-deployment-data.
    email_transport: str = "smtp"
    #: The SNS topic SES publishes delivery/bounce/complaint events to. The
    #: webhook refuses any message whose TopicArn differs (spec section 8).
    #: Empty means the event endpoint refuses everything.
    ses_sns_topic_arn: str = ""
    #: The SES configuration set every send is attached to. THIS IS WHAT MAKES
    #: DELIVERY TRACKING EXIST: SES publishes an event only for a message sent
    #: under a configuration set carrying an event destination, so a send
    #: without this name attached is one whose outcome nobody ever learns.
    #: Empty sends without one, which is correct for a deployment that has not
    #: provisioned the set: the message is still delivered and the row simply
    #: stays at `sent` rather than pretending to a delivery it cannot observe.
    ses_configuration_set: str = ""

    # ── Platform security email (auth hardening, 2026-09-29) ────────────────
    #
    # The From identity of every security email this product sends on its
    # own behalf: a security code for registration or a password change, and
    # the notice that an address already has an account. NEVER a tenant's
    # corporate sender and never an address a request supplied
    # (`services/security_email`). Under SES the domain must be a verified
    # identity in the account; in production a non-SES transport is refused
    # outright rather than falling back to SMTP.
    platform_security_sender_email: str = "contact@readypick.ai"
    platform_security_sender_name: str = "Vivekium"

    # ── Security codes (registration and password change or reset) ─────────
    #
    # Six digits, stored only as an HMAC in Redis (`services/security_codes`).
    # How long a code lives, how many wrong entries end it, and how soon a
    # second one may be asked for (a new code REPLACES the previous one).
    security_code_ttl_seconds: int = 600
    security_code_max_attempts: int = 5
    security_code_resend_seconds: int = 60

    # ── Corporate sender registration (Corporate Email System spec) ─────────
    #
    # Free/personal mail providers may never be registered as a corporate
    # sender (spec section 2). Comma-separated so an operator can extend it
    # from the environment without a deploy; matching also refuses SUBDOMAINS
    # of a blocked domain, so mail.yahoo.com cannot slip past.
    sender_domain_blocklist: str = (
        "gmail.com,googlemail.com,yahoo.com,yahoo.co.in,ymail.com,outlook.com,"
        "hotmail.com,live.com,msn.com,icloud.com,me.com,mac.com,proton.me,"
        "protonmail.com,aol.com,gmx.com,gmx.net,mail.com,rediffmail.com,"
        "rediff.com,zoho.com,zohomail.com,yandex.com,yandex.ru,fastmail.com,"
        "tutanota.com,tuta.com,hey.com,mail.ru,inbox.com,hushmail.com"
    )
    # The three sender-OTP knobs (ttl, max attempts, resend cooldown) were
    # REMOVED with the mailbox OTP on 2026-09-08, not left as dead settings. A
    # setting nothing reads is one an operator will eventually tune expecting
    # an effect; SES identity verification is the ownership check now.

    def sender_blocked_domains(self) -> frozenset[str]:
        """The blocklist, parsed once per call: lowercased, trimmed, non-empty."""
        return frozenset(
            part.strip().lower()
            for part in self.sender_domain_blocklist.split(",")
            if part.strip()
        )

    # ── Private file storage (S3) ───────────────────────────────────────────
    #
    # Durable values stored in the database are s3:// object references; a raw
    # bucket URL is never returned to a browser. Access always passes through an
    # authenticated, tenant-scoped, capability-checked endpoint rather than a
    # presigned link, because a presigned URL is a bearer token that leaves no
    # audit trail once it has been copied out of a page.
    #
    # No access key or secret lives here. In a deployed environment boto3
    # resolves the ECS task role, which is scoped to exactly this bucket by the
    # per-service IAM policy in `infra/modules/secrets`. A long-lived key in an
    # env var is precisely what that scoping exists to avoid.
    s3_bucket: str = ""
    # ap-south-2 (Hyderabad). The deployment region is a locked decision;
    # every bucket, function, cluster and secret this product addresses
    # lives there, so a wrong default here is a set of NoSuchBucket and
    # ResourceNotFound errors that read like missing resources.
    aws_region: str = "ap-south-2"
    #: Localstack / MinIO only. None in every real environment, where boto3
    #: resolves the real regional endpoint.
    s3_endpoint_url: str = ""
    #: The KMS key assessment media is encrypted under (Terraform output
    #: `aws_kms_key.this.arn`, exported as S3_KMS_KEY_ID). The bucket policy in
    #: `infra/modules/s3` DENIES any PutObject whose
    #: `x-amz-server-side-encryption` header is not `aws:kms`, and a request
    #: that names `aws:kms` without a key id is encrypted under the AWS-managed
    #: `aws/s3` key rather than this environment's key. So `video/storage`
    #: sends both, and REFUSES to write media while this is empty rather than
    #: writing it under the wrong key or being denied one part at a time.
    s3_kms_key_id: str = ""
    resume_signed_url_ttl_seconds: int = 300

    # ── Proctoring (proctoring-spec-doc.md) ─────────────────────────────────
    #
    # EVERY THRESHOLD THE PROCTORING SYSTEM USES IS HERE, AND NOWHERE ELSE.
    # The specification's "defaults" are starting configuration, not
    # constants, so each one is a setting an operator can move without a code
    # change. `services/proctoring/config.py` reads them into one frozen
    # object and serves the browser-side subset to the client at session
    # start, which is what keeps the client and the server working from the
    # same numbers. No module in the proctoring pipeline may carry a literal.
    #
    # Proctoring is MANDATORY (principle P4). There is no enable flag: a
    # candidate who declines the consent screen does not take the assessment.
    # RE-AFFIRMED by the owner on 2026-09-22 ("Proctoring remains mandatory"),
    # which is also the refusal of the pending request for a per-job on/off
    # toggle. Do not add `proctoring_enabled`. The one feature flag below
    # governs the AI-text detector only, because that signal is documented as
    # unreliable and ships disabled.
    #
    # SUPERSEDED 2026-09-22, principle P1 ONLY. This block used to be read
    # alongside "no media is ever stored"; the owner reversed that in the same
    # ruling: "Media storage is required. The assessment video must be
    # compressed and stored securely in S3, linked to the candidate
    # assessment." The media itself is NOT configured here, because it is not
    # a proctoring artifact: it is an assessment recording, it lives on
    # `video_recordings`, and its ceilings are the `video_*` settings further
    # down. What is still true of every setting in THIS block is that no
    # module in the proctoring pipeline carries a literal.
    proctoring_max_warnings: int = 3
    # Object detection (section 3.1, 4.2).
    proctoring_object_confidence_threshold: float = 0.65
    proctoring_object_consecutive_frames: int = 3
    proctoring_object_cooldown_seconds: int = 30
    proctoring_second_person_cooldown_seconds: int = 30
    # Face identity (section 3.3, 4.1).
    proctoring_face_distance_threshold: float = 0.6
    proctoring_identity_check_interval_seconds: int = 30
    proctoring_identity_consecutive_mismatches: int = 2
    # Camera obstruction versus face absence (section 4.1, 4.2, 4.6).
    proctoring_obstruction_seconds: int = 60
    #: Per-frame greyscale standard deviation below which a frame with no face
    #: is an obstruction (covered lens, tape, closed shutter) rather than an
    #: absence. Computed in the browser from the frame it then discards.
    proctoring_obstruction_variance_threshold: float = 12.0
    proctoring_face_absent_moderate_seconds: int = 20
    proctoring_face_absent_moderate_cooldown_seconds: int = 60
    proctoring_face_absent_extended_seconds: int = 90
    # Browser lockdown and focus (section 4.2).
    proctoring_focus_loss_ignore_under_seconds: float = 2.0
    proctoring_display_check_interval_seconds: int = 60
    # Audio (section 3.4, 4.2).
    proctoring_audio_chunk_seconds: int = 15
    proctoring_audio_max_chunk_bytes: int = 2 * 1024 * 1024
    proctoring_second_voice_consecutive_chunks: int = 2
    #: A second voice is flagged only when it is STRONG (master prompt, Phase
    #: 3): the second-loudest speaker the diarizer separates must have spoken
    #: for at least this many seconds of one chunk. A split of the
    #: candidate's own voice, a cough or a passing voice in the corridor is
    #: shorter than this and counts for nothing.
    proctoring_second_voice_min_seconds: float = 3.0
    #: Speech in a chunk at or above this many seconds, while no spoken answer
    #: is being captured, is "speaking during a question that does not take a
    #: spoken answer". Logged every time, never a warning, never a
    #: termination.
    proctoring_speech_min_seconds: float = 2.0
    #: From this many occurrences the report lifts the speaking finding into
    #: its summary.
    proctoring_speech_highlight_threshold: int = 3
    #: The analysis service (speaker diarization, AI-text detection). Empty
    #: means audio analysis is UNAVAILABLE, which the report states plainly;
    #: it is never silently treated as "no second voice".
    proctoring_analysis_service_url: str = ""
    proctoring_analysis_timeout_seconds: float = 20.0
    # Anti-tamper (section 9).
    proctoring_heartbeat_interval_seconds: int = 10
    proctoring_heartbeat_gap_seconds: int = 30
    proctoring_integrity_failure_termination_seconds: int = 60
    # Camera or microphone loss (master prompt, Phase 3). A loss PAUSES the
    # assessment and gives the candidate this many seconds to restore the
    # device; at most `device_max_pauses` pauses per session, and the next
    # loss, or a pause not recovered in time, ends it as a technical failure.
    # A loss shorter than the glitch window is logged and pauses nothing.
    # SUPERSEDES `proctoring_camera_recovery_seconds`, which turned an
    # unrecovered camera into an immediate termination.
    proctoring_device_max_pauses: int = 2
    proctoring_device_grace_seconds: int = 120
    proctoring_device_glitch_seconds: int = 5
    # In-browser inference performance (section 3.6).
    proctoring_sampling_fps_normal: int = 2
    proctoring_sampling_fps_confirming: int = 6
    proctoring_confirming_window_seconds: int = 5
    proctoring_sampling_fps_degraded: int = 1
    proctoring_low_light_luminance_threshold: float = 40.0
    proctoring_low_light_cooldown_seconds: int = 300
    # Behavioural capture (section 4.5). Thresholds compare the candidate
    # against THEIR OWN baseline from their first answers, never a population.
    proctoring_baseline_answers: int = 2
    proctoring_fast_entry_multiplier: float = 3.5
    proctoring_fast_entry_sustained_seconds: int = 10
    proctoring_uniform_span_chars: int = 200
    proctoring_uniform_max_corrections: int = 5
    proctoring_uniform_max_pause_seconds: float = 1.0
    proctoring_low_ratio_min_length: int = 150
    proctoring_low_ratio_threshold: float = 0.85
    proctoring_pause_gap_seconds: float = 2.0
    proctoring_burst_window_seconds: int = 5
    proctoring_mouse_sample_hz: int = 10
    proctoring_max_keystroke_samples: int = 20_000
    proctoring_event_batch_max: int = 200
    # AI-generated-text detection (section 3.5). INFORMATIONAL ONLY and
    # disabled by default: the detectors are unreliable against current
    # models, and the signal never contributes to a warning, a termination, a
    # score or a ranking whatever this flag says.
    proctoring_ai_text_detection_enabled: bool = False
    proctoring_ai_text_threshold: float = 0.9
    proctoring_ai_text_min_chars: int = 200
    #: Event retention (section 5). ZERO means "the platform's existing
    #: candidate-data policy", which is deletion by cascade when the candidate
    #: or application is deleted; the platform has no time-based purge and
    #: this setting does not invent one. A positive value enables the hourly
    #: purge of events older than that many days. Owner decision.
    proctoring_event_retention_days: int = 0

    # ── Code execution (Phase 4, `services/code_execution`) ─────────────────
    #
    # CANDIDATE CODE NEVER RUNS IN THIS PROCESS. These settings describe a
    # sandbox on a separate host that holds no application secret; the backend
    # only talks to it over HTTP inside the VPC.
    #
    # ONE BACKEND PER DEPLOYMENT, NEVER A FALLBACK CHAIN, the same shape as
    # `email_transport` and `TASK_DISPATCH_BACKEND`. `disabled` is the default
    # and a real state: every surface records "code execution unavailable"
    # rather than pretending a program ran. The test double is NOT selectable
    # here; it is installed only through `code_execution.override_provider`,
    # which refuses in production.
    code_execution_backend: str = "disabled"
    #: The languages a coding question may be written in. Keys only; every key
    #: must be one of `CODE_EXECUTION_LANGUAGE_KEYS`.
    code_execution_languages: str = "python,java,cpp,javascript"
    #: Per-run limits sent with EVERY submission. The sandbox's own MAX_* caps
    #: (`infra/modules/code_sandbox/judge0.conf.tftpl`) are the second fence;
    #: `code_execution.limits.HOST_MAXIMA` mirrors them and a test pins both.
    code_execution_cpu_seconds: float = 2.0
    code_execution_cpu_extra_seconds: float = 0.5
    code_execution_wall_seconds: float = 5.0
    code_execution_memory_mb: int = 256
    code_execution_stack_kb: int = 65536
    #: Bounds a fork bomb. JVM threads count against it, hence not single digits.
    code_execution_max_processes: int = 60
    #: Bounds stdout on the host: the sandbox writes stdout to a file, so the
    #: file-size limit IS the output limit.
    code_execution_max_file_kb: int = 1024
    #: How much of stdout, stderr and compiler output the app keeps. Display
    #: truncation only; the enforcement is `code_execution_max_file_kb`.
    code_execution_max_output_chars: int = 4000
    #: Per-language CPU and wall multipliers ("key:factor,..."). A JVM start
    #: costs more than a Python start and must not read as a slow solution.
    code_execution_cpu_multipliers: str = "java:2.0,javascript:1.5"
    #: HTTP bounds for every sandbox call. An unreachable host that HANGS
    #: defeats every error handler around it, so both are explicit.
    code_execution_connect_timeout_seconds: float = 2.0
    code_execution_request_timeout_seconds: float = 5.0
    code_execution_poll_seconds: float = 0.5
    #: The sandbox's address inside the VPC, e.g. a Cloud Map name. Empty means
    #: unavailable, which is NOT a boot refusal: an optional feature must not
    #: take the API down. `code_execution.is_enabled()` answers False instead.
    judge0_url: str = ""
    #: Mounted from Secrets Manager as `JUDGE0_AUTH_TOKEN`
    #: (`<project>-<environment>/JUDGE0_AUTH_TOKEN`, created by
    #: `infra/modules/code_sandbox`). Sent as both the authentication and the
    #: authorization header; never logged.
    judge0_auth_token: str = ""
    #: Adapter-only language ids ("key:id,..."). Checked against the sandbox's
    #: own `GET /languages` by the operator verification task.
    judge0_language_ids: str = "python:71,java:62,cpp:54,javascript:63"

    # ── Siddhi citation support (Vivekium release, WP5-C) ──────────────────
    #: The cosine similarity, between a report statement and the passage it
    #: cites, at or above which the SEMANTIC tier of `siddhi.support` calls the
    #: statement supported. Consulted only when the deterministic anchor finds
    #: no shared content term, so it rescues a paraphrase and never overrides an
    #: invented term. ASSUMPTION (owner question O5-4, accepted in CONTRACT v2):
    #: 0.55 over voyage-4 document vectors; reversible here without a deploy of
    #: new code.
    siddhi_support_similarity_min: float = 0.55

    # ── Miti's bounded retry of a not-assessed run (PLAN-p5 P5-D4, WP5-D) ────
    #: A scoring run in which some skill could not be assessed (a model
    #: failure on every substantive answer, a coding result the sandbox never
    #: produced) writes NO report on attempts before this one: the hourly
    #: `pickready.release_held_assessments` sweep re-dispatches it. On this
    #: attempt the report IS written, those skills stated "Not assessed", the
    #: report routed to a person, and `miti.not_assessed_final_report` logged
    #: at ERROR for the CloudWatch alarm. A report is permanent, so a
    #: two-minute outage must not become a permanent "Not assessed"; the bound
    #: stops a permanent state re-paying the other skills' evaluations forever.
    miti_not_assessed_attempts: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _siddhi_support_similarity_in_range(self) -> "Settings":
        """A cosine floor outside (0, 1] is a verdict decided by configuration.

        Above one nothing can reach it, so every paraphrase is `unsupported`
        and every report goes to review; at or below zero every unrelated
        sentence is `supported`. Both would look like the check working.
        """
        if not 0.0 < self.siddhi_support_similarity_min <= 1.0:
            raise ValueError(
                "SIDDHI_SUPPORT_SIMILARITY_MIN must be above 0 and at most 1"
            )
        return self

    # ── Coding question generation (Phase 4, `assessment_formats/coding_generation`)
    #
    # A coding question is accepted only after its model-written reference
    # solution has passed every one of its own tests IN THE SANDBOX. These are
    # the bounds of what the model is asked for and of the validation.
    #: Sample tests the candidate sees and may Run against.
    coding_visible_tests_min: int = 2
    coding_visible_tests_max: int = 3
    #: Tests the final submission is graded against. Never shown to anybody.
    #: The database refuses more than 30 per question.
    coding_hidden_tests_min: int = 5
    coding_hidden_tests_max: int = 10
    #: The generation loop's wall clock, across every attempt. Larger than the
    #: generic background loop's, because one attempt is a long JSON document
    #: from the model PLUS sandbox runs of the reference and every starter.
    coding_generation_deadline_seconds: float = 420.0
    #: One sandbox validation run (submit, poll, collect) may take this long.
    coding_validation_deadline_seconds: float = 60.0
    #: The reference solution may use at most this fraction of a test's CPU
    #: limit. A reference that nearly times out makes a correct candidate's
    #: solution fail on a slower moment of the same host.
    coding_reference_cpu_headroom: float = 0.5

    # ── Coding Run, Submit, review and scoring (Phase 4 WP-4B2, `coding_assessment`)
    #
    # Run is interactive and bounded; Submit is final, stored first and
    # executed by a dispatched task after commit. These are its bounds.
    #: The HARD cap on Run presses per question, counted in `coding_runs`.
    #: The Redis rate window fails open by design, so this count is the fence
    #: that holds when Redis is down.
    coding_run_max_per_question: int = 40
    #: The Run routes' Redis rate windows, per candidate per minute (the API,
    #: WP-4C). Abuse and cost control, not the fence: the window fails open
    #: when Redis is down, and `coding_run_max_per_question` is what holds.
    #: A poll is cheap but performs one bounded sandbox fetch, so it has a
    #: window of its own sized for the editor's backoff (half a second rising
    #: to two).
    coding_run_rate_per_minute: int = 20
    coding_run_poll_rate_per_minute: int = 120
    coding_submission_state_rate_per_minute: int = 30
    #: A queued Run whose sandbox has not answered for this long is recorded
    #: as unavailable, so a poll that keeps failing cannot leave the Run
    #: button disabled for the rest of a twenty-minute question.
    coding_run_deadline_seconds: float = 60.0
    #: How long one submission task polls the sandbox for its hidden tests
    #: before it records the attempt and hands back to the retry loop. The
    #: ticket is committed first, so a later attempt COLLECTS, never resubmits.
    coding_submission_poll_deadline_seconds: float = 120.0
    #: The sweep re-dispatches an execution still open after this long.
    coding_submission_redispatch_minutes: int = 5
    #: The sweep retries a failed or unstarted code-quality review after this.
    coding_review_retry_minutes: int = 15
    #: The share of a coding question's score that comes from the hidden tests
    #: (CONTRACT v2: 70 hidden tests, 30 code-quality review). Internal.
    coding_score_test_weight: float = 0.7
    #: Scoring waits for a pending coding submission for at most this long;
    #: past it the answer is "Not assessed" and the report goes to a person.
    coding_execution_max_wait_hours: int = 24
    #: The five-minute sandbox probe's canary run may take this long.
    coding_probe_deadline_seconds: float = 20.0
    #: One check of the operator verification task may take this long.
    coding_verify_deadline_seconds: float = 45.0

    # ── The question budget and the format mix (Appendix B, PLAN-p3 WP2) ─────
    #
    # HOW MANY: one question per skill in the contract, never fewer than the
    # grade's floor. With at most five skills per bucket the budget is 8 to 15.
    # EVERY ITEM IS ASKED: nothing is pre-filled from a resume or an earlier
    # employer's record, and nothing is trimmed (supersedes C2 and CR 23).
    assessment_question_floor_non_managerial: int = 10
    assessment_question_floor_managerial: int = 10
    assessment_question_floor_leadership: int = 10
    assessment_question_floor_cxo: int = 8
    #: WHAT KIND, BY COUNT, for a coding role (a STEM verdict AND a computing
    #: occupation AND a working sandbox): prose, coding, objective (multiple
    #: choice and fill-in-the-blank). Any other role gets no coding question
    #: and the coding share joins prose. The three must sum to one.
    assessment_share_prose: float = 0.7
    assessment_share_coding: float = 0.2
    assessment_share_objective: float = 0.1
    #: The time each question is allocated, by family (Appendix B section 3),
    #: is `assessment_time_prose_seconds`, `assessment_time_objective_seconds`
    #: and `assessment_time_coding_seconds`: ONE setting each, declared with
    #: the server's turn clock below (WP2 and WP3 each declared the first two;
    #: merged at the stage 2 integration). Stamped on the question row when it
    #: is written; the conversation's clock snapshots its own allocation per
    #: turn.
    #: The longest a blocking proctoring warning can stop the candidate's
    #: clock for. The pause opens when the warning is issued and closes on the
    #: candidate's acknowledgement, or here, whichever is first
    #: (`services/assessment_conversation/pauses`).
    assessment_warning_pause_max_seconds: int = 30
    #: 20 minutes, Appendix B section 3. It is the ENFORCED turn clock for a
    #: coding question now (`services/assessment_conversation/timers`), not a
    #: suggestion, so it reads the owner's table rather than a composition fit.
    assessment_time_coding_seconds: int = 1200
    #: INTERNAL weight per format, within a matrix item. What makes evidence
    #: dominance structural rather than stated.
    assessment_weight_evidence: float = 1.0
    assessment_weight_short_answer: float = 1.0
    assessment_weight_mcq_single: float = 0.4
    assessment_weight_mcq_multi: float = 0.5
    assessment_weight_fill_blank: float = 0.4
    assessment_weight_coding: float = 0.8
    #: How many times the composer may regenerate a mix that fails validation
    #: before every slot it could not fill soundly becomes a prose question,
    #: each one recorded as a degradation on the conversation.
    assessment_composition_attempts: int = 3
    #: The fewest words an AI evaluation's reasoning may carry. A bare verdict
    #: with a sentence attached is not a reasoning a recruiter can act on.
    assessment_evaluation_min_reasoning_words: int = 40
    #: The shortest quotable resume item an evidence question may anchor to.
    #: Below this an "anchor" is a single word, which anchors nothing.
    assessment_anchor_min_chars: int = 12
    #: The fewest words a distractor's misconception rationale may carry
    #: before the option counts as a real misconception rather than filler.
    assessment_misconception_min_words: int = 4

    # ── The server's turn clock (Appendix B section 3, PLAN-p3 WP3) ─────────
    #
    # ENFORCED, not suggested: a turn that runs past its allocation plus the
    # grace is submitted by the server with whatever draft it holds, and an
    # empty one is an evidence gap. Each turn SNAPSHOTS its allocation when it
    # opens (`assessment_conversations.turn_allocation_seconds`), so changing
    # one of these never moves the deadline of a question somebody is
    # answering. Paused time (device loss, transcription, a warning on screen)
    # is excluded by the server from rows it wrote; nothing the client reports
    # enters the clock.
    #: Prose (evidence-based and short-answer), typed or spoken.
    assessment_time_prose_seconds: int = 180
    #: Multiple choice and fill-in-the-blank.
    assessment_time_objective_seconds: int = 60
    #: A follow-up or a re-ask on a prose answer.
    assessment_time_follow_up_seconds: int = 100
    #: How late an answer may arrive and still be the candidate's own. Covers
    #: the request in flight at the moment the countdown reaches zero.
    assessment_submit_grace_seconds: int = 5
    #: How long a START waits before dispatching question generation again for
    #: a conversation whose questions are still missing. Generation runs on its
    #: own Fargate task, and a start polled every few seconds must not start
    #: one each time.
    assessment_question_redispatch_seconds: int = 600

    # ── Spoken answers (Appendix B section 3) ────────────────────────────────
    #: The longest spoken answer the recorder captures. The client stops at it;
    #: the byte ceiling below is the server's half of the same bound. ONE
    #: setting (WP3 and WP4 each declared it; merged at the stage 2
    #: integration): proctoring also reads it to bound how long an unfinished
    #: capture can excuse speech in the audio monitoring.
    assessment_voice_max_seconds: int = 180
    #: Upper bound on one uploaded answer, sized for three minutes of browser
    #: audio with headroom. Refused above it, never truncated.
    assessment_voice_max_bytes: int = 6 * 1024 * 1024
    #: Amazon Transcribe for ONE answer: seconds to wait, and the poll interval.
    #: The candidate's clock is paused for the whole wait.
    assessment_voice_transcribe_timeout_seconds: int = 120
    assessment_voice_transcribe_poll_seconds: int = 3
    #: How long the clock stays paused after a transcription FAILED, so the
    #: candidate can read what happened before typing. Acknowledging ends it
    #: sooner; it never needs a sweep to end.
    assessment_voice_failure_pause_seconds: int = 30

    # ── Assessment consent (2026-09-05 spec, one mode since 2026-09-24) ─────
    #
    # CONSENT IS A HARD PREREQUISITE FOR STARTING (spec section 3). The
    # wording is CONFIGURABLE, never hardcoded in a handler (spec 3.2), and the
    # versions below are stamped onto every consent row so a dispute is
    # settled by which wording was accepted. The default is complete and
    # honest, in plain language, with no em dash.
    assessment_consent_version: str = "2026-09-24"
    assessment_privacy_policy_version: str = "2026-09-05"
    assessment_terms_version: str = "2026-09-05"
    #: ONE text since 2026-09-24: there is one assessment mode (Appendix B
    #: section 1). It states every collection the session makes, the
    #: retention both clocks impose (D4), the transcription of spoken answers
    #: with only the text kept, and the paste rule, because a candidate must be
    #: told the rules before they start. No storage vendor is named
    #: (claude.md 2026-07-26); the transcription service is, because it is a
    #: processor of the candidate's voice.
    assessment_consent_text: str = (
        "Before you begin the assessment, please understand and agree to the "
        "following. Your camera and microphone record the whole session, audio "
        "and video, while you answer. The recording is compressed and stored "
        "securely, and only the hiring team for this role can view it. It is "
        "deleted 90 days after your session, or 30 days after the job closes if "
        "that comes first. If you choose to speak an answer, your speech is "
        "converted to text by an automated speech-to-text service (Amazon "
        "Transcribe); only the text is kept, and that text is your final answer. "
        "Vivekium collects what you submit during the assessment: your written "
        "and spoken answers, your responses to multiple-choice, fill-in-the-blank "
        "and coding questions, and session data such as timings and interaction "
        "records. Copying and pasting are blocked, and every attempt is recorded. "
        "Your answers are analyzed by AI as part of your evaluation and may be "
        "included in the report the hiring team receives about your candidacy. "
        "If you do not agree, you will not be able to take the assessment."
    )
    # ── The session recording: ceilings and processing knobs ────────────────
    # Every ceiling is a setting, never a literal in the pipeline (same rule
    # as proctoring and projects).
    #: The TOTAL bytes one recording may hold across all of its segments. No
    #: longer an in-memory size: the bytes arrive one part at a time (see
    #: `video_part_max_bytes`). At the capped bitrate below a gigabyte is
    #: about five hours, twice the longest session the question budget allows.
    video_max_upload_bytes: int = 1024 * 1024 * 1024
    #: Sized for the worst-case assessment (about 113 minutes at 15 skills,
    #: PLAN-p3 3.0) with room to spare.
    video_max_duration_seconds: int = 3 * 3600
    #: The largest single part the API accepts and forwards to S3. This is
    #: the whole of what one request holds in memory, which is the point: the
    #: old single upload read up to `video_max_upload_bytes` into the shared
    #: API task. S3's own floor for every part but the last is 5 MiB
    #: (`S3_MIN_PART_BYTES`, module level), so the validator below
    #: refuses a value under it.
    video_part_max_bytes: int = 16 * 1024 * 1024
    #: How many segments one recording may be split into. A segment starts at
    #: the session's start, after each device recovery (at most
    #: `proctoring_device_max_pauses`) and after a page reload, so a real
    #: session uses a handful; the ceiling bounds what a looping client can
    #: make the store hold open.
    video_max_segments: int = 32
    #: The recorder ceilings, SERVED to the browser by the session-media start
    #: so the client and the server never disagree about a number. About
    #: 200 MiB an hour, which is what keeps a two-hour session's parts small
    #: and its processing inside one Fargate task.
    video_recording_video_bps: int = 400_000
    video_recording_audio_bps: int = 48_000
    video_recording_max_width: int = 640
    video_recording_max_height: int = 360
    #: How long a recording may sit with no new part after its session ended
    #: (or after it outlived `video_max_duration_seconds`) before the hourly
    #: sweep completes its uploaded segments and processes it without the
    #: browser. Long enough for a slow final flush; short enough that a closed
    #: tab never strands a recording.
    video_orphan_grace_minutes: int = 30
    #: Amazon Transcribe. DISABLED by default because it needs an AWS account
    #: with the service enabled in the deployment region. Since 2026-09-24 it
    #: transcribes SPOKEN ANSWERS only (PLAN-p3 WP3); the session recording is
    #: never transcribed, and the video-interview mode whose recording was
    #: transcribed is deleted. Disabled is reported honestly, never a fake
    #: transcript (no silent fallback).
    transcribe_enabled: bool = False
    transcribe_language_code: str = "en-IN"
    #: THE TRANSCRIBE REGION IS NOT NECESSARILY THE DEPLOYMENT REGION, and that
    #: is a fact about AWS rather than a preference: ap-south-2 (Hyderabad) has
    #: no Transcribe endpoint at all, so a pilot deployed there must call the
    #: service in ap-south-1 (Mumbai). Empty means `aws_region`, which is
    #: correct wherever Transcribe exists in the deployment region.
    transcribe_region: str = ""
    #: A Transcribe job reads its media from, and writes its output to, a
    #: bucket in ITS OWN region: a job running in ap-south-1 cannot read
    #: s3://bucket when that bucket lives in ap-south-2. This names the working
    #: bucket in `transcribe_region`. The transcription step copies a spoken
    #: answer's audio in, runs the job, copies the transcript back to
    #: `s3_bucket` and deletes both working objects, so nothing accumulates
    #: here. Empty means `s3_bucket`, which is correct only when the two
    #: regions agree. (The whole-recording `video_transcribe_*` timeouts went
    #: with the video-interview mode on 2026-09-24; a spoken answer carries
    #: its own, shorter bounds.)
    transcribe_bucket: str = ""
    #: ffmpeg transcode settings for the long-term compressed mp4 (video spec
    #: section 8: codec and quality are configurable, storage optimization,
    #: not destructive compression). H.264 + AAC for browser playability.
    video_compression_crf: int = 28
    video_compression_preset: str = "medium"
    video_compression_audio_bitrate_kbps: int = 96
    #: The compressed object's duration must be within this many seconds of
    #: the raw recording's before the raw is deleted (video spec section 10:
    #: verify before delete).
    video_duration_tolerance_seconds: float = 3.0
    video_ffmpeg_timeout_seconds: int = 1800
    #: Presigned delivery-URL lifetimes for the client portal (video spec
    #: sections 16 and 18). A presigned URL is a bearer token once it leaves
    #: the page, so both are short by default. Preview must outlive a full
    #: watch-through of the longest permitted interview because the player's
    #: seek issues range requests against the same URL; download only needs to
    #: cover the click and a slow connection's head start.
    video_preview_url_ttl_seconds: int = 3 * 3600
    video_download_url_ttl_seconds: int = 900
    #: Owner decision D4, the session half: a recording is purged this many
    #: days after its session ended, or at the job-closure purge, whichever
    #: comes first. The value is STAMPED onto each recording as
    #: `media_purge_due_at` when it is finalized, so changing it here moves
    #: no deadline a candidate was already given. Must be positive: zero used
    #: to mean "no time-based purge", and D4 replaced that policy.
    #: `infra/modules/s3` carries the same number as the backstop lifecycle
    #: rule, and `tests/test_media_retention_d4.py` compares the two.
    assessment_media_retention_days: int = 90

    # ── Project Evidence Intelligence limits ────────────────────────────────
    #
    # Candidate project submissions are UNTRUSTED input processed into derived
    # evidence; the original artifact is temporary by product decision (the
    # 2026-09-01 Project Evidence master brief) and is deleted once evidence is
    # persisted. These are the safe processing ceilings, DATA here rather than
    # literals in the pipeline so an operator can widen them without a deploy
    # of new code paths. Sized against the resume path's own ceilings (10 MB a
    # file) and the worker's 600 s soft time limit: the whole pipeline for one
    # project must finish comfortably inside one task slot.
    project_max_projects_per_candidate: int = 10
    project_max_files: int = 20
    project_max_file_bytes: int = 25 * 1024 * 1024
    project_max_total_bytes: int = 100 * 1024 * 1024
    #: Archive extraction guards (zip bombs, nesting, entry floods).
    project_max_archive_depth: int = 2
    project_max_archive_entries: int = 2000
    project_max_extracted_bytes: int = 200 * 1024 * 1024
    #: A compressed entry claiming to inflate past this ratio is refused as a
    #: decompression bomb before a single byte is extracted.
    project_max_compression_ratio: int = 120
    #: Per-file ceiling on text promoted into deterministic parsing.
    project_max_text_chars_per_file: int = 60_000
    #: Evidence reduction: the most units one project may persist, and the most
    #: characters of evidence-pack context one AI reasoning call may receive.
    project_max_evidence_units: int = 120
    project_max_ai_context_chars: int = 24_000
    #: Public-repository ingestion caps: how many meaningful files are fetched
    #: after tree classification, and the largest single file fetched.
    project_repo_max_files: int = 40
    project_repo_max_file_bytes: int = 512_000
    #: OPTIONAL. Raises the public GitHub API rate limit; grants no private
    #: access and is never required. Public repositories only, by product
    #: decision: no private-repository OAuth or token intake exists.
    github_api_token: str = ""

    # ── Per-assessment cost telemetry (change 28D) ──────────────────────────
    #
    # THE PLATFORM PAYS IN DOLLARS AND THE OWNER THINKS IN RUPEES, AND THE
    # CONVERSION BETWEEN THEM IS A SETTING RATHER THAN A CONSTANT IN A QUERY.
    #
    # Every price in `config/llm_providers.TOKEN_PRICES_USD_PER_MILLION` is
    # USD; every figure the owner reasons about (the plans, the rate card, the
    # alert below) is INR. Something has to convert, and the two dishonest
    # places to put it are inside the aggregation query, where nobody finds it,
    # and inside the frontend, where two surfaces immediately disagree.
    #
    # BE EXACT ABOUT WHAT A FIXED RATE BUYS. It is not a live FX quote and it
    # is not fetched: it is one number, edited by a person, and the rupee
    # figures derived from it are therefore APPROXIMATE and are labelled as
    # such everywhere they are rendered. That is enough for the question being
    # asked -- "is the average per-assessment cost drifting toward the
    # threshold" -- because a rate error moves every figure in the series by
    # the same factor and leaves the trend intact. It would NOT be enough to
    # invoice from, and nothing invoices from it.
    #: USD to INR. A reviewed default, not a quote.
    usd_to_inr_rate: float = 88.0
    #: The owner's alert line: flag a month whose AVERAGE cost per assessment
    #: exceeds this many rupees. A flag, never a gate: nothing in the product
    #: refuses work because this was crossed, because the work in question has
    #: already been paid for by a customer who is owed a report.
    assessment_cost_alert_inr: float = 150.0

    # ── Retrieval, RPN-AI-UP-001 W2 ─────────────────────────────────────────
    #
    # FILTERED ANN RECALL IS THE DANGEROUS ONE, AND IT FAILS SILENTLY.
    #
    # An HNSW scan returns its top K by vector distance and the tenant
    # predicate filters AFTERWARDS. In a multi-tenant table the scan can
    # traverse mostly other tenants' vectors and return almost nothing for the
    # calling tenant. It does not error. It returns a short list that reads as
    # a legitimately sparse result, and recall degrades as tenant count grows
    # -- worst for the smallest tenants, which are the newest customers.
    #
    # `hnsw.iterative_scan` makes the index keep pulling candidates until
    # enough rows pass the predicate. `strict_order` additionally guarantees
    # exact distance ordering, which matters here because RRF fusion reads
    # ORDER and nothing else: a relaxed order would corrupt the one signal
    # fusion consumes.
    #
    # Requires pgvector 0.8.0 or later. Measured 0.8.1 on the pilot cluster and
    # 0.8.5 on the test image (2026-09-09), so it is available on both. It is a
    # SETTING rather than a literal so an environment on an older pgvector can
    # turn it off explicitly, and `off` is then a recorded deployment decision
    # rather than a silent fallback.
    #: `strict_order` | `relaxed_order` | `off`.
    retrieval_hnsw_iterative_scan: str = "strict_order"
    #: Bounds a runaway iterative scan. Without a ceiling, a query for a tenant
    #: with no matching rows scans the whole index before returning empty.
    retrieval_hnsw_max_scan_tuples: int = 20_000
    #: Candidates the HNSW layer considers per query. pgvector's default is 40,
    #: which is below the depth the fusion stage asks for.
    retrieval_hnsw_ef_search: int = 100
    #: How many documents one `reconcile_context_index` pass repairs. The sweep
    #: dispatches one indexing task per document, so this bounds the fan-out of
    #: a single hourly run rather than the work itself.
    retrieval_index_sweep_batch: int = 200
    #: How many chunks one `repair_semantic_index` pass re-embeds (PLAN-p5
    #: WP5-E): chunks with a NULL vector, a retired model or contract stamp, or
    #: the wrong width. The pass runs hourly, so this is the hourly cap on
    #: re-embedding cost. 0 PAUSES the sweep: every chunk written before
    #: 2026-09-24 carries no model stamp and is re-embedded by the first
    #: passes, and an owner watching a vendor bill needs a setting, not a
    #: deploy, to stop that.
    retrieval_repair_sweep_batch: int = 200

    # ── Retrieval intelligence (RPN-AI-UP-001 W6) ───────────────────────────
    #
    # Deployment data, one value per deployment, never a fallback chain: the
    # same shape as TASK_DISPATCH_BACKEND and email_transport. Read through a
    # validator that RAISES on an unrecognised value, because the failure mode
    # of a wrong one is SILENT -- retrieval keeps working and simply gets
    # worse, which is indistinguishable from a tenant with thin evidence.
    #: `voyage` (the cross-encoder) or `lexical` (the deterministic pass).
    #: Defaults to `lexical` deliberately: an environment that has not made the
    #: decision runs the behaviour it runs today, and turning the cross-encoder
    #: on is an explicit act.
    retrieval_reranker: str = "lexical"
    #: Whether a situating prefix is generated at index time.
    retrieval_contextual_prefix: bool = True
    #: The reranker's credential, named after the model it unlocks (rerank-2.5),
    #: the same convention as VOYAGE_CONTEXT_4 and OPENAI_GPT_TERRA. A separate
    #: variable from the embedding key even where the Voyage account issues one
    #: string: it makes "the reranker is not configured" distinguishable from
    #: "embedding is not configured", so an unset value here is a RECORDED
    #: degradation rather than an embedding outage wearing a reranker's name.
    voyage_rerank_2_5: str = ""

    # Payments: Razorpay Orders, one-time credit purchases. The Key ID is public
    # (Checkout needs it in the browser and receives it on the purchase
    # response); the Key Secret and the webhook secret are server-side only
    # and never reach a response body, a log line, or the frontend bundle.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    # Vivekium's own GST registration number, printed on every credit-pack
    # invoice (Master Directive Part 5 §5.2). Configuration, not code: it is a
    # legal identifier that changes with registration, never with a release.
    readypick_gstin: str = ""

    # App
    environment: str = "development"
    #: Set by terraform on the containers that SIGN (the API service and the
    #: task worker). See `_refuse_an_unconfigured_jwt_secret`.
    require_jwt_secret: bool = False
    # Public demo is off unless DEMO_MODE is set. Production also needs
    # DEMO_MODE_CONFIRM=public-demo, enforced by the validator below.
    demo_mode: bool = False
    demo_mode_confirm: str = ""
    demo_initial_credits: int = Field(default=25, ge=1, le=10000)

    @model_validator(mode="after")
    def _refuse_an_unconfigured_jwt_secret(self) -> "Settings":
        """In production, refuse to boot without a real signing key.

        ONE SECRET KEYS EVERYTHING, which is what makes this worth a boot
        refusal rather than a warning. `jwt_secret` signs the portal session
        cookies, the outreach links, the assessment invite tokens and the
        signed resume URLs. A known value lets anyone mint
        `{"aud": "pickready:owner", "role": "super_admin"}` and reach
        `get_superadmin_db`, which is the RLS bypass scope. That is total
        platform compromise from a default string.

        AND IT COULD ARRIVE EMPTY. `_drop_placeholder_secrets` below rewrites
        `PLACEHOLDER_NOT_CONFIGURED` to "", and PyJWT signs HS256 with an empty
        key without complaint -- so an unprovisioned secret does not fail, it
        silently signs. That is the exact shape of the 2026-09-06 Firebase
        incident, where a secret CONTAINER was mistaken for a configured
        secret, on the one credential whose blast radius is everything.

        Ordered BEFORE the placeholder rewrite so the refusal can name which of
        the two states it found. Development and test are unaffected: the
        default is what lets a fresh clone run.
        """
        if (self.environment or "").strip().lower() != "production":
            return self
        # FIRES ONLY WHERE THE PROCESS DECLARES IT SIGNS. The first
        # production roll (SEC-24, 2026-09-19) proved the unconditional form
        # wrong twice in one day: it refused the MIGRATE one-shot, which
        # signs nothing, and then took every Lambda down, because secrets
        # are enumerated PER SERVICE and the drafting lambdas hold no
        # signing key by design. A guard that assumes every container holds
        # every secret is a guard against the least-privilege model itself.
        # Terraform sets REQUIRE_JWT_SECRET on the containers that mint or
        # verify signed material (the API service, and the task worker,
        # which signs assessment invite links); a signer that boots without
        # its key still refuses, which is the original guarantee, held where
        # it is true.
        if not self.require_jwt_secret:
            return self
        value = (self.jwt_secret or "").strip()
        if not value or value == PLACEHOLDER_SECRET or value == DEV_JWT_SECRET:
            raise ValueError(
                "JWT_SECRET is not configured in production. It signs the "
                "session cookies and every signed link, so a "
                "default or empty value is a full platform compromise. Set it "
                "in Secrets Manager and redeploy."
            )
        return self

    @model_validator(mode="after")
    def _refuse_demo_mode_without_confirm(self) -> "Settings":
        """Production and pilot cannot turn the public demo on by accident.

        The live task sets ENVIRONMENT=production. A process that boots with
        ENVIRONMENT=pilot is held to the same confirm phrase. Development
        and test may set DEMO_MODE without it.
        """
        environment = (self.environment or "").strip().lower()
        if (
            environment in {"production", "pilot"}
            and self.demo_mode
            and self.demo_mode_confirm != "public-demo"
        ):
            raise ValueError(
                "DEMO_MODE on production requires DEMO_MODE_CONFIRM=public-demo."
            )
        return self

    @model_validator(mode="after")
    def _drop_placeholder_secrets(self) -> "Settings":
        """Turn every placeholder back into "not configured".

        Applied to EVERY string field rather than a named list, because the
        list would be the thing that drifts: a secret added to
        `infra/modules/secrets` without a matching entry here would arrive as a
        non-empty sentinel and read as configured.
        """
        for name, field in type(self).model_fields.items():
            if getattr(self, name, None) == PLACEHOLDER_SECRET:
                object.__setattr__(self, name, "")
        return self
    frontend_url: str = "http://localhost:3000"

    # Platform Owner  -  the ONLY identity permitted to hold the owner
    # (super_admin) role. Enforced in the API layer, not just seed/UI.
    owner_email: str = "manjuchro@gmail.com"

    # Gmail's authenticated mailbox is always the From address.

    # Outbound-delivery retry cap. Email retries transient failures after a
    # fixed 60-second delay. Permanent failures never retry.
    delivery_max_retries: int = 3  # initial attempt + up to 3 retries

    @model_validator(mode="after")
    def validate_gmail_smtp(self) -> "Settings":
        configured = bool(self.smtp_host or self.smtp_user or self.smtp_password)
        if not configured:
            return self
        if self.smtp_host != "smtp.gmail.com":
            raise ValueError("SMTP_HOST must be smtp.gmail.com")
        if self.smtp_port != 587 or not self.smtp_starttls or self.smtp_ssl:
            raise ValueError(
                "Gmail SMTP requires port 587 with STARTTLS enabled and SSL disabled"
            )
        if self.smtp_user and not self.smtp_user.lower().endswith("@gmail.com"):
            raise ValueError("SMTP_USER must be a Gmail address")
        if self.smtp_user and self.smtp_from_email.lower() != self.smtp_user.lower():
            raise ValueError("SMTP_FROM_EMAIL must match SMTP_USER")
        return self

    @property
    def effective_transcribe_region(self) -> str:
        """The region the Transcribe client is built in."""
        return (self.transcribe_region or self.aws_region or "").strip()

    @property
    def effective_transcribe_bucket(self) -> str:
        """The bucket a Transcribe job reads and writes, in that region."""
        return (self.transcribe_bucket or self.s3_bucket or "").strip()

    @model_validator(mode="after")
    def validate_transcribe_colocation(self) -> "Settings":
        """Refuse the cross-region misconfiguration rather than fail per job.

        Transcribe is region-local over S3, so pointing the client at another
        region while leaving the bucket behind produces a BadRequestException
        on EVERY recording, one at a time, hours after the deploy. Naming it
        here makes it a boot failure a deploy can see.
        """
        if not self.transcribe_enabled:
            return self
        deployment = (self.aws_region or "").strip()
        region = (self.transcribe_region or "").strip()
        if region and region != deployment and not (self.transcribe_bucket or "").strip():
            raise ValueError(
                "TRANSCRIBE_REGION differs from AWS_REGION, so TRANSCRIBE_BUCKET "
                "must name a bucket in TRANSCRIBE_REGION: an Amazon Transcribe "
                "job cannot read or write a bucket in another region."
            )
        return self

    @model_validator(mode="after")
    def validate_recording_retention(self) -> "Settings":
        """Refuse a recording configuration the product cannot honour.

        A non-positive retention would stamp a purge date in the past (or
        at the session's end) onto every recording, so the hourly sweep would
        delete each one the moment it was stored. A part ceiling under S3's
        5 MiB floor would make every multipart upload fail at completion,
        one candidate at a time, instead of at boot.
        """
        if self.assessment_media_retention_days <= 0:
            raise ValueError(
                "ASSESSMENT_MEDIA_RETENTION_DAYS must be positive (owner "
                "decision D4 keeps a session recording for 90 days)."
            )
        if self.video_part_max_bytes < S3_MIN_PART_BYTES:
            raise ValueError(
                "VIDEO_PART_MAX_BYTES must be at least 5 MiB, the smallest "
                "part S3 accepts in a multipart upload."
            )
        return self

    @model_validator(mode="after")
    def validate_email_transport(self) -> "Settings":
        """Exactly one of the two real transports; a typo must not silently
        select anything (Corporate Email System spec section 6)."""
        value = (self.email_transport or "").strip().lower()
        if value not in {"smtp", "ses"}:
            raise ValueError("EMAIL_TRANSPORT must be smtp or ses")
        object.__setattr__(self, "email_transport", value)
        return self

    @model_validator(mode="after")
    def validate_security_codes(self) -> "Settings":
        """A security code that expires at once, can never be entered, or can
        be re-sent without pause is not a security control. Refused at boot."""
        if self.security_code_ttl_seconds < 60:
            raise ValueError("SECURITY_CODE_TTL_SECONDS must be at least 60")
        if self.security_code_max_attempts < 1:
            raise ValueError("SECURITY_CODE_MAX_ATTEMPTS must be at least 1")
        if self.security_code_resend_seconds < 1:
            raise ValueError("SECURITY_CODE_RESEND_SECONDS must be at least 1")
        if "@" not in (self.platform_security_sender_email or ""):
            raise ValueError("PLATFORM_SECURITY_SENDER_EMAIL must be an email address")
        return self

    @model_validator(mode="after")
    def validate_proctoring_pause_rules(self) -> "Settings":
        """Refuse a device-pause or audio rule that cannot mean anything.

        These numbers are read to a candidate before they start ("you have
        about two minutes to fix it") and decide whether their assessment
        ends, so a configuration under which the grace is shorter than the
        glitch filter, or a pause cap is zero, must fail at boot rather than
        on a candidate's camera.
        """
        if self.proctoring_device_max_pauses < 0:
            raise ValueError("PROCTORING_DEVICE_MAX_PAUSES must be zero or more")
        if self.proctoring_device_glitch_seconds < 0:
            raise ValueError("PROCTORING_DEVICE_GLITCH_SECONDS must be zero or more")
        if self.proctoring_device_grace_seconds <= self.proctoring_device_glitch_seconds:
            raise ValueError(
                "PROCTORING_DEVICE_GRACE_SECONDS must be longer than "
                "PROCTORING_DEVICE_GLITCH_SECONDS"
            )
        if self.proctoring_second_voice_min_seconds <= 0:
            raise ValueError("PROCTORING_SECOND_VOICE_MIN_SECONDS must be positive")
        if self.proctoring_speech_min_seconds <= 0:
            raise ValueError("PROCTORING_SPEECH_MIN_SECONDS must be positive")
        if self.proctoring_speech_highlight_threshold < 1:
            raise ValueError("PROCTORING_SPEECH_HIGHLIGHT_THRESHOLD must be at least one")
        if self.assessment_warning_pause_max_seconds <= 0:
            raise ValueError("ASSESSMENT_WARNING_PAUSE_MAX_SECONDS must be positive")
        if self.assessment_voice_max_seconds <= 0:
            raise ValueError("ASSESSMENT_VOICE_MAX_SECONDS must be positive")
        return self

    @model_validator(mode="after")
    def validate_turn_clock(self) -> "Settings":
        """Refuse a turn clock or a spoken-answer bound that cannot work, at
        boot rather than on a candidate's turn: a zero allocation would expire
        every question the moment it opened, and a poll no shorter than its
        timeout would never poll twice."""
        positive = {
            "ASSESSMENT_TIME_PROSE_SECONDS": self.assessment_time_prose_seconds,
            "ASSESSMENT_TIME_OBJECTIVE_SECONDS": self.assessment_time_objective_seconds,
            "ASSESSMENT_TIME_FOLLOW_UP_SECONDS": self.assessment_time_follow_up_seconds,
            "ASSESSMENT_TIME_CODING_SECONDS": self.assessment_time_coding_seconds,
            "ASSESSMENT_QUESTION_REDISPATCH_SECONDS": self.assessment_question_redispatch_seconds,
            "ASSESSMENT_VOICE_MAX_BYTES": self.assessment_voice_max_bytes,
            "ASSESSMENT_VOICE_TRANSCRIBE_TIMEOUT_SECONDS": (
                self.assessment_voice_transcribe_timeout_seconds
            ),
            "ASSESSMENT_VOICE_TRANSCRIBE_POLL_SECONDS": (
                self.assessment_voice_transcribe_poll_seconds
            ),
            "ASSESSMENT_VOICE_FAILURE_PAUSE_SECONDS": self.assessment_voice_failure_pause_seconds,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.assessment_submit_grace_seconds < 0:
            raise ValueError("ASSESSMENT_SUBMIT_GRACE_SECONDS must not be negative")
        if (
            self.assessment_voice_transcribe_poll_seconds
            >= self.assessment_voice_transcribe_timeout_seconds
        ):
            raise ValueError(
                "ASSESSMENT_VOICE_TRANSCRIBE_POLL_SECONDS must be shorter than "
                "ASSESSMENT_VOICE_TRANSCRIBE_TIMEOUT_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def validate_question_mix(self) -> "Settings":
        """Refuse a question budget or mix that cannot describe an assessment.

        The three shares are apportioned by largest remainder over the budget
        (`assessment_questions.budget.mix`), which only means something when
        they are non-negative and sum to one. A floor below one would let a
        job with one skill be assessed on one question. Refused at boot rather
        than discovered when the first candidate's questions are written.
        """
        shares = (
            self.assessment_share_prose,
            self.assessment_share_coding,
            self.assessment_share_objective,
        )
        if any(share < 0 for share in shares) or abs(sum(shares) - 1.0) > 1e-9:
            raise ValueError(
                "ASSESSMENT_SHARE_PROSE, ASSESSMENT_SHARE_CODING and "
                "ASSESSMENT_SHARE_OBJECTIVE must be non-negative and sum to 1"
            )
        if self.assessment_share_prose <= 0:
            raise ValueError("ASSESSMENT_SHARE_PROSE must be above 0")
        floors = (
            self.assessment_question_floor_non_managerial,
            self.assessment_question_floor_managerial,
            self.assessment_question_floor_leadership,
            self.assessment_question_floor_cxo,
        )
        if any(floor < 1 for floor in floors):
            raise ValueError("every ASSESSMENT_QUESTION_FLOOR_* must be at least 1")
        times = (
            self.assessment_time_prose_seconds,
            self.assessment_time_objective_seconds,
            self.assessment_time_coding_seconds,
        )
        if any(seconds < 1 for seconds in times):
            raise ValueError("every ASSESSMENT_TIME_*_SECONDS must be at least 1")
        return self

    @model_validator(mode="after")
    def validate_code_execution(self) -> "Settings":
        """Refuse a malformed code-execution configuration at boot.

        A typo in the backend name must not silently select anything, and a
        malformed language or limit map would otherwise fail on the first
        candidate who presses Run, one request at a time. A MISSING URL or
        token is deliberately NOT refused here: `is_enabled()` answers False
        and every surface records the feature as unavailable.
        """
        backend = (self.code_execution_backend or "").strip().lower()
        if backend not in CODE_EXECUTION_BACKENDS:
            raise ValueError("CODE_EXECUTION_BACKEND must be judge0 or disabled")
        object.__setattr__(self, "code_execution_backend", backend)

        languages = self.code_execution_language_list
        unknown = [key for key in languages if key not in CODE_EXECUTION_LANGUAGE_KEYS]
        if unknown or not languages:
            raise ValueError(
                "CODE_EXECUTION_LANGUAGES must list at least one of "
                f"{', '.join(CODE_EXECUTION_LANGUAGE_KEYS)}; unknown: {unknown}"
            )
        # Reading the properties parses them; a malformed value raises here.
        multipliers = self.code_execution_cpu_multiplier_map
        for key, factor in multipliers.items():
            if key not in languages:
                raise ValueError(f"CODE_EXECUTION_CPU_MULTIPLIERS names {key!r}, which is not configured")
            if factor < 1.0:
                raise ValueError("a CODE_EXECUTION_CPU_MULTIPLIERS factor must be at least 1.0")
        ids = self.judge0_language_id_map
        if backend == "judge0":
            missing = [key for key in languages if key not in ids]
            if missing:
                raise ValueError(f"JUDGE0_LANGUAGE_IDS has no id for {missing}")

        positive = {
            "CODE_EXECUTION_CPU_SECONDS": self.code_execution_cpu_seconds,
            "CODE_EXECUTION_WALL_SECONDS": self.code_execution_wall_seconds,
            "CODE_EXECUTION_MEMORY_MB": self.code_execution_memory_mb,
            "CODE_EXECUTION_STACK_KB": self.code_execution_stack_kb,
            "CODE_EXECUTION_MAX_PROCESSES": self.code_execution_max_processes,
            "CODE_EXECUTION_MAX_FILE_KB": self.code_execution_max_file_kb,
            "CODE_EXECUTION_MAX_OUTPUT_CHARS": self.code_execution_max_output_chars,
            "CODE_EXECUTION_CONNECT_TIMEOUT_SECONDS": self.code_execution_connect_timeout_seconds,
            "CODE_EXECUTION_REQUEST_TIMEOUT_SECONDS": self.code_execution_request_timeout_seconds,
            "CODE_EXECUTION_POLL_SECONDS": self.code_execution_poll_seconds,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.code_execution_cpu_extra_seconds < 0:
            raise ValueError("CODE_EXECUTION_CPU_EXTRA_SECONDS must not be negative")
        if self.code_execution_wall_seconds < self.code_execution_cpu_seconds:
            raise ValueError("CODE_EXECUTION_WALL_SECONDS must be at least CODE_EXECUTION_CPU_SECONDS")

        # Coding question generation. 30 is `models.coding.HIDDEN_TESTS_MAX`,
        # the database CHECK; a range the table refuses would fail every
        # question at INSERT, after the model and the sandbox had been paid.
        if not 1 <= self.coding_visible_tests_min <= self.coding_visible_tests_max <= 5:
            raise ValueError(
                "CODING_VISIBLE_TESTS_MIN and _MAX must satisfy 1 <= min <= max <= 5"
            )
        if not 1 <= self.coding_hidden_tests_min <= self.coding_hidden_tests_max <= 30:
            raise ValueError(
                "CODING_HIDDEN_TESTS_MIN and _MAX must satisfy 1 <= min <= max <= 30"
            )
        if self.coding_generation_deadline_seconds <= self.coding_validation_deadline_seconds:
            raise ValueError(
                "CODING_GENERATION_DEADLINE_SECONDS must exceed "
                "CODING_VALIDATION_DEADLINE_SECONDS"
            )
        if self.coding_validation_deadline_seconds <= 0:
            raise ValueError("CODING_VALIDATION_DEADLINE_SECONDS must be greater than zero")
        if not 0 < self.coding_reference_cpu_headroom <= 1:
            raise ValueError("CODING_REFERENCE_CPU_HEADROOM must be in (0, 1]")
        # Coding Run, Submit and scoring. A weight of 0 or 1 would silently
        # drop one of the two halves the contract says a coding score has.
        if not 0 < self.coding_score_test_weight < 1:
            raise ValueError("CODING_SCORE_TEST_WEIGHT must be strictly between 0 and 1")
        for name, value in {
            "CODING_RUN_MAX_PER_QUESTION": self.coding_run_max_per_question,
            "CODING_RUN_DEADLINE_SECONDS": self.coding_run_deadline_seconds,
            "CODING_SUBMISSION_POLL_DEADLINE_SECONDS": self.coding_submission_poll_deadline_seconds,
            "CODING_SUBMISSION_REDISPATCH_MINUTES": self.coding_submission_redispatch_minutes,
            "CODING_REVIEW_RETRY_MINUTES": self.coding_review_retry_minutes,
            "CODING_EXECUTION_MAX_WAIT_HOURS": self.coding_execution_max_wait_hours,
            "CODING_PROBE_DEADLINE_SECONDS": self.coding_probe_deadline_seconds,
            "CODING_VERIFY_DEADLINE_SECONDS": self.coding_verify_deadline_seconds,
        }.items():
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")
        return self

    @property
    def code_execution_language_list(self) -> list[str]:
        """The configured coding languages, in the order they were listed."""
        keys = [part.strip().lower() for part in (self.code_execution_languages or "").split(",")]
        return list(dict.fromkeys(key for key in keys if key))

    @property
    def code_execution_cpu_multiplier_map(self) -> dict[str, float]:
        parsed: dict[str, float] = {}
        for key, value in _parse_pairs(self.code_execution_cpu_multipliers, "CODE_EXECUTION_CPU_MULTIPLIERS"):
            try:
                parsed[key] = float(value)
            except ValueError as exc:
                raise ValueError(f"CODE_EXECUTION_CPU_MULTIPLIERS value for {key!r} is not a number") from exc
        return parsed

    @property
    def judge0_language_id_map(self) -> dict[str, int]:
        parsed: dict[str, int] = {}
        for key, value in _parse_pairs(self.judge0_language_ids, "JUDGE0_LANGUAGE_IDS"):
            if not value.isdigit():
                raise ValueError(f"JUDGE0_LANGUAGE_IDS value for {key!r} is not a positive integer")
            parsed[key] = int(value)
        return parsed

    @model_validator(mode="after")
    def validate_cookie_policy(self) -> "Settings":
        value = (self.cookie_samesite or "").strip().lower()
        if value not in {"strict", "lax", "none"}:
            raise ValueError("COOKIE_SAMESITE must be strict, lax or none")
        object.__setattr__(self, "cookie_samesite", value)
        if value == "none" and self.environment != "production":
            # SameSite=None is only honoured on a Secure (https) cookie, and
            # `secure` is tied to the production flag. Refuse the combination
            # rather than shipping a cookie every browser silently drops.
            raise ValueError(
                "COOKIE_SAMESITE=none requires ENVIRONMENT=production so the "
                "cookie is marked Secure"
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def serves_over_https(self) -> bool:
        """Whether the browser reaches this deployment over TLS.

        THE AUTH COOKIE'S `Secure` FLAG READS THIS, not `is_production`, and the
        difference is a real one: `Secure` is a property of the ORIGIN, not a
        production feature. Tying it to `is_production` meant a cookie issued by
        the pilot or by staging went out without it over a genuine HTTPS origin.

        Derived from `frontend_url` because that is the same string the load
        balancer actually serves and `jobs.public_job_url` already builds
        candidate links from, so this cannot drift from reality. It is https in
        every deployed environment, http on a laptop, and right in an
        environment nobody has thought about yet.
        """
        return self.frontend_url.lower().startswith("https://")

    def missing_delivery_keys(self) -> list[str]:
        """Names of unset outbound-delivery credentials (for startup preflight).

        Gmail SMTP (host/user/password) is the one credential set. The SMS
        credentials left with the SMS sender on 2026-09-24.
        """
        checks = {
            "SMTP_HOST": self.smtp_host,
            "SMTP_USER": self.smtp_user,
            "SMTP_PASSWORD": self.smtp_password,
        }
        return [name for name, value in checks.items() if not value]


def preflight_delivery_config() -> list[str]:
    """Log a loud WARNING for any missing email credential at startup.

    ASSUMPTION: a missing key must NOT hard-crash the container in development:
    local dev without SMTP keys has to remain possible (the sprint brief only
    requires that a missing key not fail *silently*). In production the same
    warning is emitted; enforcement/alerting on it is an ops concern, not a
    process-exit here. Returns the list of missing key names so callers (or
    tests) can assert on it.
    """
    import logging

    settings = get_settings()
    missing = settings.missing_delivery_keys()
    if missing:
        logging.getLogger(__name__).warning(
            "delivery.preflight MISSING outbound credentials: %s  -  emails "
            "using these will fail. Set them in the environment. env=%s",
            ", ".join(missing),
            settings.environment,
        )
    else:
        logging.getLogger(__name__).info(
            "delivery.preflight ok  -  SMTP credentials present"
        )
    return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
