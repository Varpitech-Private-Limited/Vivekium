/**
 * ReadyPick STAGING.
 *
 * Composes every module. Two environment roots rather than one root with a
 * workspace, and that is deliberate: a `terraform.workspace` conditional means
 * one plan file describes two environments, and the moment somebody runs the
 * wrong workspace the blast radius is production. Two directories means the
 * production plan cannot be produced by accident from the staging one.
 *
 * WHAT STAGING IS FOR, AND THEREFORE WHAT IS SMALLER HERE
 * --------------------------------------------------------
 * Staging exists to prove a deploy works, not to survive an AZ failure. So:
 * one NAT gateway, no RDS Multi-AZ, no Redis replica, one task per service,
 * seven-day backups, no deletion protection. Every one of those is a cost
 * decision and every one is stated in this file rather than defaulted, so the
 * production file's differences are readable as a diff.
 *
 * READ `infra/environments/README.md` BEFORE RUNNING ANYTHING. spec-doc5 §D.1
 * is explicit that this phase produces a codebase that is buildable and
 * planable but that NO LIVE DEPLOYMENT IS EXECUTED -- and it makes that a
 * pass/fail criterion in the opposite direction from usual.
 */

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60"
    }
  }

  # REMOTE STATE IS IN `backend.tf`, NOT HERE, AND NOT COMMENTED OUT.
  #
  # This block used to be a commented-out backend block with a note saying the
  # bucket had to be bootstrapped first. The bucket has existed since
  # 2026-09-04 and the comment stayed, so every `terraform init` in CI silently
  # initialised the LOCAL backend and every apply would have started from empty
  # state on an ephemeral runner. Read `backend.tf` for what that costs.
  #
  # It lives in its own file so `infra/plan-offline.sh` can plan a copy of this
  # directory with that one file omitted, which is what lets a plan run with no
  # credentials and no network.
}

provider "aws" {
  region = var.region

  # ── THE PLANNING PROFILE (spec-doc6 §13.3) ─────────────────────────────────
  #
  # These four are the calls the AWS provider makes BEFORE it plans anything:
  # STS GetCallerIdentity, the region catalogue, and the EC2 instance metadata
  # endpoint. They are the entire reason the previous phase concluded that
  # `terraform plan` "cannot complete without credentials", and they can simply
  # be switched off. With `planning_profile = true` a plan runs offline, in CI,
  # with dummy credentials from the environment and nothing else.
  #
  # FALSE BY DEFAULT. A real apply must keep every one of these checks: with
  # them skipped, a misconfigured profile fails later and far less clearly.
  #
  # Read `var.planning_profile` for exactly what an offline plan proves and what
  # it does not. It does not prove "ready to run".
  skip_credentials_validation = var.planning_profile
  skip_requesting_account_id  = var.planning_profile
  skip_region_validation      = var.planning_profile
  skip_metadata_api_check     = var.planning_profile

  default_tags {
    tags = local.tags
  }
}

locals {
  environment = "staging"

  # THE INTERNAL SERVICE NAMESPACE, OWNED HERE AND NOWHERE ELSE.
  #
  # The analysis service sits behind no load balancer and has no public path,
  # so a Cloud Map name is the only way the api and the worker can address it.
  # The namespace is built here rather than inside the ecs module because both
  # sides of the arrangement need the same string: the module registers the
  # service under it, and the two callers get the URL below in their
  # environment. Two places deriving one hostname from the same parts is two
  # places that can drift.
  internal_namespace = "${var.project}-${local.environment}.internal"

  # 8100 matches the analysis service's container port and the internal port
  # the network module opens from the task security group to itself. Plain
  # http: the hop is task to task inside a private subnet, the same reasoning
  # the load balancer's target groups already follow.
  analysis_service_url = "http://analysis.${local.internal_namespace}:8100"

  # ── The Redis node ids, for the alarms that watch them ───────────────────
  #
  # COMPUTED HERE RATHER THAN READ BACK OFF THE MODULE, and that is a plan-time
  # constraint rather than a preference. A replication group's member cluster
  # list is a resource attribute that does not exist until apply, and a
  # `for_each` over an unknown set cannot be planned at all. The same rule
  # `enable_alb_alarms` follows, and the same rule that keeps
  # `invokable_function_keys` naming functions by key.
  #
  # The ids are deterministic: ElastiCache names the members of a replication
  # group `<group id>-001`, `-002`, and the group id is `<project>-<environment>`
  # (see `infra/modules/elasticache`). The replica count is a local rather than
  # a literal in the module block below so that the two cannot disagree, which
  # would show up as an alarm on a node that does not exist sitting in
  # INSUFFICIENT_DATA for ever and reading as quiet.
  redis_replica_count = 0
  redis_cluster_ids = toset([
    for index in range(1 + local.redis_replica_count) :
    format("%s-%03d", "${var.project}-${local.environment}", index + 1)
  ])

  tags = {
    Project     = var.project
    Environment = local.environment
    ManagedBy   = "terraform"
    Repository  = "readypick"
  }
}

# ── KMS ──────────────────────────────────────────────────────────────────────
#
# ONE CUSTOMER-MANAGED KEY PER ENVIRONMENT, used by S3, RDS, ElastiCache,
# Secrets Manager and the log groups. One key rather than five because the
# question it answers -- "who can decrypt this environment's data" -- has one
# answer, and five keys would be five key policies to keep in step.
#
# Customer-managed rather than the AWS-managed default because an AWS-managed
# key has no key policy you can read: "who can decrypt" collapses into "whoever
# has the IAM permission", and the two questions stop being separable.

# THE KEY POLICY IS WRITTEN OUT, NOT DEFAULTED.
#
# This is the argument the block above already makes, finished. A
# customer-managed key was chosen over the AWS-managed default because an
# AWS-managed key "has no key policy you can read: who can decrypt collapses into
# whoever has the IAM permission, and the two questions stop being separable".
#
# Omitting the policy on a customer-managed key re-creates exactly that. AWS
# substitutes a default policy granting the account root full access and
# delegating every decision back to IAM, so the key reads as customer-managed
# and answers the same single question. Stating the policy is what makes the
# choice mean anything.
data "aws_iam_policy_document" "kms" {
  # THE ACCOUNT ROOT KEEPS ADMINISTRATIVE CONTROL. Without this statement the
  # key becomes unmanageable: KMS does not let IAM policies grant access to a
  # key whose own policy does not delegate to the account, so a key policy that
  # omits it can be neither used nor deleted by anybody, ever.
  statement {
    sid    = "AccountRootAdministersTheKey"
    effect = "Allow"
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${var.account_id}:root"]
    }
    actions   = ["kms:*"]
    resources = ["*"] # A key policy's resource IS the key it is attached to.
  }

  # THE AWS SERVICES THAT ENCRYPT THIS ENVIRONMENT'S DATA AT REST, enumerated.
  # Not a wildcard service principal: this list is the answer to "what can
  # decrypt this environment's data", and it should be readable as a list.
  statement {
    sid    = "ServicesThatEncryptThisEnvironmentsDataAtRest"
    effect = "Allow"
    principals {
      type = "Service"
      identifiers = [
        "s3.amazonaws.com",
        "rds.amazonaws.com",
        "elasticache.amazonaws.com",
        "secretsmanager.amazonaws.com",
        "logs.${var.region}.amazonaws.com",
      ]
    }
    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:CreateGrant",
      "kms:DescribeKey",
    ]
    resources = ["*"]

    # SCOPED TO THIS ACCOUNT. A service principal with no account condition is
    # the confused-deputy shape: the principal reads as narrow because it is a
    # named AWS service, and it is reachable from any account using that service.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }

  # THE SERVICES THAT PUBLISH TO THE KMS-ENCRYPTED ALARM TOPIC.
  #
  # ADDED 2026-09-17, AND WITHOUT IT THE ALARMS ABOVE ARE DECORATIVE.
  # `aws_sns_topic.alarms` sets `kms_master_key_id` to this key, so publishing
  # to it is a KMS operation as well as an SNS one. The topic POLICY already
  # allowed `cloudwatch.amazonaws.com` to publish; the KEY policy did not allow
  # it to encrypt, so the publish is refused by KMS after passing the topic
  # policy. Nothing about that is visible from the alarm: it transitions to
  # ALARM exactly as it should and the notification is simply never delivered.
  # An alarm nobody receives is indistinguishable from a healthy system.
  #
  # Two principals, enumerated, and deliberately NOT merged into the
  # encrypt-at-rest statement above: these two encrypt a MESSAGE in transit
  # through SNS, not this environment's stored data, and they need two actions
  # rather than six. `budgets.amazonaws.com` is here for the same reason --
  # `aws_budgets_budget.monthly` notifies through this same topic.
  statement {
    sid    = "ServicesThatPublishToTheEncryptedAlarmTopic"
    effect = "Allow"
    principals {
      type = "Service"
      identifiers = [
        "cloudwatch.amazonaws.com",
        "budgets.amazonaws.com",
      ]
    }
    # GenerateDataKey to encrypt the notification, Decrypt because SNS reads it
    # back on delivery. Nothing else: neither principal has any business
    # creating a grant against this key.
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey*",
    ]
    resources = ["*"]

    # The same account condition every other service principal in this file
    # carries, and it is safe to rely on here for a concrete reason: the SNS
    # topic policy ALREADY conditions the CloudWatch publish on
    # `AWS:SourceAccount`. If that key were not populated on these calls,
    # delivery would already be blocked one layer earlier, so this condition
    # cannot be the thing that silently breaks it.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_kms_key" "this" {
  description             = "ReadyPick ${local.environment}"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.kms.json

  tags = { Name = "${var.project}-${local.environment}" }
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.project}-${local.environment}"
  target_key_id = aws_kms_key.this.key_id
}

# ── Network ──────────────────────────────────────────────────────────────────

module "network" {
  source = "../../modules/network"

  project            = var.project
  environment        = local.environment
  region             = var.region
  cidr_block         = "10.20.0.0/16"
  availability_zones = var.availability_zones

  # Flow logs: the only record of what actually reached what. Every other
  # control in the network module is preventive and leaves no trace of having
  # refused anything.
  kms_key_arn             = aws_kms_key.this.arn
  flow_log_retention_days = 30

  # ONE NAT. Staging is disposable; a per-AZ pair is $64/month buying
  # resilience for an environment whose whole purpose is to be thrown away.
  single_nat_gateway = true

  tags = local.tags
}

# ── Registries ───────────────────────────────────────────────────────────────

module "ecr" {
  source = "../../modules/ecr"

  project     = var.project
  environment = local.environment
  kms_key_arn = aws_kms_key.this.arn

  # Fewer than production: staging images are rebuilt constantly and nobody
  # rolls back to the thirtieth one.
  keep_images = 10

  tags = local.tags
}

# ── Secrets ──────────────────────────────────────────────────────────────────

module "secrets" {
  source = "../../modules/secrets"

  project     = var.project
  environment = local.environment
  region      = var.region
  kms_key_id  = aws_kms_key.this.key_id
  kms_key_arn = aws_kms_key.this.arn

  tags = local.tags
}

# ── Data ─────────────────────────────────────────────────────────────────────

module "rds" {
  source = "../../modules/rds"

  project           = var.project
  environment       = local.environment
  subnet_ids        = module.network.data_subnet_ids
  security_group_id = module.network.rds_security_group_id

  instance_class        = "db.t4g.small"
  allocated_storage     = 20
  max_allocated_storage = 50
  multi_az              = false
  backup_retention_days = 7

  # BOTH GUARDS STATED RATHER THAN INHERITED (2026-09-17). They default to the
  # safe value in the module now, and stating them here is what makes turning
  # one off a visible line in a diff rather than a default quietly changing.
  #
  # Staging is disposable and this still stays on. A staging database is
  # rebuilt on purpose, not by accident, and a deliberate teardown can
  # afford one extra apply to turn the guard off. What it cannot afford is
  # `terraform destroy` run in the wrong directory.
  deletion_protection = true
  skip_final_snapshot = false

  kms_key_id  = aws_kms_key.this.key_id
  kms_key_arn = aws_kms_key.this.arn

  tags = local.tags
}

module "elasticache" {
  source = "../../modules/elasticache"

  project           = var.project
  environment       = local.environment
  subnet_ids        = module.network.data_subnet_ids
  security_group_id = module.network.redis_security_group_id

  node_type = "cache.t4g.micro"
  # No replica in staging. In production this is 1, because a Redis failure
  # there is not a cache miss: the proctoring gate answers 503 rather than
  # silently not warning, so it is every assessment turn.
  replica_count = local.redis_replica_count

  kms_key_arn = aws_kms_key.this.arn

  tags = local.tags
}

module "s3" {
  source = "../../modules/s3"

  project     = var.project
  environment = local.environment
  region      = var.region
  kms_key_arn = aws_kms_key.this.arn

  # NAMED, NOT DERIVED. S3 bucket names are global across every AWS account, so
  # a derived name is a name that may already belong to somebody else.
  bucket_name = var.storage_bucket_name

  noncurrent_retain_days = 7

  tags = local.tags
}

# ── Traffic: certificate, load balancer, DNS, WAF ────────────────────────────
#
# THE LAYER THE PREVIOUS PHASE DID NOT HAVE. Seven modules stood up a VPC, a
# cluster, a database, a cache, a registry and a secret store, and nothing could
# route a request to any of it. Ordering here is expressed as data dependencies
# rather than as `depends_on`: the certificate must be ISSUED before the HTTPS
# listener exists, and the load balancer must exist before a DNS record can
# alias to it.

module "acm" {
  source = "../../modules/acm"

  project     = var.project
  environment = local.environment

  domain_name    = var.domain_name
  hosted_zone_id = var.hosted_zone_id

  tags = local.tags
}

module "alb" {
  source = "../../modules/alb"

  project     = var.project
  environment = local.environment
  account_id  = var.account_id

  vpc_id            = module.network.vpc_id
  public_subnet_ids = module.network.public_subnet_ids
  security_group_id = module.network.alb_security_group_id

  # From the VALIDATION resource, not from the certificate. A listener created
  # against a PENDING_VALIDATION certificate is accepted by AWS and then fails
  # the TLS handshake for every visitor.
  certificate_arn = module.acm.certificate_arn

  access_logs_bucket_name = var.access_logs_bucket_name

  # No deletion protection in staging; on in production. Staging is meant to be
  # destroyable, which is most of what it is for.
  enable_deletion_protection = false

  target_groups = {
    api = {
      port = 8000
      # DEEP, NOT A STATIC 200. `/health` resolves a pooled database session AND
      # issues a Redis round trip, so a task with a wrong DSN or an unreachable
      # Redis fails this check and the ECS circuit breaker rolls the deploy back.
      # A static 200 would promote that same task.
      health_path             = "/health"
      health_interval_seconds = 30
      health_timeout_seconds  = 10
    }
    frontend = {
      port                    = 3000
      health_path             = "/"
      health_interval_seconds = 30
      health_timeout_seconds  = 10
    }
  }

  default_target_group = "frontend"
  public_target_group  = "api"

  # THE ENUMERATED UNAUTHENTICATED SURFACE (RBAC §15 and §33).
  #
  # These are the paths behind the public job link a candidate reaches from
  # LinkedIn or a forwarded email, mounted at both API prefixes because the
  # handler object is mounted twice and a link already sitting in somebody's
  # inbox may carry either. The handler returns `PublicJobOut`, which carries no
  # status, no creator, no compensation and no approval trail, and it 404s an
  # unpublished, archived or expired job without revealing which of the three.
  #
  # THAT is what makes RBAC §33 hold, not the fact that the id is a UUID:
  # "Obscurity is NOT authorization", and the projection is the half doing the
  # work. The load balancer contributes the narrower half, which is that these
  # patterns and nothing else reach the API before the application is asked.
  #
  # Adding to this list widens the unauthenticated surface of the product.
  # `backend/tests/test_deploy_secret_hygiene.py` reads it back out of this file
  # so that an addition fails a test rather than only appearing in a rule set.
  public_path_patterns = [
    "/api/v1/jobs/public/*",
    "/api/v2/jobs/public/*",
  ]

  routes = {
    # Everything else under /api is the application's to authorize. It reaches
    # the API target group by routing, and `require_capability` decides whether
    # the caller may actually have it.
    api = {
      priority      = 100
      target_group  = "api"
      path_patterns = ["/api/*", "/docs", "/openapi.json"]
    }
  }

  tags = local.tags
}

module "dns" {
  source = "../../modules/dns"

  hosted_zone_id = var.hosted_zone_id
  hostnames      = [var.domain_name]

  alb_dns_name = module.alb.dns_name
  # The LOAD BALANCER's zone, which is an AWS-owned zone and is NOT
  # `var.hosted_zone_id`. Passing the product's own zone here produces a record
  # that resolves to nothing.
  alb_zone_id = module.alb.zone_id
}

module "waf" {
  source = "../../modules/waf"

  project     = var.project
  environment = local.environment

  # BUILT AND OFF (spec-doc6 §13.2). `enabled = false` creates nothing at all,
  # rather than a permissive web ACL that costs money and proves nothing.
  # Turning it on is one line, and the procedure in `docs/DEPLOY_AWS.md` says to
  # turn it on in `count_only` mode first: the managed rule sets inspect request
  # bodies, and this product's request bodies are resumes, client-written job
  # descriptions and interview answers, which is a corpus no generic rule set
  # was ever tuned against.
  enabled    = false
  count_only = true

  alb_arn     = module.alb.arn
  kms_key_arn = aws_kms_key.this.arn

  tags = local.tags
}

# ── Compute ──────────────────────────────────────────────────────────────────

module "ecs" {
  source = "../../modules/ecs"

  project     = var.project
  environment = local.environment
  region      = var.region

  private_subnet_ids    = module.network.private_subnet_ids
  ecs_security_group_id = module.network.ecs_security_group_id

  # The Cloud Map namespace resolves inside this VPC and nowhere else.
  vpc_id              = module.network.vpc_id
  discovery_namespace = local.internal_namespace

  secret_policy_arns  = module.secrets.policy_arns
  s3_policy_arn       = module.s3.access_policy_arn
  ecr_repository_arns = values(module.ecr.repository_arns)

  kms_key_arn        = aws_kms_key.this.arn
  log_retention_days = 14

  # ECS Exec ON in staging, OFF in production. A shell in a container holding
  # real candidate data is a different thing from a shell in one holding seed
  # data, and the difference should be a decision rather than an inheritance.
  enable_execute_command = true

  common_environment = {
    # `ENVIRONMENT`, NOT `APP_ENV`. `Settings.environment` reads this name,
    # and `APP_ENV` is a Cloud Run convention from the previous platform that
    # nothing has ever read. With it set, every deployed service believed it
    # was in `development` -- the default -- which is what the pilot's first
    # agent run reported in its own log.
    ENVIRONMENT                   = local.environment
    AWS_REGION                    = var.region
    S3_BUCKET                     = module.s3.bucket_name
    S3_KMS_KEY_ID                 = aws_kms_key.this.arn
    EMBEDDING_DIMENSIONS          = "1024"
    RESUME_SIGNED_URL_TTL_SECONDS = "300"
    # The origin the product is actually served on. `jobs.public_job_url` builds
    # the candidate-facing application link from it and the CORS allowlist in
    # `app/main.py` is keyed on it, so a wrong value here is a job link that
    # goes nowhere and a browser that refuses every API call.
    FRONTEND_URL = "https://${var.domain_name}"
    # THE PLATFORM SECURITY SENDER. Every security code and account notice
    # leaves from this identity and never from a tenant's corporate sender
    # (`services/security_email`). The API validates it at boot; the task
    # worker, which actually sends, carries the same two values below.
    PLATFORM_SECURITY_SENDER_EMAIL = var.platform_security_sender_email
    PLATFORM_SECURITY_SENDER_NAME  = "Vivekium"
  }

  services = {
    api = {
      image         = "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
      cpu           = 512
      memory        = 1024
      desired_count = 1
      max_count     = 2
      port          = 8000
      health_path   = "/health"
      # REGISTERS THE TASKS WITH THE LOAD BALANCER. Without this the service
      # runs, is attached to no target group, and serves no traffic at all while
      # every dashboard reports it healthy.
      target_group_arn = module.alb.target_group_arns["api"]
      needs_s3         = true
      # Where the proctoring pipeline posts a fifteen-second audio chunk for a
      # speaker count. NOT a credential, so it is a plain environment entry:
      # it is an internal hostname that resolves in this VPC only. An empty
      # value would mean audio analysis is unavailable, which the report states
      # plainly rather than reading as "no second voice was heard".
      environment = {
        PROCTORING_ANALYSIS_SERVICE_URL = local.analysis_service_url
      }
      # The backend writes nothing to disk by design -- resume bytes never
      # persist on the application filesystem -- so this enforces an invariant
      # the code already claims.
      readonly_root = true
      secrets = {
        DATABASE_URL                  = module.secrets.secret_arns["DATABASE_URL"]
        REDIS_URL                     = module.secrets.secret_arns["REDIS_URL"]
        JWT_SECRET                    = module.secrets.secret_arns["JWT_SECRET"]
        OPENAI_GPT_TERRA              = module.secrets.secret_arns["OPENAI_GPT_TERRA"]
        OPENAI_GPT_LUNA               = module.secrets.secret_arns["OPENAI_GPT_LUNA"]
        VOYAGE_CONTEXT_4              = module.secrets.secret_arns["VOYAGE_CONTEXT_4"]
        VOYAGE_RERANK_2_5             = module.secrets.secret_arns["VOYAGE_RERANK_2_5"]
        FIREBASE_SERVICE_ACCOUNT_JSON = module.secrets.secret_arns["FIREBASE_SERVICE_ACCOUNT_JSON"]
        RAZORPAY_KEY_SECRET           = module.secrets.secret_arns["RAZORPAY_KEY_SECRET"]
        # WITHOUT THIS THE BILLING WEBHOOK CANNOT VERIFY A SIGNATURE.
        # The secret existed in `module.secrets` and was mounted on nothing,
        # and the handler used to fall through and PROCESS an unsigned event
        # when it was absent, which made credit issuance an unauthenticated
        # POST. The handler now refuses when it is missing, so the absence is
        # loud instead of silent, and this line is what makes it present.
        RAZORPAY_WEBHOOK_SECRET = module.secrets.secret_arns["RAZORPAY_WEBHOOK_SECRET"]
        # THE INBOUND WEBHOOK'S SHARED SECRET. `POST /verification/inbound-email`
        # is a PUBLIC route that writes into verification requests, BGV threads
        # and conversations, and its only protection was that a caller had to
        # know a per-thread token -- which travels by email, so it exists in
        # every mailbox that ever received or forwarded one of these threads.
        # An empty setting leaves the route OPEN and logs
        # `verification.inbound_unauthenticated` on every call.
        #
        # MOUNTED HERE EVEN THOUGH NOTHING IN THIS ENVIRONMENT RELAYS MAIL.
        # SES receiving and the inbound-mail function live in the pilot
        # composition only, so with the secret set this route accepts nobody
        # here, which is the correct answer for an endpoint with no legitimate
        # caller. The alternative -- leaving it unset because "nothing uses it"
        # -- is an environment whose public write endpoint is open and whose
        # openness is invisible in a diff.
        INBOUND_WEBHOOK_SECRET = module.secrets.secret_arns["INBOUND_WEBHOOK_SECRET"]
      }
    }

    # THE ASSESSMENT AGENT: the long half of background work.
    #
    # A task definition and NOTHING ELSE: no service, no desired count, no
    # autoscaling. `readypick-assessment-trigger` calls RunTask against this
    # family when an assessment, a matrix compilation or a matching pass is
    # dispatched; the container runs that one piece of work and exits.
    #
    # Fargate does not scale to zero, which is precisely why long AI work that
    # runs a few times an hour is a task definition rather than a service.
    agent = {
      image = "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
      # The entry point role in docker-entrypoint.sh: read one dispatched task
      # from the environment, run it, exit.
      command       = ["agent"]
      cpu           = 1024
      memory        = 2048
      on_demand     = true
      desired_count = 0
      max_count     = 0
      # FARGATE'S MAXIMUM, and it is a SIGTERM grace period rather than a
      # ceiling: this task runs for as long as its work takes and stops when the
      # process exits. Two minutes is what an interrupted run gets to finish the
      # piece it is on, instead of the 30-second default that would leave a
      # conversation scored with no report written.
      stop_timeout = 120
      needs_s3     = true
      # NOT read-only: resume and project parsing write temp files.
      readonly_root = false
      # The agent runs the proctoring reconciliation work, which re-reads a
      # session, so it reaches the analysis service on the same name the api
      # does.
      environment = {
        PROCTORING_ANALYSIS_SERVICE_URL = local.analysis_service_url
      }
      # NO FIREBASE KEY. A background task never authenticates a browser
      # session, so it has no business being able to read the service account.
      secrets = {
        DATABASE_URL      = module.secrets.secret_arns["DATABASE_URL"]
        REDIS_URL         = module.secrets.secret_arns["REDIS_URL"]
        OPENAI_GPT_TERRA  = module.secrets.secret_arns["OPENAI_GPT_TERRA"]
        OPENAI_GPT_LUNA   = module.secrets.secret_arns["OPENAI_GPT_LUNA"]
        VOYAGE_CONTEXT_4  = module.secrets.secret_arns["VOYAGE_CONTEXT_4"]
        VOYAGE_RERANK_2_5 = module.secrets.secret_arns["VOYAGE_RERANK_2_5"]
      }
    }

    # THE MIGRATION JOB. A task definition with no service, run as a one-shot
    # task by `scripts/run-migration.sh` BEFORE the services are updated.
    #
    # Never a step in the API's startup: several tasks boot at once during a
    # rollout and would race each other through the same migration. Alembic
    # takes a lock, so the losers crash-loop.
    #
    # ONE SECRET, the DSN, and nothing else at all. It connects, applies DDL and
    # exits; anything else it could read is reach its work does not need, and
    # `tests/test_deploy_secret_hygiene.py` asserts exactly that.
    migrate = {
      image = "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
      # The entry point role that runs `alembic upgrade head` and exits.
      command       = ["migrate"]
      cpu           = 512
      memory        = 1024
      on_demand     = true
      desired_count = 0
      max_count     = 0
      # Fargate's maximum, and only a SIGTERM grace period: the migration's real
      # bound is `run-migration.sh`'s own 900-second wait, which polls for
      # STOPPED and reads the exit code. That script deliberately does NOT treat
      # a timeout as success, because a half-applied schema is the one state a
      # deploy must never be layered on top of.
      stop_timeout  = 120
      readonly_root = true
      secrets = {
        DATABASE_URL = module.secrets.secret_arns["DATABASE_URL"]
      }
    }

    frontend = {
      image            = "${module.ecr.repository_urls["frontend"]}:${var.image_tag}"
      cpu              = 512
      memory           = 1024
      desired_count    = 1
      max_count        = 2
      port             = 3000
      health_path      = "/"
      target_group_arn = module.alb.target_group_arns["frontend"]
      readonly_root    = false # Next.js writes its own cache
      # The frontend holds NO secrets. The Razorpay key id it needs is public
      # and arrives at runtime on the API's subscribe and purchase responses,
      # which is why it was
      # never a NEXT_PUBLIC_ build variable.
      secrets = {}
    }

    # THE PROCTORING ANALYSIS SERVICE (analysis-service/).
    #
    # Speaker counting over a fifteen-second audio chunk, and an AI-text
    # estimate that ships disabled. Its own image and its own service, for
    # three reasons worth keeping separate:
    #
    #   IT CARRIES THE MODEL LIBRARIES. torch, pyannote.audio and transformers
    #   are several hundred megabytes the api and the worker never load, and
    #   an inference call that pinned a request worker would cost a candidate
    #   mid-assessment their next question.
    #
    #   IT HANDLES THE ONE MEDIA TYPE THAT LEAVES THE BROWSER. The chunk is
    #   decoded from an in-memory buffer and destroyed; nothing in the image
    #   writes audio anywhere, and the service is deliberately not next to code
    #   that persists files.
    #
    #   IT IS NOT PUBLIC. No target group, no listener rule, no path through
    #   the load balancer. It is reachable at its Cloud Map name from tasks in
    #   the ECS security group and from nothing else.
    analysis = {
      image = "${module.ecr.repository_urls["analysis"]}:${var.image_tag}"
      # CPU inference on a fifteen-second chunk. The memory figure is the
      # binding one: the diarization pipeline holds three models resident.
      cpu           = 2048
      memory        = 8192
      desired_count = 1
      max_count     = 2
      port          = 8100
      # The body says which component loaded. A container with no token is up
      # and honest rather than restarting forever over a decision nobody made
      # by mistake, so the check asks whether the process serves.
      health_path = "/health"
      # NO TARGET GROUP. Reached by name, inside the VPC, on 8100.
      discoverable = true
      # READ-ONLY ROOT. The service writes nothing to disk by design, and the
      # one exception is the import-time caches torch and matplotlib insist on:
      # the image points MPLCONFIGDIR, TORCH_HOME and XDG_CACHE_HOME at /tmp,
      # and the mount below is the only writable path in the container.
      readonly_root  = true
      writable_paths = ["/tmp"]
      # The Hugging Face token, and nothing else. The diarization models are
      # gated: the licence is accepted per account, so the service refuses to
      # load them without a token even though the weights are baked into the
      # image. It holds no DSN, no Redis and no model-provider key, because
      # all it is handed is audio and all it answers is a speaker count.
      secrets = {
        HUGGINGFACE_TOKEN = module.secrets.secret_arns["HUGGINGFACE_TOKEN"]
      }
    }
  }

  tags = local.tags
}

# ── Background work ──────────────────────────────────────────────────────────
#
# The short half of background work: one Lambda runs every task routed to
# Route.LAMBDA, and the agent task definition above runs every Route.ECS one.
# See `infra/environments/pilot/main.tf` for the full argument; the short
# version is that short work is billed per invocation, long work is one
# on-demand Fargate task per dispatch, and the schedule is a managed
# scheduler with no singleton process to lose.

module "lambda" {
  source = "../../modules/lambda"

  project     = var.project
  environment = local.environment
  region      = var.region
  account_id  = var.account_id

  vpc_subnet_ids     = module.network.private_subnet_ids
  security_group_ids = [module.network.ecs_security_group_id]

  # ARM64, matching the `ecs` module's default. The same backend image backs the
  # API service, the on-demand agent and three of these four functions, so one
  # architecture is not a preference here, it is a correctness requirement.
  architecture = "arm64"

  secret_policy_arns = module.secrets.policy_arns

  kms_key_arn        = aws_kms_key.this.arn
  log_retention_days = 14
  failure_topic_arn  = aws_sns_topic.alarms.arn

  functions = {
    "task-worker" = {
      package     = "image"
      description = "Every short background task: delivery, resume parsing, the reconciliation sweeps."
      image_uri   = "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
      handler     = "app.workers.entrypoints.lambda_worker.lambda_handler"
      # ITSELF, AND ONLY ITSELF. A Route.LAMBDA sweep that fans out to
      # Route.LAMBDA work is this function invoking this function: one function
      # serves every short task. `pickready.reconcile_context_index` is the
      # first task in the product that does it, and production answered
      # AccessDeniedException because no earlier sweep had ever needed the
      # grant -- every previous one dispatched to Route.ECS, which goes through
      # ecs:RunTask and is a different permission.
      invokable_function_keys = ["task-worker"]
      memory_mb               = 1024
      timeout_seconds         = 600
      reserved_concurrency    = var.reserve_lambda_concurrency ? 10 : null
      secret_policy_key       = "task-worker"
      # The SAME map the ECS services use. ECS injects these; Lambda has no
      # equivalent, so the function fetches them at cold start with the
      # policy below. Only the ARNs are here.
      secrets = {
        DATABASE_URL      = module.secrets.secret_arns["DATABASE_URL"]
        REDIS_URL         = module.secrets.secret_arns["REDIS_URL"]
        OPENAI_GPT_TERRA  = module.secrets.secret_arns["OPENAI_GPT_TERRA"]
        OPENAI_GPT_LUNA   = module.secrets.secret_arns["OPENAI_GPT_LUNA"]
        VOYAGE_CONTEXT_4  = module.secrets.secret_arns["VOYAGE_CONTEXT_4"]
        VOYAGE_RERANK_2_5 = module.secrets.secret_arns["VOYAGE_RERANK_2_5"]
        SMTP_PASSWORD     = module.secrets.secret_arns["SMTP_PASSWORD"]
        TAVILY_API_KEY    = module.secrets.secret_arns["TAVILY_API_KEY"]
      }
      environment = {
        # AWS_REGION IS NOT SET HERE. It is one of Lambda's RESERVED keys: the
        # runtime injects it with the function's own region, and CreateFunction
        # answers 400 for any request that also supplies it. Nothing is lost --
        # `Settings.aws_region` reads that same variable, so boto3 and the
        # application agree with the platform rather than with a literal.
        ENVIRONMENT                     = local.environment
        S3_BUCKET                       = module.s3.bucket_name
        S3_KMS_KEY_ID                   = aws_kms_key.this.arn
        FRONTEND_URL                    = "https://${var.domain_name}"
        EMBEDDING_DIMENSIONS            = "1024"
        RESUME_SIGNED_URL_TTL_SECONDS   = "300"
        TASK_DISPATCH_BACKEND           = "aws"
        PROCTORING_ANALYSIS_SERVICE_URL = local.analysis_service_url
        # `pickready.send_security_email` runs here, so this is the hop that
        # names the platform sender as the From address.
        PLATFORM_SECURITY_SENDER_EMAIL = var.platform_security_sender_email
        PLATFORM_SECURITY_SENDER_NAME  = "Vivekium"
      }
    }

    "jd-gen" = {
      package              = "image"
      description          = "Writes one job description draft. Invoked synchronously by the request handler that is already waiting for it."
      image_uri            = "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
      handler              = "app.workers.entrypoints.agents.jd_generation_handler"
      memory_mb            = 512
      timeout_seconds      = 600
      reserved_concurrency = var.reserve_lambda_concurrency ? 10 : null
      secret_policy_key    = "jd-gen"
      # The SAME map the ECS services use. ECS injects these; Lambda has no
      # equivalent, so the function fetches them at cold start with the
      # policy below. Only the ARNs are here.
      secrets = {
        DATABASE_URL     = module.secrets.secret_arns["DATABASE_URL"]
        OPENAI_GPT_TERRA = module.secrets.secret_arns["OPENAI_GPT_TERRA"]
        OPENAI_GPT_LUNA  = module.secrets.secret_arns["OPENAI_GPT_LUNA"]
      }
      environment = {
        # AWS_REGION IS NOT SET HERE. It is one of Lambda's RESERVED keys: the
        # runtime injects it with the function's own region, and CreateFunction
        # answers 400 for any request that also supplies it. Nothing is lost --
        # `Settings.aws_region` reads that same variable, so boto3 and the
        # application agree with the platform rather than with a literal.
        ENVIRONMENT           = local.environment
        TASK_DISPATCH_BACKEND = "aws"
      }
    }

    "company-profile" = {
      package              = "image"
      description          = "Drafts one company's three profile sections from public sources."
      image_uri            = "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
      handler              = "app.workers.entrypoints.agents.company_profile_handler"
      memory_mb            = 512
      timeout_seconds      = 300
      reserved_concurrency = var.reserve_lambda_concurrency ? 5 : null
      secret_policy_key    = "company-profile"
      # The SAME map the ECS services use. ECS injects these; Lambda has no
      # equivalent, so the function fetches them at cold start with the
      # policy below. Only the ARNs are here.
      secrets = {
        DATABASE_URL     = module.secrets.secret_arns["DATABASE_URL"]
        OPENAI_GPT_TERRA = module.secrets.secret_arns["OPENAI_GPT_TERRA"]
        OPENAI_GPT_LUNA  = module.secrets.secret_arns["OPENAI_GPT_LUNA"]
        TAVILY_API_KEY   = module.secrets.secret_arns["TAVILY_API_KEY"]
      }
      environment = {
        # AWS_REGION IS NOT SET HERE. It is one of Lambda's RESERVED keys: the
        # runtime injects it with the function's own region, and CreateFunction
        # answers 400 for any request that also supplies it. Nothing is lost --
        # `Settings.aws_region` reads that same variable, so boto3 and the
        # application agree with the platform rather than with a literal.
        ENVIRONMENT           = local.environment
        TASK_DISPATCH_BACKEND = "aws"
      }
    }

    # THE ONLY ZIP, AND THE ONLY THING HOLDING iam:PassRole. Thirty lines and
    # boto3, and it must stay that way: passing a role is a privilege-escalation
    # primitive, and the mitigation is that this function's whole source fits on
    # a screen.
    "assessment-trigger" = {
      package                       = "zip"
      description                   = "Starts one on-demand assessment-agent task and returns. Reads no secret."
      source_dir                    = "${path.root}/../../../lambda/assessment_trigger"
      handler                       = "handler.lambda_handler"
      memory_mb                     = 128
      timeout_seconds               = 30
      run_task_cluster_arn          = module.ecs.cluster_arn
      run_task_task_definition_arns = [module.ecs.on_demand_task_definition_arns["agent"]]
      # Both roles: RunTask passes the task role the container runs as AND the
      # execution role the ECS agent uses to pull the image and fetch secrets
      # before it starts, so a grant naming only the first fails at RunTask.
      run_task_role_arns = [
        module.ecs.task_role_arns["agent"],
        module.ecs.execution_role_arns["agent"],
      ]
      environment = {
        # AWS_REGION IS NOT SET HERE. It is one of Lambda's RESERVED keys: the
        # runtime injects it with the function's own region, and CreateFunction
        # answers 400 for any request that also supplies it. Nothing is lost --
        # `Settings.aws_region` reads that same variable, so boto3 and the
        # application agree with the platform rather than with a literal.
        ECS_CLUSTER            = module.ecs.cluster_name
        ECS_TASK_DEFINITION    = module.ecs.on_demand_task_families["agent"]
        ECS_CONTAINER_NAME     = "agent"
        PRIVATE_SUBNET_IDS     = join(",", module.network.private_subnet_ids)
        ECS_SECURITY_GROUP_IDS = module.network.ecs_security_group_id
      }
    }
  }

  tags = local.tags
}

# ── The task worker's object-store grant ─────────────────────────────────────
#
# THE TASK WORKER HAD NONE, IN ANY ENVIRONMENT. The ECS services get the
# application prefixes through `needs_s3`; the Lambda that runs every short
# task got only its secrets, logs and VPC policies. Yet the sweeps that delete
# stored objects run there: `purge_assessment_media` (owner decision D4),
# `reconcile_assessment_recordings` (the raw-deletion retry),
# `purge_closed_job_assessments` and the erasure reconciler. Each would have
# answered AccessDenied on its first real object, counted a failure, and come
# back the next hour to fail again. Found on 2026-09-24 while wiring the
# recording's retry sweep; nothing had ever been deleted in an applied
# environment, which is why nothing had ever reported it.
resource "aws_iam_role_policy_attachment" "task_worker_s3" {
  role       = element(split("/", module.lambda.execution_role_arns["task-worker"]), 1)
  policy_arn = module.s3.access_policy_arn
}

# ── The schedule ─────────────────────────────────────────────────────────────
#
# MIRRORS `backend/app/workers/schedule.py`, and
# `backend/tests/test_schedule_parity.py` reads both and fails if they drift.

module "scheduler" {
  source = "../../modules/scheduler"

  project     = var.project
  environment = local.environment
  account_id  = var.account_id

  target_function_arn = module.lambda.function_arns["task-worker"]
  timezone            = "Asia/Kolkata"

  schedules = {
    "readypick-refresh-dashboard-views" = {
      task            = "pickready.refresh_dashboard_views"
      rate_expression = "rate(5 minutes)"
    }
    "readypick-remind-unsaved-skills" = {
      task            = "pickready.remind_unsaved_skills"
      rate_expression = "rate(60 minutes)"
    }
    "readypick-reconcile-job-setup" = {
      task            = "pickready.reconcile_job_setup"
      rate_expression = "rate(15 minutes)"
    }
    "readypick-reconcile-assessment-credits" = {
      task            = "pickready.reconcile_assessment_credits"
      rate_expression = "rate(60 minutes)"
    }
    "readypick-reconcile-project-intake" = {
      task            = "pickready.reconcile_project_intake"
      rate_expression = "rate(60 minutes)"
    }
    "readypick-reconcile-proctoring-sessions" = {
      task            = "pickready.reconcile_proctoring_sessions"
      rate_expression = "rate(60 minutes)"
    }
    "readypick-purge-proctoring-events" = {
      task            = "pickready.purge_proctoring_events"
      rate_expression = "rate(60 minutes)"
    }
    # Owner decision D4: a session recording is purged at the earlier of its
    # stored purge date (session end plus 90 days) and its job's closure
    # purge. The S3 lifecycle rule in modules/s3 is only the backstop; this is
    # the HEAD-confirmed deletion that stamps the row.
    "readypick-purge-assessment-media" = {
      task            = "pickready.purge_assessment_media"
      rate_expression = "rate(60 minutes)"
    }
    # The recording pipeline's repair: raw segment deletions that did not
    # confirm, recordings whose tab closed before finalize, finalized
    # recordings no processing run picked up, and runs whose task was killed
    # mid-transcode. The Terraform half of the entry
    # in app/workers/schedule.py; tests/test_schedule_parity.py fails on drift.
    "readypick-reconcile-assessment-recordings" = {
      task            = "pickready.reconcile_assessment_recordings"
      rate_expression = "rate(60 minutes)"
    }
    # Change request 22, owner ruling 2026-09-22: closing a job WITHHOLDS its
    # assessment data for thirty days instead of deleting it inline, and this
    # sweep is the half that makes the thirty days real. The Terraform half of
    # the entry in app/workers/schedule.py; tests/test_schedule_parity.py
    # fails on drift, and this is the SILENT direction of that failure -- a
    # retention window with no rule behind it produces the same empty log as
    # one with nothing to delete, while every assessed candidate's promise
    # quietly stops being kept. Hourly, because it deletes stored objects one
    # network call at a time and a store that refuses must be retried inside
    # the same day.
    "readypick-purge-closed-job-assessments" = {
      task            = "pickready.purge_closed_job_assessments"
      rate_expression = "rate(60 minutes)"
    }
    "readypick-sweep-consent-lifecycle" = {
      task            = "pickready.sweep_consent_lifecycle"
      rate_expression = "rate(1440 minutes)"
    }
    # Email 3 of the vivekium BGV flow (feature 4): the day-3 chase for an
    # employer who has not answered. Daily; reminder_sent_at is the
    # once-only latch, so running late delays the letter, never duplicates.
    # The object half of an erasure (feature 7, change 15). The Terraform half
    # of the entry in app/workers/schedule.py; tests/test_schedule_parity.py
    # fails on drift. Hourly, and it never gives up: a request that cannot be
    # finished is escalated in the log, never abandoned.
    "readypick-reconcile-candidate-erasures" = {
      task            = "pickready.reconcile_candidate_erasures"
      rate_expression = "rate(60 minutes)"
    }
    "readypick-sweep-bgv-reminders" = {
      task            = "pickready.sweep_bgv_reminders"
      rate_expression = "rate(1440 minutes)"
    }
    # RPN-AI-UP-001 W2.2. The Terraform half of the entry in
    # app/workers/schedule.py. tests/test_schedule_parity.py fails on drift,
    # because an entry in Python with no rule here is the SILENT half: a sweep
    # does nothing when there is nothing to repair, so "not running" and
    # "nothing to do" produce the same empty log.
    "readypick-reconcile-context-index" = {
      task            = "pickready.reconcile_context_index"
      rate_expression = "rate(60 minutes)"
    }
    # PLAN-p5 WP5-E. The Terraform half of the entry in app/workers/schedule.py:
    # re-embeds chunks whose vector is NULL, from a retired model or contract,
    # or the wrong width. retrieval_repair_sweep_batch caps a pass; 0 pauses it.
    "readypick-repair-semantic-index" = {
      task            = "pickready.repair_semantic_index"
      rate_expression = "rate(60 minutes)"
    }
    # Registered since the credit work and scheduled by nothing until now: it
    # was dispatched only when a bundle was granted, so a report lost to a
    # failed dispatch or a killed container stayed lost, for a candidate who
    # had done the work and a customer who had been charged.
    "readypick-release-held-assessments" = {
      task            = "pickready.release_held_assessments"
      rate_expression = "rate(60 minutes)"
    }
    # Change request 25. A credit lot reaching its three-month expiry writes
    # the ledger debit for whatever was left on it, so the balance stays the
    # plain SUM of the ledger. Every gate already expires on read, so this is
    # for the idle account whose figure the Provider overview reads. The
    # Terraform half of the entry in app/workers/schedule.py.
    "readypick-expire-credit-lots" = {
      task            = "pickready.expire_credit_lots"
      rate_expression = "rate(1440 minutes)"
    }
    # Phase 6 WP6-C. The Terraform half of the entry in
    # app/workers/schedule.py; tests/test_schedule_parity.py fails on drift.
    # Re-dispatches candidate emails whose after-commit send was lost; never
    # resends a row a worker has already claimed.
    "readypick-reconcile-queued-emails" = {
      task            = "pickready.reconcile_queued_emails"
      rate_expression = "rate(15 minutes)"
    }
    # Phase 4 WP-4B2. The Terraform half of the two entries in
    # app/workers/schedule.py; tests/test_schedule_parity.py fails on drift.
    # Re-dispatches coding submissions nothing is working on and hands a
    # completed conversation to scoring once its coding work is done.
    "readypick-reconcile-coding-submissions" = {
      task            = "pickready.reconcile_coding_submissions"
      rate_expression = "rate(15 minutes)"
    }
    # One canary through the code sandbox; logs status=disabled while
    # CODE_EXECUTION_BACKEND is disabled.
    "readypick-probe-code-execution" = {
      task            = "pickready.probe_code_execution"
      rate_expression = "rate(5 minutes)"
    }
  }

  tags = local.tags
}

# ── Observability ────────────────────────────────────────────────────────────

resource "aws_sns_topic" "alarms" {
  name              = "${var.project}-${local.environment}-alarms"
  kms_master_key_id = aws_kms_key.this.arn
  tags              = local.tags
}

data "aws_iam_policy_document" "alarm_topic" {
  statement {
    sid     = "AllowCloudWatchAlarms"
    effect  = "Allow"
    actions = ["SNS:Publish"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    resources = [aws_sns_topic.alarms.arn]
    # Without an account condition a named service principal is the
    # confused-deputy shape: it reads as narrow and is reachable from any
    # account using that service.
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceAccount"
      values   = [var.account_id]
    }
  }

  statement {
    sid     = "AllowLambdaFailureDestinations"
    effect  = "Allow"
    actions = ["SNS:Publish"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    resources = [aws_sns_topic.alarms.arn]
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceAccount"
      values   = [var.account_id]
    }
  }

  # THE BUDGET NOTIFIES THROUGH THIS TOPIC, so it needs to be allowed to
  # publish to it. Same shape and same confused-deputy condition as the two
  # statements above; `aws:SourceArn` is added because AWS's own documented
  # example for a Budgets notification topic carries it, and a budget ARN is
  # the one thing that can narrow this further.
  statement {
    sid     = "AllowBudgetNotifications"
    effect  = "Allow"
    actions = ["SNS:Publish"]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }
    resources = [aws_sns_topic.alarms.arn]
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceAccount"
      values   = [var.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "AWS:SourceArn"
      values   = ["arn:aws:budgets::${var.account_id}:budget/*"]
    }
  }
}

resource "aws_sns_topic_policy" "alarms" {
  arn    = aws_sns_topic.alarms.arn
  policy = data.aws_iam_policy_document.alarm_topic.json
}

# AN EMAIL SUBSCRIPTION IS PENDING until its recipient clicks the confirmation
# link, and Terraform reports it as created either way.
resource "aws_sns_topic_subscription" "alarm_email" {
  for_each = toset(var.alarm_emails)

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

module "observability" {
  source = "../../modules/observability"

  project     = var.project
  environment = local.environment
  region      = var.region

  alarm_topic_arn = aws_sns_topic.alarms.arn

  enable_alb_alarms        = true
  load_balancer_arn_suffix = module.alb.arn_suffix
  target_group_arn_suffix  = module.alb.target_group_arn_suffixes["api"]

  db_instance_id = module.rds.instance_id
  function_names = module.lambda.function_names

  # There is no ECS metric for "a task exited non-zero", and the Lambda that
  # started it returned as soon as RunTask was accepted, so a metric filter over
  # the agent's own `ecs_task.failed` line is the only report of the failure.
  agent_log_group_name = module.ecs.log_group_names["agent"]

  # PLAN-p5 WP5-E. The metric filter over `rag.repair.degraded`, the only
  # report of a semantic index repair sweep that cannot embed.
  task_worker_log_group_name = module.lambda.log_group_names["task-worker"]

  # ADDED 2026-09-17. Both were visible on the dashboard and watched by
  # nothing. See the module for what each alarm catches.
  #
  # The connection threshold is roughly 80 percent of what RDS derives from
  # this instance class: db.t4g.small (2 GiB, ~225 connections).
  # It is stated per environment because the ceiling is a property of the
  # instance and a shared default would be wrong everywhere but here.
  db_connection_alarm_threshold = 180

  # Computed in `locals` rather than read off the module, because a
  # `for_each` over a resource attribute that is unknown until apply cannot
  # be planned.
  redis_cluster_ids = local.redis_cluster_ids

  kms_key_arn = aws_kms_key.this.arn
  tags        = local.tags
}

# ── The bill ────────────────────────────────────────────────────
#
# THERE WAS NO BUDGET AND NO BILLING ALARM ANYWHERE IN THIS REPOSITORY, in any
# environment, before 2026-09-17.
#
# That gap is not the same shape as a missing operational alarm. Every alarm
# above watches something that breaks loudly; cost does the opposite. A runaway
# NAT gateway, a Fargate service that scaled up and never came back down, a
# model-calling task retrying in a loop, an S3 prefix whose lifecycle rule does
# not reach it -- all of them look exactly like a working system, for a whole
# month, until an invoice arrives. This platform already runs several things
# that cannot scale to zero (Fargate says so in its own module comment), so the
# floor is real and the ceiling is unwatched.
#
# THE LIMIT IS A VARIABLE AND ITS DEFAULT IS NOT A JUDGEMENT ABOUT THIS
# ENVIRONMENT. Read `var.monthly_budget_usd`: it is deliberately conservative
# so that an unset value alerts EARLY and noisily rather than late and quietly,
# and the owner is expected to replace it with a real number.
#
# NO COST FILTER, AND THAT IS DELIBERATE.
#
# The obvious refinement is to scope the budget to this environment's resources
# with a `TagKeyValue` filter on `Environment`. It is not used, because a cost
# allocation tag has to be ACTIVATED in the Billing console before it appears
# in cost data at all, and Terraform cannot do that. A filter on an
# unactivated tag matches nothing, so the budget reports a spend of zero and
# never notifies -- a billing alarm that is silent because it is misconfigured
# is worse than none, since it also stops anybody from looking.
#
# So this measures the ACCOUNT. If the three environments share one account,
# all three budgets watch the same total and the owner will get duplicate
# notifications at the lowest limit of the three: noisy, visible, and safe.
# Separate accounts per environment is the real answer, and adding the tag
# filter is correct the day somebody has activated the tag and can say so.
resource "aws_budgets_budget" "monthly" {
  name = "${var.project}-${local.environment}-monthly"

  budget_type = "COST"
  time_unit   = "MONTHLY"
  # The provider takes this as a string. Converted here rather than declaring
  # the variable as a string, so a non-numeric value fails in the variable and
  # not in an API call.
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"

  # `time_period_start` is deliberately omitted. The provider computes it, and
  # a hardcoded date is a value that is wrong from the day after it is written
  # and produces drift on every plan thereafter.

  # THREE NOTIFICATIONS, AND THE FORECAST ONE IS THE ONLY USEFUL ONE.
  #
  # An ACTUAL notification arrives after the money is spent, which for a
  # monthly budget can be three weeks after the thing that caused it started.
  # FORECASTED fires when the run rate says the month will end over the limit,
  # which is days rather than weeks, and it is the notification that can still
  # change the outcome. The two actual thresholds are kept because a forecast
  # is a prediction and a spend is a fact.
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alarms.arn]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.alarms.arn]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "FORECASTED"
    subscriber_sns_topic_arns = [aws_sns_topic.alarms.arn]
  }

  # The topic must be able to accept a publish from Budgets before a
  # notification is created against it, and the key must be able to encrypt it.
  depends_on = [aws_sns_topic_policy.alarms]
}
