/**
 * ReadyPick PILOT, in ap-south-2.
 *
 * The first environment of the Lambda-plus-on-demand-ECS architecture, and the
 * first one this repository has ever actually applied. Staging and production
 * next door were moved to the same shape in the same change, but neither has
 * ever been applied and production is DERIVED from staging by
 * `derive-production.py`, so this is the composition that has met a real
 * account. Copy this one.
 *
 * WHAT IS DIFFERENT FROM THE ENVIRONMENTS BESIDE IT
 * --------------------------------------------------
 * There is no `worker` service and no `beat` service. The work they did is now:
 *
 *   short work        `readypick-task-worker`, a Lambda. Delivery, resume
 *                     parsing, the reconciliation sweeps. Billed per
 *                     invocation, so an idle platform costs nothing for it.
 *   long work         `readypick-agent`, an ECS task definition with NO
 *                     SERVICE. A Lambda calls RunTask when work arrives; the
 *                     container does one piece of work and exits. Fargate does
 *                     not scale to zero, which is exactly why this is a task
 *                     definition and not a service.
 *   the schedule      EventBridge Scheduler rules, one per sweep, invoking the
 *                     task worker with the payload a dispatch would have sent.
 *                     No singleton process to lose.
 *
 * WHY THIS IS NOT A NEW `terraform/` TREE
 * ----------------------------------------
 * The infrastructure brief asks for `terraform/` at the repository root. This
 * repository already had a complete Terraform tree under `infra/`, with eleven
 * modules, an offline planning profile, a wildcard-IAM checker and CI wiring,
 * all of which this environment reuses. A second tree would be two answers to
 * "where is the infrastructure", and the older one is the one CI reads.
 * Recorded in DEPLOYMENT_LOG.md.
 *
 * WHAT AN APPLY OF THIS FILE DOES NOT GIVE YOU
 * ---------------------------------------------
 * Secrets are created EMPTY. Every model credential, the JWT signing key and
 * the Hugging Face token are containers with no value until a human runs
 * `aws secretsmanager put-secret-value`. A service started against an empty
 * secret starts and fails on first use, so the deploy order in
 * DEPLOYMENT_LOG.md puts the secrets before the services deliberately.
 */

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }

  # The backend lives in `backend.tf`, on its own, and that separation is
  # load-bearing rather than tidy: `terraform plan` refuses to run against an
  # uninitialised backend, and the offline planning profile has no credentials
  # and no network to initialise an S3 one with. `infra/plan-offline.sh` copies
  # this directory WITHOUT that one file, which is how this environment can have
  # both real remote state and a plan CI can run with no AWS account at all.
}

provider "aws" {
  region = var.region

  # See staging's file for what these four are. FALSE for a real apply: with
  # them skipped a misconfigured profile fails later and far less clearly.
  skip_credentials_validation = var.planning_profile
  skip_requesting_account_id  = var.planning_profile
  skip_region_validation      = var.planning_profile
  skip_metadata_api_check     = var.planning_profile

  default_tags {
    tags = local.tags
  }
}

# A SECOND REGION, FOR ONE SERVICE. Amazon Transcribe has no endpoint in
# ap-south-2 at all -- not "unavailable to this account", the DNS name does not
# resolve -- so a deployment here must call it in ap-south-1. A Transcribe job
# is region-local over S3, reading and writing a bucket in its own region, so
# the small working bucket below comes with it. Nothing else in this
# environment uses this alias, and nothing else should.
provider "aws" {
  alias  = "transcribe"
  region = var.transcribe_region

  skip_credentials_validation = var.planning_profile
  skip_requesting_account_id  = var.planning_profile
  skip_region_validation      = var.planning_profile
  skip_metadata_api_check     = var.planning_profile

  default_tags {
    tags = local.tags
  }
}

locals {
  environment = "pilot"

  # SEC-24, decided 2026-09-18 (owner delegated the call). The application's
  # ENVIRONMENT is "production" even though this composition is named pilot,
  # because readypick.ai serves real users from here and the five
  # `is_production` guards exist for exactly this deployment: the `record`
  # dispatch backend is refused, a missing Voyage key RAISES instead of
  # returning pseudo-random vectors, the seed scripts refuse the live
  # database, logs are structured, and the debug instrumentation cannot be
  # enabled. `local.environment` stays "pilot" because it names RESOURCES,
  # and renaming every resource is a migration, not a setting. Verified
  # before the flip: JWT_SECRET in Secrets Manager is a real 64-character
  # value, so the production boot refusal passes.
  app_environment = "production"

  # The role that OWNS every database object. Not the RDS master: the master's
  # password is rotated by Secrets Manager on a seven-day schedule, and on
  # 2026-09-11 that rotation took the whole product down because `DATABASE_URL`
  # held a copy of it. Ownership was moved here so the master is needed exactly
  # once, and the application's own credential is one ReadyPick rotates.
  #
  # A LITERAL, and it has to be: Terraform does not create this role. SQL does
  # (`app.scripts.provision_app_db_role`, through
  # `scripts/rotate-app-db-credential.sh`), for the same reason `CREATE
  # EXTENSION vector` lives in migration 0001 rather than in the rds module --
  # a Postgres provider holding the master credential in state is worse than
  # the duplication of a name.
  db_owner_role = "readypick_owner"

  # The frontend's tag, falling back to the shared one. See the variable's own
  # description for why the two can differ and what an apply that ignores it
  # does to the running service.
  frontend_image_tag = var.frontend_image_tag != "" ? var.frontend_image_tag : var.image_tag
  # Same fallback, same reason. See the variable's description for why the
  # analysis image so often lags the other two.
  analysis_image_tag = var.analysis_image_tag != "" ? var.analysis_image_tag : var.image_tag

  # The internal service namespace, owned here and nowhere else. The analysis
  # service sits behind no load balancer, so a Cloud Map name is the only way
  # anything addresses it. Both sides need the same string, so it is built once.
  internal_namespace   = "${var.project}.local"
  analysis_service_url = "http://analysis.${local.internal_namespace}:8100"

  # WHETHER THIS ENVIRONMENT HAS A PUBLIC ENTRY POINT AT ALL.
  #
  # Derived from a VARIABLE, so it is known at plan time and the `count`s below
  # plan cleanly. That is the difference between this and a count keyed on an
  # ARN, which does not exist until apply and cannot be planned.
  has_domain = var.domain_name != null && var.domain_name != ""

  # WHETHER ANYTHING CAN REACH THIS ENVIRONMENT AT ALL.
  #
  # Not the same question as `has_domain`. The load balancer needs a
  # CERTIFICATE; ACM needs a domain to ISSUE one, but it will hold one you
  # IMPORT for any name. A pilot with no domain is therefore reachable over
  # https on the load balancer's own AWS hostname, behind a self-signed
  # certificate whose subject alternative name matches it. One warning, about
  # the issuer, which a visitor clicks through.
  #
  # There is deliberately no plaintext alternative: the application sets Secure
  # cookies and uvicorn runs with `--proxy-headers`, so over plain http every
  # auth cookie is refused and an http-only environment is one nobody can sign
  # in to. A visible warning beats a silent login failure.
  has_public_entry = local.has_domain || (
    var.fallback_certificate_arn != null && var.fallback_certificate_arn != ""
  )

  # The origin the product believes it is served on. `jobs.public_job_url`
  # builds the candidate-facing application link from it and `app/main.py` keys
  # its CORS allowlist on it.
  #
  # With a domain it is the domain. Without one but with a certificate it is
  # the load balancer's own name, which is a real, working address. With
  # neither there is no public origin at all, and this says so in the one way
  # that cannot be mistaken for a working address: `.invalid` is reserved by
  # RFC 2606 precisely so it never resolves, so a job link built from it is
  # obviously broken rather than subtly wrong.
  frontend_url = (
    local.has_domain ? "https://${var.domain_name}" :
    local.has_public_entry ? "https://${module.alb[0].dns_name}" :
    "https://${var.project}.invalid"
  )

  # ── Receiving a reply ──────────────────────────────────────────────────────
  #
  # A SUBDOMAIN of the product's own zone, because receiving mail means owning
  # the MX record and the apex's MX belongs to whatever mailbox the company
  # actually reads. Empty without a domain, and that is a real state: the
  # deployment still SENDS verification requests, and the employer's reply
  # arrives in the sending mailbox instead of in the thread. The application
  # records that rather than hiding it (`conversations.reply_address`).
  reply_domain = local.has_domain ? "reply.${var.domain_name}" : ""

  # SES EMAIL RECEIVING DOES NOT EXIST IN EVERY REGION SES SENDS FROM, AND THIS
  # PILOT IS IN ONE OF THE GAPS. `ap-south-2` sends perfectly well;
  # `describe-active-receipt-rule-set` answers `InvalidAction` there because the
  # API is absent, and `inbound-smtp.ap-south-2.amazonaws.com` does not resolve
  # at all. Applying the module here would publish an MX record pointing at a
  # hostname with no address: every employer's reply would bounce at their own
  # mail server, and nothing in this account would log it.
  #
  # So inbound stays OFF here until the receiving half is moved to a region
  # that has it (`ap-south-1` is verified and holds no active rule set), which
  # needs a provider alias and a second `lambda` instance in that region. The
  # module refuses the wrong region by validation, so this cannot be turned on
  # by editing one boolean.
  #
  # THE CONSEQUENCE IS STATED RATHER THAN HIDDEN. With no inbound domain the
  # product sets no Reply-To, `conversations.reply_address` records that once,
  # and an employer's reply arrives in the sending mailbox instead of in the
  # thread. That is a real, working, degraded mode. Advertising a Reply-To that
  # nothing receives would be strictly worse.
  # The inbound-mail parser, merged into `module.lambda.functions` only when
  # `has_inbound` says mail can arrive. See that local for why it is false here.
  inbound_email_function = {
    # The inbound-mail parser. A SECOND ZIP FUNCTION, and the reason is the
    # same one the trigger gives: it sits on the open internet's side of the
    # product, because anything that can send mail to the reply domain reaches
    # it. Standard library and boto3 only, so what a stranger can reach is one
    # file a reviewer reads in full. It holds no database credential and no
    # model key; the one thing it can do is POST to a webhook that authorises
    # itself on a token the message already carried.
    "inbound-email" = {
      package     = "zip"
      description = "Parses one received message and posts it to the inbound-email webhook. Reads one S3 object and nothing else."
      source_dir  = "${path.root}/../../../lambda/inbound_email"
      handler     = "handler.handler"
      memory_mb   = 256
      # A large attachment is fetched whole before it is parsed. Thirty seconds
      # is generous for that and short enough that a hung webhook is a failed
      # invocation rather than a held one.
      timeout_seconds = 30
      # OUTSIDE THE VPC. It talks to S3 and to the product's own public
      # endpoint, so putting it in a private subnet would buy nothing and cost
      # a NAT hop for every reply.
      in_vpc = false
      # No secret_policy_key: it reads no secret, which is why it has no entry
      # in the secrets module's map at all.
      s3_read_object_arns = [local.inbound_mail_objects]
      environment = {
        WEBHOOK_URL = "${local.frontend_url}/api/v1/verification/inbound-email"
      }
    }
  }

  has_inbound = (
    local.reply_domain != "" && contains(var.ses_receiving_regions, var.region)
  )

  # Composed from the BUCKET NAME rather than read from the module's output,
  # because `ses_inbound` subscribes the function and therefore depends on it;
  # taking the ARN from that module would be a cycle. The name is deterministic
  # and both sides are given the same string.
  inbound_mail_objects = "arn:aws:s3:::${var.project}-${local.environment}-inbound-mail/inbound/*"

  # WITHOUT INGRESS, ONE TASK PER SERVICE.
  #
  # Two once traffic can arrive, one while it cannot. A second task buys
  # redundancy for requests that have no way in, and one is enough to prove
  # what an unreachable stage is for: that the image boots, resolves its
  # secrets, and reaches RDS and Redis from a private subnet.
  # PILOT RUNS LEAN, EXPLICITLY (2026-09-11 cost decision). One task per
  # service at rest, because this environment holds three demo tenants and
  # zero candidates and was paying for warm redundancy nobody consumes: the
  # analysis service alone (2 vCPU / 8 GB each) idled at two tasks for a
  # proctoring feature no assessment has ever exercised here. Autoscaling
  # CEILINGS are unchanged or higher, so behaviour under real load is
  # preserved: target tracking (CPU 65) grows each service toward its
  # max_count and shrinks it back, and rolling deploys still start the new
  # task before draining the old, so a deploy is not an outage. What IS
  # accepted is that an AZ failure briefly downs the demo site; production
  # keeps its own sizing and this block does not touch it.
  service_count = 1

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
# ONE CUSTOMER-MANAGED KEY, used by S3, RDS, ElastiCache, Secrets Manager, SNS
# and the log groups. One key rather than six because the question it answers,
# "who can decrypt this environment's data", has one answer, and six keys would
# be six key policies to keep in step.
#
# The policy is written out rather than defaulted, for the reason staging's file
# sets out at length: an omitted policy makes a customer-managed key behave like
# an AWS-managed one, which is the thing choosing customer-managed was avoiding.

data "aws_iam_policy_document" "kms" {
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
        "sns.amazonaws.com",
        # SES ENCRYPTS THE EVENT PAYLOAD ITSELF before handing it to SNS, so
        # publishing to an encrypted topic needs the SES principal on the KEY,
        # not just on the topic. Without it CreateConfigurationSetEventDestination
        # fails outright: "Access denied to KMS key for SNS topic".
        #
        # This is also why the topic uses this environment's own CMK rather
        # than `alias/aws/sns`: an AWS-managed key has a fixed policy that
        # cannot be granted to SES at all, so encryption would have had to be
        # dropped to make delivery tracking work.
        "ses.amazonaws.com",
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

    # Scoped to this account. A service principal with no account condition is
    # the confused-deputy shape: it reads as narrow because it names an AWS
    # service, and it is reachable from any account using that service.
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

# ── Where an alarm goes ──────────────────────────────────────────────────────
#
# The topic lives HERE rather than inside `observability`, for the same reason
# the KMS key does: two modules use it. The `lambda` module publishes a
# permanently failed asynchronous invocation to it, and `observability` alarms
# to it while depending on that module's function names. Owning it in either
# one would be a dependency cycle.

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

resource "aws_sns_topic" "alarms" {
  name              = "${var.project}-${local.environment}-alarms"
  kms_master_key_id = aws_kms_key.this.arn
  tags              = local.tags
}

resource "aws_sns_topic_policy" "alarms" {
  arn    = aws_sns_topic.alarms.arn
  policy = data.aws_iam_policy_document.alarm_topic.json
}

# AN EMAIL SUBSCRIPTION IS PENDING UNTIL ITS RECIPIENT CLICKS THE CONFIRMATION
# LINK, and Terraform reports it as created either way. A plan that says
# "1 to add" is not evidence that anybody is being notified, which is exactly
# the kind of thing this codebase has been burned by before. Confirm it in the
# inbox, then check the subscription in the console.
resource "aws_sns_topic_subscription" "alarm_email" {
  for_each = toset(var.alarm_emails)

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

# ── Network ──────────────────────────────────────────────────────────────────

module "network" {
  source = "../../modules/network"

  project            = var.project
  environment        = local.environment
  region             = var.region
  cidr_block         = "10.0.0.0/16"
  availability_zones = var.availability_zones

  # Flow logs: the only record of what actually reached what. Every other
  # control in the network module is preventive and leaves no trace of having
  # refused anything.
  kms_key_arn             = aws_kms_key.this.arn
  flow_log_retention_days = 30

  # ONE NAT, a locked decision. A per-AZ pair buys resilience this pilot is not
  # buying yet, at roughly $32 a month each.
  single_nat_gateway = true

  # The pilot already has a managed NAT gateway for the model provider, SES
  # and other public APIs.  Paying for four interface endpoint ENIs as well is
  # duplicate egress infrastructure while Judge0 is disabled.  Keep the
  # endpoints only for the isolated Judge0 host, which deliberately has no
  # internet route and therefore cannot fall back to NAT.
  interface_endpoints = var.judge0_enabled ? [
    "ecr.api",
    "ecr.dkr",
    "secretsmanager",
    "logs",
  ] : []

  # The code sandbox host reaches ECR, Secrets Manager and Logs through the
  # existing interface endpoints. Empty while `judge0_enabled` is false, which
  # leaves the endpoints group exactly as it is.
  endpoint_client_security_group_ids = var.judge0_enabled ? [module.code_sandbox[0].host_security_group_id] : []

  tags = local.tags
}

# ── Registries ───────────────────────────────────────────────────────────────

module "ecr" {
  source = "../../modules/ecr"

  project     = var.project
  environment = local.environment
  kms_key_arn = aws_kms_key.this.arn

  # RETAINED BY COUNT, NEVER BY AGE. An age rule deletes the image a
  # long-running service needs in order to restart.
  keep_images = 20

  tags = local.tags
}

# ── Secrets ──────────────────────────────────────────────────────────────────
#
# Containers only. See the file docstring: every value is a human's to put in.

module "secrets" {
  source = "../../modules/secrets"

  project     = var.project
  environment = local.environment
  region      = var.region
  kms_key_id  = aws_kms_key.this.key_id
  kms_key_arn = aws_kms_key.this.arn

  tags = local.tags
}

# ── The native arm64 image builder ───────────────────────────────────────────
#
# A deploy used to build all three arm64 images under QEMU on an x86 laptop,
# which cost about two hours. This is a CodeBuild project on a native arm64
# host that `scripts/build-images-remote.sh` starts with one commit's source
# archive; docs/operations/DEPLOY_AWS.md makes it the default build path and
# keeps the local QEMU build as the fallback.
#
# PURELY ADDITIVE: a bucket, a log group, a role and a project, none of which
# any existing resource references, which is why the switch defaults to true.
# The bucket name defaults to one carrying the account id, so it is this
# account's in practice; if somebody else ever holds it the apply fails on
# BucketAlreadyExists, loudly, and `image_builder_bucket_name` names another.

module "image_builder" {
  source = "../../modules/image_builder"
  count  = var.image_builder_enabled ? 1 : 0

  project     = var.project
  environment = local.environment
  region      = var.region
  account_id  = var.account_id

  repository_arns = module.ecr.repository_arns
  repository_urls = module.ecr.repository_urls

  bucket_name                  = var.image_builder_bucket_name != "" ? var.image_builder_bucket_name : "${var.project}-${local.environment}-image-builds-${var.account_id}"
  huggingface_token_secret_arn = module.secrets.secret_arns["HUGGINGFACE_TOKEN"]
  kms_key_arn                  = aws_kms_key.this.arn

  tags = local.tags
}

# ── Data ─────────────────────────────────────────────────────────────────────

module "rds" {
  source = "../../modules/rds"

  project           = var.project
  environment       = local.environment
  subnet_ids        = module.network.data_subnet_ids
  security_group_id = module.network.rds_security_group_id

  # t4g.medium, up from t4g.micro (2026-09-10). Two reasons, and neither is
  # raw connection count. 1GB of RAM is not comfortable working memory for
  # pgvector HNSW index operations once real candidates land, and RDS derives
  # its default max_connections from instance memory, so the bump also lifts
  # the connection ceiling roughly fourfold against Lambda concurrency spikes
  # (worker_session builds a fresh engine per invocation by design, so Lambda
  # concurrency, not the app pool size, is what actually drives peak
  # connections here).
  #
  # AN RDS PROXY WAS EVALUATED AND DELIBERATELY NOT BUILT (owner decision,
  # 2026-09-10). The AWS pinning documentation settles it: for PostgreSQL the
  # proxy pins a session on any SET command, on set_config(), and on named
  # prepared statements. This application issues SET LOCAL ROLE inside every
  # tenant-scoped transaction (core/db.tenant_scope), a session-level
  # set_config on every worker connection (workers/runtime.worker_session),
  # and asyncpg caches prepared statements by default, so effectively every
  # session would pin immediately: no multiplexing, a held backend connection
  # per client for its whole lifetime, and the one benefit left is queuing
  # connection storms. Revisit only if CloudWatch DatabaseConnections ever
  # approaches the instance ceiling; the fix that makes a proxy worthwhile is
  # an application change (transaction-scoped worker bypass, statement cache
  # off), not a Terraform change.
  #
  # `db_pool_size=12, db_max_overflow=3` in the app were sized against the
  # micro instance's ceiling and are deliberately untouched by this bump: they
  # bind the long-lived ECS api service, which was never the pressure point.
  # Raise them only against a measured need.
  instance_class = "db.t4g.medium"
  # 50 GB growing to 200 (raised from 100 on 2026-09-10). Chunk embeddings are
  # 1024 floats each with an HNSW index on top, many chunks per document, and
  # they grow faster than relational rows; the ceiling gets room before this
  # needs revisiting. The STARTING allocation stays 50: gp3's baseline IOPS is
  # a function of size, so the floor is a performance floor as well as a
  # capacity one, and pre-paying for unused space buys neither.
  allocated_storage     = 50
  max_allocated_storage = 200
  # MUST FLIP TO true BEFORE ANY REAL (NON-DEMO) TENANT'S DATA LIVES HERE.
  # Today this environment holds three demo tenants and zero candidates, and
  # Multi-AZ roughly doubles the RDS bill for redundancy protecting data that
  # does not yet exist. The day a real customer is onboarded to pilot, this
  # line is part of that onboarding. Production already runs multi_az = true.
  multi_az              = false
  backup_retention_days = 7

  # BOTH GUARDS STATED RATHER THAN INHERITED (2026-09-17). They default to the
  # safe value in the module now, and stating them here is what makes turning
  # one off a visible line in a diff rather than a default quietly changing.
  #
  # This environment is the only one that has ever been applied. It holds
  # three demo tenants and a month of setup that exists nowhere else.
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
  # A single node. Redis is no longer the message broker, so a failure here is
  # not lost work: it is a rate limiter that fails open, a cache that misses,
  # and a proctoring warning counter that answers 503 rather than silently not
  # warning. The health check probes it, so a task that loses Redis leaves the
  # target group instead of serving assessments it cannot monitor.
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

  noncurrent_retain_days = 30

  tags = local.tags
}

# The account id, for the ARNs in this environment that have to be written out
# by hand: a Transcribe job's ARN is not an attribute of any resource here,
# because the jobs are created by the application at run time.
#
# `var.account_id` AND NOT `data "aws_caller_identity"`. The data source calls
# STS, which the OFFLINE PLAN cannot do: it runs against account 000000000000
# in a region that does not exist and has never contacted AWS, so the lookup
# fails DNS resolution and takes the whole plan with it. Pilot was the one
# environment that could not be planned offline for exactly this reason, and a
# pre-apply check that cannot run is a check nobody reads. The variable is the
# same account and is already required.

# ── Speech to text, in the one region that has it ────────────────────────────
#
# A WORKING BUCKET, not a store. The transcription step copies a spoken
# answer's audio in from `voice-answers/`, runs the job, copies the transcript
# back to the product's own bucket and deletes both working objects. The
# expiry rule below is the backstop for a delete that did not happen, not the
# mechanism.
#
# SSE-S3 rather than the environment's KMS key, and that is forced rather than
# chosen: the key is regional and lives in `var.region`, so an object encrypted
# with it cannot be written here.

resource "aws_s3_bucket" "transcribe" {
  count    = var.transcribe_enabled ? 1 : 0
  provider = aws.transcribe

  # NAMED, NOT DERIVED, the same rule the storage bucket follows: S3 names are
  # global across every AWS account, so a derived name is one that may already
  # belong to somebody else.
  bucket = var.transcribe_bucket_name
}

resource "aws_s3_bucket_public_access_block" "transcribe" {
  count    = var.transcribe_enabled ? 1 : 0
  provider = aws.transcribe

  bucket                  = aws_s3_bucket.transcribe[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "transcribe" {
  count    = var.transcribe_enabled ? 1 : 0
  provider = aws.transcribe

  bucket = aws_s3_bucket.transcribe[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "transcribe" {
  count    = var.transcribe_enabled ? 1 : 0
  provider = aws.transcribe

  bucket = aws_s3_bucket.transcribe[0].id

  rule {
    id     = "expire-working-objects"
    status = "Enabled"
    filter {}
    # ONE DAY. The pipeline deletes its own objects; anything still here
    # outlived a job that failed between the copy in and the delete, and a
    # candidate's assessment audio must not sit in a second region waiting for
    # somebody to notice.
    expiration {
      days = 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# The task worker is the only caller: a spoken answer is transcribed by a
# Route.LAMBDA task. This grant sat on the ECS agent role while the deleted
# video-interview mode transcribed whole recordings there. Scoped to this
# product's own job names and to the working bucket, never `*`.
data "aws_iam_policy_document" "transcribe" {
  count = var.transcribe_enabled ? 1 : 0

  statement {
    sid = "RunTranscriptionJobs"
    actions = [
      "transcribe:StartTranscriptionJob",
      "transcribe:GetTranscriptionJob",
    ]
    resources = [
      "arn:aws:transcribe:${var.transcribe_region}:${var.account_id}:transcription-job/readypick-*",
    ]
  }

  statement {
    sid = "WorkingObjects"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = ["${aws_s3_bucket.transcribe[0].arn}/*"]
  }
}

resource "aws_iam_role_policy" "task_worker_transcribe" {
  count = var.transcribe_enabled ? 1 : 0

  name   = "${var.project}-${local.environment}-transcribe"
  role   = element(split("/", module.lambda.execution_role_arns["task-worker"]), 1)
  policy = data.aws_iam_policy_document.transcribe[0].json
}

# ── Outbound mail ────────────────────────────────────────────────────────────
#
# `ses:SendRawEmail` and nothing else. The transport builds the MIME message
# itself (attachments, Reply-To), so the simple send action would not serve it,
# and no read, no identity management and no configuration-set write is reach
# the delivery path needs.
#
# The task worker is where mail actually leaves the platform, because delivery
# is Route.LAMBDA. The API holds the same grant for the one send that is not
# dispatched: the corporate-sender ownership code, which a person is waiting on
# in the browser.
data "aws_iam_policy_document" "ses_send" {
  # Scoped to the "identity" resource type SES defines for both of these
  # actions (a verified email address or domain, account- and region-bound),
  # rather than the account-wide "*": AWS's own IAM reference lists
  # `SendRawEmail` and `GetIdentityVerificationAttributes` among the actions
  # that support it. The sending domain is not a fixed literal -- a corporate
  # sender's own domain is verified dynamically -- so the identity NAME stays
  # wildcarded while the partition, service, region and account do not.
  statement {
    sid       = "SendRawEmail"
    actions   = ["ses:SendRawEmail"]
    resources = ["arn:aws:ses:${var.region}:${var.account_id}:identity/*"]
  }

  statement {
    sid       = "ReadSendingIdentityStatus"
    actions   = ["ses:GetIdentityVerificationAttributes"]
    resources = ["arn:aws:ses:${var.region}:${var.account_id}:identity/*"]
  }

  # `ListIdentities` enumerates every identity on the ACCOUNT and genuinely
  # has no resource type in SES's API -- unlike its two neighbours above, an
  # ARN here would not narrow the grant, it would make the call fail. See
  # `check-no-wildcard-iam.py`'s `RESOURCELESS_ACTIONS`.
  statement {
    sid       = "ListSendingIdentities"
    actions   = ["ses:ListIdentities"]
    resources = ["*"]
  }
}

# ── SES delivery events: one configuration set, one topic, one subscription ──
#
# SHARED, NOT PER TENANT. A topic per company multiplies an AWS resource by a
# number the product grows without bound, and every one would carry the same
# policy and the same single subscriber. Correlation is DATA instead: each send
# is tagged with the tenant, sender and candidate it belongs to, and the webhook
# resolves the row from the SES message id it already stores.
#
# Standard, never FIFO: SES refuses a FIFO topic as an event destination.

resource "aws_sns_topic" "ses_events" {
  name = "${var.project}-${local.environment}-ses-events"
  # THIS ENVIRONMENT'S OWN CMK, not `alias/aws/sns`. An AWS-managed key has a
  # fixed policy that cannot be granted to the SES service principal, and SES
  # encrypts the event payload itself before publishing -- so the managed key
  # makes CreateConfigurationSetEventDestination fail outright. The choice is
  # the CMK or no encryption at all, and a bounce event names the candidate's
  # address, so it is the CMK. The matching grant is in the key policy above.
  kms_master_key_id = aws_kms_key.this.arn
}

# SES publishes as a SERVICE PRINCIPAL, so the grant lives on the topic rather
# than on any role this platform holds. Conditioned on our own account, or SES
# in any other account could publish events here and move a delivery status.
data "aws_iam_policy_document" "ses_events_topic" {
  statement {
    sid       = "AllowSESPublish"
    effect    = "Allow"
    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.ses_events.arn]

    principals {
      type        = "Service"
      identifiers = ["ses.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "ses_events" {
  arn    = aws_sns_topic.ses_events.arn
  policy = data.aws_iam_policy_document.ses_events_topic.json
}

resource "aws_sesv2_configuration_set" "this" {
  configuration_set_name = "${var.project}-${local.environment}"

  delivery_options {
    # TLS WHERE THE RECEIVER OFFERS IT. REQUIRE bounces mail to any receiving
    # domain without STARTTLS, which is a deliverability decision this product
    # has no business making on a candidate's behalf.
    tls_policy = "OPTIONAL"
  }

  reputation_options {
    reputation_metrics_enabled = true
  }

  sending_options {
    sending_enabled = true
  }
}

resource "aws_sesv2_configuration_set_event_destination" "sns" {
  configuration_set_name = aws_sesv2_configuration_set.this.configuration_set_name
  event_destination_name = "${var.project}-${local.environment}-sns"

  event_destination {
    enabled = true

    sns_destination {
      topic_arn = aws_sns_topic.ses_events.arn
    }

    # NO OPEN AND NO CLICK. Both require SES to rewrite the message -- a
    # tracking pixel and wrapped links -- inside mail telling a candidate about
    # their own application. The delivery OUTCOME is all this product needs.
    matching_event_types = [
      "SEND",
      "DELIVERY",
      "BOUNCE",
      "COMPLAINT",
      "REJECT",
      "RENDERING_FAILURE",
      "DELIVERY_DELAY",
    ]
  }
}

# The webhook verifies the SNS signature and pins the topic ARN, so an https
# subscription is safe to declare. SNS posts a SubscriptionConfirmation first
# and the endpoint confirms it only after that signature check passes, which is
# why the API has to know the topic ARN BEFORE this is created. That ordering
# is the reason the topic and the task definition's environment ship together.
resource "aws_sns_topic_subscription" "ses_events_webhook" {
  count = local.has_public_entry ? 1 : 0

  topic_arn = aws_sns_topic.ses_events.arn
  protocol  = "https"
  endpoint  = "${local.frontend_url}/api/v1/email-senders/events/ses"

  # RAW DELIVERY OFF. The handler reads the SNS envelope itself -- Type,
  # TopicArn, SigningCertURL, Signature -- and raw delivery strips exactly the
  # fields the signature check needs.
  raw_message_delivery = false

  # Terraform waits for the endpoint to confirm. If it cannot, that is a real
  # failure worth surfacing rather than a subscription silently left pending.
  confirmation_timeout_in_minutes = 5
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

resource "aws_iam_role_policy" "task_worker_ses" {
  name   = "${var.project}-${local.environment}-ses-send"
  role   = element(split("/", module.lambda.execution_role_arns["task-worker"]), 1)
  policy = data.aws_iam_policy_document.ses_send.json
}

# ── The API's right to start work ────────────────────────────────────────────
#
# THE API COULD NOT INVOKE A SINGLE LAMBDA, and that was not a research bug: it
# broke every dispatched background task in the product. `TASK_DISPATCH_BACKEND`
# is `aws`, so `dispatch()` invokes `readypick-task-worker`, and `dispatch` RAISES
# on failure by design. Resume parsing, email delivery, matching runs and the
# reconciliation sweeps all start with that call. Both synchronous agents failed
# the same way, which is how it was found: Company Research answered 503 with
# `AccessDeniedException ... not authorized to perform: lambda:InvokeFunction`,
# and JD generation had the identical gap.
#
# It survived because nothing in the deploy asks this question. The functions
# exist, the task definitions carry the right environment, every service reports
# healthy, and the failure only appears when a human clicks something that
# dispatches. `terraform plan` cannot see a MISSING grant.
#
# ENUMERATED BY NAME, never `lambda:*` and never a prefix, which is the same
# rule `service_secrets` follows one module over. A revision qualifier is
# deliberately absent: these are invoked by function NAME, so an alias-less ARN
# is the exact grant rather than a wildcard standing in for one.
data "aws_iam_policy_document" "api_invoke_agents" {
  statement {
    sid     = "InvokeTheFunctionsTheApiActuallyCalls"
    actions = ["lambda:InvokeFunction"]
    resources = [
      # workers/dispatch.py: WORKER_FUNCTION and TRIGGER_FUNCTION.
      "arn:aws:lambda:${var.region}:${var.account_id}:function:${var.project}-task-worker",
      "arn:aws:lambda:${var.region}:${var.account_id}:function:${var.project}-assessment-trigger",
      # workers/agent_client.py: the two agents a recruiter waits on.
      "arn:aws:lambda:${var.region}:${var.account_id}:function:${var.project}-jd-gen",
      "arn:aws:lambda:${var.region}:${var.account_id}:function:${var.project}-company-profile",
    ]
  }
}

resource "aws_iam_role_policy" "api_invoke_agents" {
  name   = "${var.project}-${local.environment}-invoke-agents"
  role   = element(split("/", module.ecs.task_role_arns["api"]), 1)
  policy = data.aws_iam_policy_document.api_invoke_agents.json
}

resource "aws_iam_role_policy" "api_ses" {
  name   = "${var.project}-${local.environment}-ses-send"
  role   = element(split("/", module.ecs.task_role_arns["api"]), 1)
  policy = data.aws_iam_policy_document.ses_send.json
}

# ── Traffic ──────────────────────────────────────────────────────────────────
#
# The certificate, the DNS record and the WAF are CONDITIONAL on a domain being
# configured, and the load balancer is not: without a domain the environment
# still gets a load balancer at its own AWS name, which is what the deployment
# brief asks for.
#
# There is no plaintext-only mode, and that is deliberate rather than an
# omission. The application sets Secure cookies and the container runs uvicorn
# with `--proxy-headers`, so over plain http every auth cookie is refused and
# nobody can sign in. An http-only environment would not be a smaller product;
# it would be a product with no login.

module "acm" {
  source = "../../modules/acm"
  count  = local.has_domain ? 1 : 0

  project     = var.project
  environment = local.environment

  domain_name    = var.domain_name
  hosted_zone_id = var.hosted_zone_id
  # The bare apex is what the frontend, cookies and CORS allowlist all key on
  # (`local.frontend_url` above). `www` is added here purely so a visitor who
  # types it does not hit a certificate name mismatch; the DNS module below
  # aliases it to the same load balancer rather than standing up a redirect.
  subject_alternative_names = ["www.${var.domain_name}"]

  tags = local.tags
}

module "alb" {
  source = "../../modules/alb"
  count  = local.has_public_entry ? 1 : 0

  project     = var.project
  environment = local.environment
  account_id  = var.account_id

  vpc_id            = module.network.vpc_id
  public_subnet_ids = module.network.public_subnet_ids
  security_group_id = module.network.alb_security_group_id

  # From the VALIDATION resource, not from the certificate itself: a listener
  # created against a PENDING_VALIDATION certificate is accepted by AWS and
  # then fails the TLS handshake for every visitor.
  # From the ACM VALIDATION resource where there is a domain, so a listener is
  # never created against a PENDING_VALIDATION certificate: AWS accepts that
  # and then fails the TLS handshake for every visitor. Otherwise the imported
  # stopgap, which is ISSUED the moment it is imported.
  certificate_arn = local.has_domain ? module.acm[0].certificate_arn : var.fallback_certificate_arn

  access_logs_bucket_name = var.access_logs_bucket_name

  # Off for a pilot, which is meant to be destroyable.
  enable_deletion_protection = false

  target_groups = {
    api = {
      # LIVENESS, NOT READINESS, AND THE CHANGE IS DELIBERATE.
      #
      # This was `/health`, which is DEEP: it resolves a pooled database
      # session and pings Redis. The reasoning for that was sound as a DEPLOY
      # gate, and wrong as an ongoing rotation check, because the two questions
      # have opposite failure preferences.
      #
      # Redis is a single shared ElastiCache. An outage there did not make one
      # task unhealthy, it made every task unhealthy at once: the target group
      # emptied and the ALB answered 503 for the whole product, including jobs,
      # candidates, billing and reports, all of which would otherwise have kept
      # working. ECS then replaced the tasks and the replacements failed the
      # same check, which is a restart loop that also discards every warm
      # connection pool. There was nothing to route around TO.
      #
      # The deploy gate is not lost. `scripts/smoke-test.sh` runs after the
      # rollout and its AUTHENTICATED probes (/api/v1/auth/me, /api/v1/jobs)
      # require the database and Redis to answer, so a release with a broken
      # dependency still fails. That gate is now real in a way the old
      # `/health` probe in that script was not: it followed redirects, so it
      # had been passing by loading the login page.
      port                    = 8000
      health_path             = "/health/live"
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

  # THE ENUMERATED UNAUTHENTICATED SURFACE (RBAC sections 15 and 33). These are
  # the paths behind the public job link a candidate reaches from a forwarded
  # email. Adding to this list widens the unauthenticated surface of the
  # product, and `backend/tests/test_deploy_secret_hygiene.py` reads it back out
  # of the environment files so an addition fails a test.
  public_path_patterns = [
    "/api/v1/jobs/public/*",
    "/api/v2/jobs/public/*",
  ]

  routes = {
    api = {
      priority     = 100
      target_group = "api"
      # `/docs` IS NOT HERE, AND THAT IS THE FIX RATHER THAN AN OMISSION.
      #
      # This rule used to send `/docs` to the API so FastAPI's Swagger UI would
      # answer there. Two things were wrong with that and only the second was
      # visible. The first: the frontend has a PUBLIC DOCUMENTATION PAGE at
      # `app/(public)/docs/page.tsx`, and the site header and footer link to
      # `/docs` from every public page, so that page was shadowed by Swagger and
      # nobody could reach it. The second: once the interactive docs were closed
      # on any TLS deployment, the same rule started answering a linked nav item
      # with a 404, because the route it forwarded to no longer exists.
      #
      # `/openapi.json` STAYS. `scripts/smoke-test.sh` probes it unauthenticated
      # after every deploy and fails the deploy if it is not 200, and the
      # frontend has no page at that path to shadow.
      # `/health/live` is routed to the API so the post-deploy smoke test can
      # actually reach it. `/health` is deliberately NOT: it is the deep
      # readiness probe and reports whether the database and cache are up,
      # which is not a fact this product owes the internet. The target group
      # reaches it directly regardless of listener rules.
      path_patterns = ["/api/*", "/openapi.json", "/health/live"]
    }
  }

  tags = local.tags
}

module "dns" {
  source = "../../modules/dns"
  count  = local.has_domain ? 1 : 0

  hosted_zone_id = var.hosted_zone_id
  hostnames      = [var.domain_name, "www.${var.domain_name}"]

  alb_dns_name = module.alb[0].dns_name
  # The LOAD BALANCER's zone, an AWS-owned zone, NOT `var.hosted_zone_id`.
  # Passing the product's own zone here produces a record that resolves to
  # nothing.
  alb_zone_id = module.alb[0].zone_id
}

module "waf" {
  source = "../../modules/waf"
  count  = local.has_public_entry ? 1 : 0

  project     = var.project
  environment = local.environment

  # BUILT AND OFF. `enabled = false` creates nothing at all, rather than a
  # permissive web ACL that costs money and proves nothing. Turn it on in
  # `count_only` mode first: the managed rule sets inspect request bodies, and
  # this product's request bodies are resumes, client-written job descriptions
  # and interview answers, which is a corpus no generic rule set was tuned
  # against.
  enabled    = false
  count_only = true

  alb_arn     = module.alb[0].arn
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

  vpc_id              = module.network.vpc_id
  discovery_namespace = local.internal_namespace

  secret_policy_arns = module.secrets.policy_arns

  # The WRITE half, and it lands on the TASK role rather than the

  # execution role: the execution role injects secrets before the

  # container starts, while `app.scripts.provision_app_db_role` writes

  # the rotated DSN with the application's own SDK.

  secret_writer_policy_arns = module.secrets.writer_policy_arns
  s3_policy_arn             = module.s3.access_policy_arn
  ecr_repository_arns       = values(module.ecr.repository_arns)

  kms_key_arn        = aws_kms_key.this.arn
  log_retention_days = 30

  # OFF. A shell in a container holding real candidate data is a different
  # thing from a shell in one holding seed data, and this environment is the
  # one with pilot customers in it.
  enable_execute_command = false

  common_environment = {
    # `ENVIRONMENT`, NOT `APP_ENV`. `Settings.environment` reads this name,
    # and `APP_ENV` is a Cloud Run convention from the previous platform that
    # nothing has ever read. With it set, every deployed service believed it
    # was in `development` -- the default -- which is what the pilot's first
    # agent run reported in its own log.
    ENVIRONMENT                   = local.app_environment
    AWS_REGION                    = var.region
    S3_BUCKET                     = module.s3.bucket_name
    S3_KMS_KEY_ID                 = aws_kms_key.this.arn
    EMBEDDING_DIMENSIONS          = "1024"
    RESUME_SIGNED_URL_TTL_SECONDS = "300"
    FRONTEND_URL                  = local.frontend_url
    # `aws` is the only backend that reaches a Lambda. `local` runs tasks in a
    # thread and `record` runs nothing, and neither belongs on a deployed
    # service: `record` is refused in production by the dispatcher itself.
    TASK_DISPATCH_BACKEND = "aws"
    # ONE TRANSPORT PER DEPLOYMENT, never a fallback chain: the same shape
    # TASK_DISPATCH_BACKEND has. SES sends as the tenant's own verified
    # sender, which is the whole point of the corporate-sender feature and
    # something an authenticated Gmail mailbox structurally cannot do.
    EMAIL_TRANSPORT = "ses"
    # THE CONFIGURATION SET IS WHAT MAKES DELIVERY TRACKING EXIST. SES only
    # publishes events for a message sent under a configuration set that has an
    # event destination, so a send without this name attached is a send nobody
    # ever learns the outcome of. The topic ARN is what the webhook pins an
    # incoming message against; empty there means refuse everything.
    SES_CONFIGURATION_SET = aws_sesv2_configuration_set.this.configuration_set_name
    SES_SNS_TOPIC_ARN     = aws_sns_topic.ses_events.arn
    # THE PLATFORM SECURITY SENDER. Every security code and account notice
    # leaves from this identity and never from a tenant's corporate sender
    # (`services/security_email`); with ENVIRONMENT=production a transport
    # other than SES refuses to send it. The API validates the value at boot;
    # the task worker, which sends, carries the same two values.
    PLATFORM_SECURITY_SENDER_EMAIL = var.platform_security_sender_email
    PLATFORM_SECURITY_SENDER_NAME  = "Vivekium"
    # THE DOMAIN A REPLY COMES BACK TO, and the domain the application builds a
    # thread's Reply-To on. Both halves read this ONE value, so the address a
    # verification request asks an employer to reply to and the address SES is
    # configured to receive cannot drift into each other's blind spot. Empty
    # without a domain, which is a real state rather than a broken one: the
    # deployment still sends, and `conversations.reply_address` records that the
    # reply will arrive in the sending mailbox instead of in the thread.
    INBOUND_EMAIL_DOMAIN = local.has_inbound ? local.reply_domain : ""
    # ONE RERANKER PER DEPLOYMENT, never a fallback chain: the same shape
    # TASK_DISPATCH_BACKEND and EMAIL_TRANSPORT have, validated against a
    # closed set by `reranker.configured_backend()`, which RAISES on anything
    # outside it rather than defaulting.
    #
    # PILOT RUNS THE CROSS-ENCODER AND THE OTHER ENVIRONMENTS DO NOT, which is
    # deliberate. `rerank-2.5` was proven live on 2026-09-09 (see
    # VERIFICATION_RESULTS.md) and the CODE default is `lexical`, so leaving
    # this unset anywhere means the deterministic pass runs there. Turning it
    # on changes which evidence an agent reads FIRST, and retrieval quality is
    # still unmeasured -- the golden set is 24 hand-authored cases against a
    # floor of 300 -- so the change is made where it can be watched before it
    # is made where it cannot.
    #
    # IT IS SET ON THE LAMBDAS TOO, and that uniformity is the point. Agents
    # reach retrieval through `services/tools/implementations`, which runs in
    # the API, in `task-worker` and in `agent`. Setting it on the ECS services
    # alone would give two halves of one product different rankings for the
    # same query, which is worse than either value applied everywhere.
    #
    # This is not a claim the cross-encoder will always answer. When it cannot,
    # the lexical pass runs and the record carries `reranker="lexical",
    # degraded=true` with a reason, because a degradation is RECORDED and never
    # silent. What the value guarantees is that a deployment believing it runs
    # a cross-encoder is not quietly running the placeholder, which is the
    # failure W6.1 exists to end.
    RETRIEVAL_RERANKER = "voyage"
  }

  services = {
    api = {
      image         = "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
      cpu           = 512
      memory        = 1024
      desired_count = local.service_count
      # The ceiling the OLD sizing allowed (4), kept: lowering the resting
      # count must not lower what the service can grow to under load.
      max_count   = 4
      port        = 8000
      health_path = "/health"
      # REGISTERS THE TASKS WITH THE LOAD BALANCER, when there is one. Without
      # it the service runs, is attached to no target group, and serves no
      # traffic while every dashboard reports it healthy. Null here is not that
      # mistake: there is no load balancer to be attached to yet, and the ECS
      # module's own validation refuses a target group without a port so the
      # two cannot get out of step.
      target_group_arn = local.has_public_entry ? module.alb[0].target_group_arns["api"] : null
      needs_s3         = true
      environment = merge(local.judge0_client_environment, {
        # THIS CONTAINER SIGNS (sessions, OTP hashes, signed links), so
        # in production it must refuse to boot without its key. The
        # guard is opt-in per container because secrets are enumerated
        # per service and most containers rightly hold no signing key.
        REQUIRE_JWT_SECRET              = "1"
        PROCTORING_ANALYSIS_SERVICE_URL = local.analysis_service_url
        # PUBLIC BY DESIGN, and a plain variable rather than a secret for that
        # reason: the browser receives it at runtime on the API's subscribe
        # and purchase responses.
        # Its partner, RAZORPAY_KEY_SECRET, is server-side only and is mounted
        # from Secrets Manager below. Checkout needs both.
        RAZORPAY_KEY_ID = var.razorpay_key_id
      })
      # Code sandbox stage B (all three empty until `judge0_clients_enabled`):
      # the client group that admits the API to the sandbox, and the read on
      # the token the execution role injects as JUDGE0_AUTH_TOKEN.
      extra_security_group_ids    = local.judge0_client_security_group_ids
      extra_execution_policy_arns = local.judge0_token_policy_arns
      # The backend writes no APPLICATION data to disk by design: resume bytes
      # never persist on the filesystem, and that invariant is what
      # `readonly_root` enforces.
      #
      # `/tmp` IS THE NARROW EXCEPTION, AND IT IS LOAD BEARING FOR SIGN-IN.
      # Verifying a Firebase ID token fetches Google's public signing certs,
      # and `google-auth` caches that response through `cachecontrol`, which
      # writes it to a NamedTemporaryFile. With no writable temp directory
      # that raises FileNotFoundError deep inside verification, where it is
      # indistinguishable from a bad credential: the route answered 401
      # "Invalid Firebase session" for every valid Google and password
      # sign-in on the live site, which is what sent the search to Firebase's
      # authorized-domain list instead of here. An empty ephemeral volume, so
      # the rest of the filesystem stays immutable for the life of the task.
      readonly_root  = true
      writable_paths = ["/tmp"]
      # The backend image runs as uid 10001 (`pickready`). Mounting the volume
      # is only half the fix: a Fargate task volume arrives root-owned 0755, so
      # it has to be handed to that uid before the application starts.
      writable_paths_uid = "10001"
      secrets = merge({
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
        # AI Reach calls Tavily from the request handler, so the API is the
        # process that needs this. See the IAM list in modules/secrets.
        TAVILY_API_KEY = module.secrets.secret_arns["TAVILY_API_KEY"]
        # THE INBOUND WEBHOOK'S SHARED SECRET. `POST /verification/inbound-email`
        # is a PUBLIC route that writes into verification requests, BGV threads
        # and conversations, and its only protection was that a caller had to
        # know a per-thread token -- which travels by email, so it exists in
        # every mailbox that ever received or forwarded one of these threads.
        #
        # With this absent the route STAYS OPEN and logs
        # `verification.inbound_unauthenticated` on every call, which is the
        # right behaviour for a value that might legitimately be missing and the
        # wrong state to leave an environment in. It is minted by the secrets
        # module rather than by a human for exactly that reason.
        INBOUND_WEBHOOK_SECRET = module.secrets.secret_arns["INBOUND_WEBHOOK_SECRET"]
      }, local.judge0_client_secrets)
    }

    # THE ASSESSMENT AGENT. A task definition and nothing else: no service, no
    # desired count, no autoscaling. `readypick-assessment-trigger` calls
    # RunTask against this family when an assessment, a matrix compilation or a
    # matching pass is dispatched, the container runs that one piece of work,
    # and the process exits. The task stops, and that is when the meter stops.
    #
    # This is the whole cost argument for the architecture. Fargate does not
    # scale to zero, so long AI work that runs a few times an hour must not be
    # a standing service.
    agent = {
      image = "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
      # `agent` is the entry point role in docker-entrypoint.sh: read one
      # dispatched task from the environment, run it, exit.
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
      environment = merge(local.judge0_client_environment, {
        PROCTORING_ANALYSIS_SERVICE_URL = local.analysis_service_url
      })
      # Code sandbox stage B: the token read for the injection. The client
      # GROUP for this task is on the trigger's ECS_SECURITY_GROUP_IDS below,
      # because an on-demand task has no service to carry one.
      extra_execution_policy_arns = local.judge0_token_policy_arns
      # NO FIREBASE KEY. A background task never authenticates a browser
      # session, so it has no business reading the service account.
      secrets = merge({
        DATABASE_URL      = module.secrets.secret_arns["DATABASE_URL"]
        REDIS_URL         = module.secrets.secret_arns["REDIS_URL"]
        OPENAI_GPT_TERRA  = module.secrets.secret_arns["OPENAI_GPT_TERRA"]
        OPENAI_GPT_LUNA   = module.secrets.secret_arns["OPENAI_GPT_LUNA"]
        VOYAGE_CONTEXT_4  = module.secrets.secret_arns["VOYAGE_CONTEXT_4"]
        VOYAGE_RERANK_2_5 = module.secrets.secret_arns["VOYAGE_RERANK_2_5"]
      }, local.judge0_client_secrets)
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
      # THE ONLY PLACE IN THE CLUSTER THAT CAN ESCALATE, AND THE ONLY PLACE
      # THAT CAN REWRITE THE DSN. Both belong to the one-shot job and to
      # nothing that serves a request.
      #
      # `DATABASE_URL` carries a least-privileged role that owns no object, so
      # `alembic upgrade` has to SET ROLE to the owner for DDL, and
      # `app.scripts.provision_app_db_role` has to be able to replace the DSN
      # when it rotates that role's password. The API, the agent and the
      # Lambda worker read the same secret and have neither variable, so
      # neither power is reachable from a route.
      #
      # Why any of this exists: `DATABASE_URL` used to hold a copy of the RDS
      # MASTER password, which `manage_master_user_password` has Secrets
      # Manager rotate on a schedule. It rotated on 2026-09-11, the copy went
      # stale, and every database connection in the product failed at once.
      #
      # The owner is a DEDICATED NOLOGIN role rather than the RDS master, and
      # PostgreSQL 16 is the reason: a role holds no ADMIN OPTION on itself, so
      # the master cannot grant itself to the application role, and it is not a
      # superuser here. It can grant a role it CREATED, so
      # `app.scripts.provision_app_db_role` creates this one, hands it every
      # object with REASSIGN OWNED, and grants it onward -- after which the
      # master is unused and its rotation stops mattering.
      environment = {
        POSTGRES_MIGRATION_ROLE = local.db_owner_role
        APP_DSN_SECRET_ID       = module.secrets.secret_arns["DATABASE_URL"]
      }
    }

    frontend = {
      image = "${module.ecr.repository_urls["frontend"]}:${local.frontend_image_tag}"
      # Temporary solo-test size. The 14-day pilot peak was 35.46% CPU and
      # 9.18% memory at 512/1024; 256/512 is a valid Linux Fargate pair and
      # still leaves ample headroom for one developer's manual E2E testing.
      cpu              = 256
      memory           = 512
      desired_count    = local.service_count
      max_count        = 4
      port             = 3000
      health_path      = "/"
      target_group_arn = local.has_public_entry ? module.alb[0].target_group_arns["frontend"] : null
      readonly_root    = false # Next.js writes its own cache
      # The frontend holds NO secrets. The Razorpay key id it needs is public
      # and arrives at runtime on the API's subscribe and purchase responses,
      # which is why it was
      # never a NEXT_PUBLIC_ build variable.
      secrets = {}
    }

    # THE PROCTORING ANALYSIS SERVICE. Speaker counting over a fifteen-second
    # audio chunk. Its own image because it carries torch and pyannote, its own
    # service because an inference call that pinned a request worker would cost
    # a candidate mid-assessment their next question, and NOT PUBLIC: no target
    # group, no listener rule, reachable only at its Cloud Map name from inside
    # the ECS security group.
    analysis = {
      image         = "${module.ecr.repository_urls["analysis"]}:${local.analysis_image_tag}"
      cpu           = 2048
      memory        = 8192
      desired_count = local.service_count
      # Two, not four: each analysis task is 2 vCPU / 8 GB, the costliest
      # step in the cluster, and its workload (fifteen-second audio chunks)
      # has never occurred in this environment.
      max_count    = 2
      port         = 8100
      health_path  = "/health"
      discoverable = true
      # READ-ONLY ROOT with one exception: the import-time caches torch and
      # matplotlib insist on, which the image points at /tmp.
      readonly_root  = true
      writable_paths = ["/tmp"]
      # The image runs as uid 10001. A Fargate task volume mounts root-owned
      # 0755, so without this the mount is unwritable and torch and matplotlib
      # fail at import -- the same trap that broke sign-in on the API.
      writable_paths_uid = "10001"
      # The Hugging Face token, and nothing else. It holds no DSN and no
      # model-provider key, because all it is handed is audio and all it
      # answers is a speaker count.
      secrets = {
        HUGGINGFACE_TOKEN = module.secrets.secret_arns["HUGGINGFACE_TOKEN"]
      }
    }
  }

  tags = local.tags
}

# ── Background work ──────────────────────────────────────────────────────────

module "lambda" {
  source = "../../modules/lambda"

  project     = var.project
  environment = local.environment
  region      = var.region
  account_id  = var.account_id

  # IN THE VPC, all four. Three of them reach RDS and Redis, which live in
  # subnets with no route to the internet in either direction. The trigger does
  # not need the database, and it is in the VPC anyway: it calls ecs:RunTask
  # through the VPC endpoint rather than out through NAT, which keeps the one
  # function holding iam:PassRole off the public internet entirely.
  vpc_subnet_ids     = module.network.private_subnet_ids
  security_group_ids = [module.network.ecs_security_group_id]

  # ARM64, matching the `ecs` module's default. The same backend image backs the
  # API service, the on-demand agent and three of these four functions, so one
  # architecture is not a preference here, it is a correctness requirement.
  architecture = "arm64"

  secret_policy_arns = module.secrets.policy_arns

  # THE OTHER HALF OF THE INBOUND WEBHOOK'S HANDSHAKE, and the one place in
  # this composition where a credential is delivered as a plain environment
  # variable rather than mounted.
  #
  # `handler.py` reads `os.environ.get("WEBHOOK_SECRET", "")` and sends the
  # header only when it is non-empty. It CANNOT read Secrets Manager, and that
  # is a property worth keeping rather than a gap to fill: this is the one
  # function anything on the open internet can reach, by sending mail to the
  # reply domain, so it is a zip of one file importing the standard library and
  # boto3, with no database credential, no model key and no secret grant at all.
  # Giving it a grant and a cold-start fetch to deliver a value whose whole job
  # is to be shown to our own API would widen the narrowest thing here.
  #
  # The value is the SAME one the API mounts -- one `random_password`, two
  # readers -- so the two sides cannot drift. Empty when `has_inbound` is false,
  # which is the same ternary the function itself is gated on: a secret env var
  # for a function that does not exist is refused by the module rather than
  # dropped.
  function_secret_environment = local.has_inbound ? {
    "inbound-email" = {
      WEBHOOK_SECRET = module.secrets.generated_secret_values["INBOUND_WEBHOOK_SECRET"]
    }
  } : {}

  kms_key_arn        = aws_kms_key.this.arn
  log_retention_days = 30
  failure_topic_arn  = aws_sns_topic.alarms.arn

  # A FUNCTION NOTHING CAN INVOKE IS DEAD INFRASTRUCTURE, so the inbound-mail
  # parser exists only where mail can actually arrive. Merged rather than
  # written into the literal below, because a `count` cannot gate one entry of a
  # map and an entry gated by a ternary on every field would be four lines of
  # noise around one decision.
  functions = merge(local.has_inbound ? local.inbound_email_function : {}, {
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
      # Ten minutes. The binding case is a delivery task backing off sixty
      # seconds between attempts; the retry loop refuses an attempt that cannot
      # finish inside what is left, so this is a ceiling rather than a target.
      timeout_seconds = 600
      # A CEILING, not a reservation. The database has a connection limit and
      # each concurrent invocation opens its own engine; twenty is comfortably
      # inside db.t4g.micro's limit with the API's pool alongside it.
      reserved_concurrency = var.reserve_lambda_concurrency ? 20 : null
      secret_policy_key    = "task-worker"
      # Code sandbox stage B: the client group on THIS function only (the
      # drafting agents never execute code), and the token read its cold-start
      # fetch of JUDGE0_AUTH_TOKEN needs. Empty until `judge0_clients_enabled`.
      extra_security_group_ids = local.judge0_client_security_group_ids
      extra_policy_arns        = local.judge0_token_policy_arns
      # The SAME map the ECS services use. ECS injects these; Lambda has no
      # equivalent, so the function fetches them at cold start with the
      # policy below. Only the ARNs are here.
      secrets = merge({
        DATABASE_URL = module.secrets.secret_arns["DATABASE_URL"]
        # The worker MINTS assessment invite links (workers/tasks.py, the
        # invitation email), which are signed material: it needs the real
        # key, and REQUIRE_JWT_SECRET below makes it refuse to boot in
        # production without it rather than sign with an empty string.
        JWT_SECRET        = module.secrets.secret_arns["JWT_SECRET"]
        REDIS_URL         = module.secrets.secret_arns["REDIS_URL"]
        OPENAI_GPT_TERRA  = module.secrets.secret_arns["OPENAI_GPT_TERRA"]
        OPENAI_GPT_LUNA   = module.secrets.secret_arns["OPENAI_GPT_LUNA"]
        VOYAGE_CONTEXT_4  = module.secrets.secret_arns["VOYAGE_CONTEXT_4"]
        VOYAGE_RERANK_2_5 = module.secrets.secret_arns["VOYAGE_RERANK_2_5"]
        TAVILY_API_KEY    = module.secrets.secret_arns["TAVILY_API_KEY"]
      }, local.judge0_client_secrets)
      environment = merge(local.judge0_client_environment, {
        # Must match the ECS services: agents reach retrieval from here too,
        # and two halves of one product ranking the same query differently
        # is worse than either value applied everywhere. See the note on
        # RETRIEVAL_RERANKER in common_environment above.
        RETRIEVAL_RERANKER = "voyage"
        # AWS_REGION IS NOT SET HERE. It is one of Lambda's RESERVED keys: the
        # runtime injects it with the function's own region, and CreateFunction
        # answers 400 for any request that also supplies it. Nothing is lost --
        # `Settings.aws_region` reads that same variable, so boto3 and the
        # application agree with the platform rather than with a literal.
        ENVIRONMENT                   = local.app_environment
        REQUIRE_JWT_SECRET            = "1"
        S3_BUCKET                     = module.s3.bucket_name
        S3_KMS_KEY_ID                 = aws_kms_key.this.arn
        FRONTEND_URL                  = local.frontend_url
        EMBEDDING_DIMENSIONS          = "1024"
        RESUME_SIGNED_URL_TTL_SECONDS = "300"
        # A task can dispatch another task: `run_matching` dispatches a report
        # synthesis per newly completed candidate.
        TASK_DISPATCH_BACKEND           = "aws"
        PROCTORING_ANALYSIS_SERVICE_URL = local.analysis_service_url
        # THE OUTBOUND MAIL HOP. Delivery is Route.LAMBDA, so this function is
        # where a message actually leaves the platform. It reads the same
        # single transport the services do; the Gmail app password is not
        # mounted here any more, because a credential for a transport this
        # deployment does not use is reach the function does not need.
        EMAIL_TRANSPORT = "ses"
        # SES sends only for an identity the ACCOUNT has verified, so this is
        # not a free-text display address: it must be a verified sender.
        SMTP_FROM_EMAIL = var.platform_from_email
        SMTP_FROM_NAME  = "ReadyPick"
        # `pickready.send_security_email` runs here: the security codes and the
        # account notice leave from this identity, SES only, never SMTP.
        PLATFORM_SECURITY_SENDER_EMAIL = var.platform_security_sender_email
        PLATFORM_SECURITY_SENDER_NAME  = "Vivekium"
        # This function is the hop that actually writes the Reply-To header, so
        # it needs the same value the API used to build the address.
        INBOUND_EMAIL_DOMAIN = local.has_inbound ? local.reply_domain : ""
        # This function is the last hop before SES, so it is the one that must
        # attach the configuration set. Without it SES accepts the message and
        # publishes no event, and every row stays `sent` for ever.
        SES_CONFIGURATION_SET = aws_sesv2_configuration_set.this.configuration_set_name
        SES_SNS_TOPIC_ARN     = aws_sns_topic.ses_events.arn
        # SPEECH TO TEXT RUNS HERE NOW. A spoken answer is transcribed by a
        # Route.LAMBDA task, so this function is the only caller of Amazon
        # Transcribe; the agent task lost the grant and these three values
        # when the video-interview transcription it served was deleted. The
        # region is separate from AWS_REGION because ap-south-2 has no
        # Transcribe endpoint, and the bucket travels with the region because
        # a job cannot read a bucket outside its own. With the feature off the
        # product hides the microphone rather than fabricating a transcript.
        TRANSCRIBE_ENABLED = var.transcribe_enabled ? "true" : "false"
        TRANSCRIBE_REGION  = var.transcribe_region
        # A SPLAT AND A JOIN, never `[0]` behind a conditional. Terraform does
        # not reliably short-circuit an index expression, so `[0]` on a
        # count = 0 resource fails the plan even on the branch that never runs.
        # An empty splat joins to "", which is exactly "no working bucket".
        TRANSCRIBE_BUCKET = join("", aws_s3_bucket.transcribe[*].id)
      })
    }

    "jd-gen" = {
      package     = "image"
      description = "Writes one job description draft. Invoked synchronously by the request handler that is already waiting for it."
      image_uri   = "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
      handler     = "app.workers.entrypoints.agents.jd_generation_handler"
      memory_mb   = 512
      # The task's own model budget is 25s per attempt and 50s in total, inside
      # an agent loop bounded at two attempts. Ten minutes is the ceiling that
      # cannot be reached rather than the time this takes.
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
        ENVIRONMENT           = local.app_environment
        TASK_DISPATCH_BACKEND = "aws"
      }
    }

    "company-profile" = {
      package     = "image"
      description = "Drafts one company's three profile sections from public sources. Synchronous, like jd-gen."
      image_uri   = "${module.ecr.repository_urls["backend"]}:${var.image_tag}"
      handler     = "app.workers.entrypoints.agents.company_profile_handler"
      memory_mb   = 512
      # A web search plus a model call. Longer than jd-gen's real budget
      # because the search is a third party with its own latency.
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
        ENVIRONMENT           = local.app_environment
        TASK_DISPATCH_BACKEND = "aws"
      }
    }

    # THE ONLY ZIP, AND THE ONLY THING HOLDING iam:PassRole.
    #
    # Thirty lines and boto3. It must stay that way: passing a role is a
    # privilege-escalation primitive, since anything that can pass a role can
    # run code as it, and the mitigation is that this function's whole source
    # fits on a screen.
    "assessment-trigger" = {
      package         = "zip"
      description     = "Starts one on-demand assessment-agent task and returns. Holds iam:PassRole and reads no secret."
      source_dir      = "${path.root}/../../../lambda/assessment_trigger"
      handler         = "handler.lambda_handler"
      memory_mb       = 128
      timeout_seconds = 30
      # No secret_policy_key. It reads nothing, which is why it has no entry in
      # the secrets module's map at all.
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
        ECS_CLUSTER         = module.ecs.cluster_name
        ECS_TASK_DEFINITION = module.ecs.on_demand_task_families["agent"]
        ECS_CONTAINER_NAME  = "agent"
        PRIVATE_SUBNET_IDS  = join(",", module.network.private_subnet_ids)
        # The shared ECS group, plus the code sandbox's client group from
        # stage B on (the agent scores coding answers). Comma-joined, which is
        # what the handler splits on; with the switch off this is exactly the
        # one group it always was.
        ECS_SECURITY_GROUP_IDS = join(",", concat(
          [module.network.ecs_security_group_id],
          local.judge0_client_security_group_ids,
        ))
      }
    }

  })

  tags = local.tags
}

# ── Receiving a verification reply ───────────────────────────────────────────
#
# The MX record, the SES receipt rule, the bucket the raw message lands in, and
# the notification that wakes the parser. See `modules/ses_inbound` for why the
# rule matches the whole subdomain and why the message goes through S3 rather
# than straight to the function.

module "ses_inbound" {
  source = "../../modules/ses_inbound"
  count  = local.has_inbound ? 1 : 0

  project     = var.project
  environment = local.environment
  account_id  = var.account_id
  region      = var.region

  reply_domain      = local.reply_domain
  hosted_zone_id    = var.hosted_zone_id
  receiving_regions = var.ses_receiving_regions

  lambda_function_arn  = module.lambda.function_arns["inbound-email"]
  lambda_function_name = module.lambda.function_names["inbound-email"]

  # The environment's own CMK. Its policy already names both `sns` and `ses`
  # with an account condition, which is what an encrypted topic SES publishes
  # to needs, and why the module does not mint one.
  kms_key_arn = aws_kms_key.this.arn

  # PILOT IS THE ONE RECEIVING IN THIS REGION. SES allows exactly one active
  # receipt rule set per region per account, so a second environment setting
  # this would silently take pilot's mail. Stated here so that change is a
  # conflict in a diff rather than an outage nobody can see.
  activate_rule_set = true

  tags = local.tags
}

# ── The schedule ─────────────────────────────────────────────────────────────
#
# MIRRORS `backend/app/workers/schedule.py`, and
# `backend/tests/test_schedule_parity.py` reads both and fails if they drift.
# Two copies of one fact stay honest only when something compares them.

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

# ── Owner-only pilot sleep guard ─────────────────────────────────────────────
#
# RDS automatically starts a stopped instance after seven days.  A sleeping
# owner-only pilot must not then accrue database compute indefinitely because
# the operator's laptop was off.  This universal Scheduler target is the one
# deliberate exception to the disabled application schedules: sleep mode
# enables it, wake mode disables it before starting RDS.  Calling StopDBInstance
# while RDS is already stopped is harmless and retries are disabled.

data "aws_iam_policy_document" "pilot_sleep_guard_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "pilot_sleep_guard" {
  name               = "${var.project}-${local.environment}-rds-sleep-guard"
  assume_role_policy = data.aws_iam_policy_document.pilot_sleep_guard_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "pilot_sleep_guard" {
  statement {
    effect    = "Allow"
    actions   = ["rds:StopDBInstance"]
    resources = ["arn:aws:rds:${var.region}:${var.account_id}:db:${var.project}-${local.environment}"]
  }
}

resource "aws_iam_role_policy" "pilot_sleep_guard" {
  name   = "stop-pilot-rds"
  role   = aws_iam_role.pilot_sleep_guard.id
  policy = data.aws_iam_policy_document.pilot_sleep_guard.json
}

resource "aws_scheduler_schedule" "pilot_sleep_guard" {
  name       = "${var.project}-${local.environment}-rds-sleep-guard"
  group_name = "default"

  schedule_expression = "rate(1 hour)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:rds:stopDBInstance"
    role_arn = aws_iam_role.pilot_sleep_guard.arn
    input = jsonencode({
      DbInstanceIdentifier = "${var.project}-${local.environment}"
    })

    retry_policy {
      maximum_retry_attempts = 0
    }
  }

  # Operational state belongs to scripts/pilot_power.py.  Terraform owns the
  # target, permission and cadence without waking or disabling the guard during
  # an ordinary deploy.
  state = "DISABLED"
  lifecycle {
    ignore_changes = [state]
  }

  depends_on = [aws_iam_role_policy.pilot_sleep_guard]
}

# ── Observability ────────────────────────────────────────────────────────────

module "observability" {
  source = "../../modules/observability"

  project     = var.project
  environment = local.environment
  region      = var.region

  alarm_topic_arn = aws_sns_topic.alarms.arn

  # The load balancer alarms exist only when the load balancer does. Gated on a
  # bool derived from a variable, so the count is known at plan time.
  enable_alb_alarms        = local.has_public_entry
  load_balancer_arn_suffix = local.has_public_entry ? module.alb[0].arn_suffix : null
  target_group_arn_suffix  = local.has_public_entry ? module.alb[0].target_group_arn_suffixes["api"] : null

  db_instance_id = module.rds.instance_id

  # One alarm per function. See the module: a single aggregate error rate would
  # be dominated by whichever function is busiest, so the task worker running
  # every email in the product could hide the JD writer failing every call.
  function_names = module.lambda.function_names

  # The metric filter over `ecs_task.failed`. There is no ECS metric for "a
  # task exited non-zero", and the Lambda that started it returned as soon as
  # RunTask was accepted, so this log line is the only report of the failure.
  agent_log_group_name = module.ecs.log_group_names["agent"]

  # PLAN-p5 WP5-E. The metric filter over `rag.repair.degraded`, the only
  # report of a semantic index repair sweep that cannot embed.
  task_worker_log_group_name = module.lambda.log_group_names["task-worker"]

  # ADDED 2026-09-17. Both were visible on the dashboard and watched by
  # nothing. See the module for what each alarm catches.
  #
  # The connection threshold is roughly 80 percent of what RDS derives from
  # this instance class: db.t4g.medium (4 GiB, ~450 connections).
  # It is stated per environment because the ceiling is a property of the
  # instance and a shared default would be wrong everywhere but here.
  db_connection_alarm_threshold = 360

  # Computed in `locals` rather than read off the module, because a
  # `for_each` over a resource attribute that is unknown until apply cannot
  # be planned.
  redis_cluster_ids = local.redis_cluster_ids

  kms_key_arn = aws_kms_key.this.arn
  tags        = local.tags
}

# ── The code sandbox (Judge0 CE), DISABLED BY DEFAULT ────────────────────────
#
# Candidate code runs on this host and nowhere else. `judge0_enabled` defaults
# to false, so an apply with the defaults creates NOTHING here and changes
# nothing above (the network module's extra endpoint client list stays empty).
# The rollout is staged and each stage is its own reviewed plan:
#
#   A1  judge0_enabled = true            subnet, route table, S3 endpoint, NACL,
#                                        groups, role, registries, token, alarms.
#                                        The one change to an existing resource
#                                        is the endpoints group admitting the
#                                        host group.
#   A2  judge0_instance_enabled = true   the instance, after the images are
#       + ami + digests                  mirrored (scripts/mirror-judge0-images.sh).
#   B   judge0_clients_enabled = true    `client_security_group_id` on the API,
#                                        the task worker and the agent, the
#                                        token mount and JUDGE0_URL, with
#                                        CODE_EXECUTION_BACKEND still disabled.
#                                        Wired below (`local.judge0_*`).
#   C   CODE_EXECUTION_BACKEND = judge0  only after the sandbox verification
#                                        task is green.
#
# docs/operations/JUDGE0_RUNBOOK.md is the procedure and the cost estimate.

module "code_sandbox" {
  source = "../../modules/code_sandbox"
  count  = var.judge0_enabled ? 1 : 0

  project     = var.project
  environment = local.environment
  region      = var.region

  vpc_id            = module.network.vpc_id
  availability_zone = var.availability_zones[0]
  # 10.0.30.0/24: the private tier is 10+i and the data tier 20+i, so 30 is
  # clear of both for as many zones as this VPC will ever have.
  sandbox_cidr_block                    = cidrsubnet(module.network.vpc_cidr_block, 8, 30)
  private_subnet_cidr_blocks            = module.network.private_subnet_cidr_blocks
  data_subnet_cidr_blocks               = module.network.data_subnet_cidr_blocks
  interface_endpoints_security_group_id = module.network.endpoints_security_group_id

  register_in_namespace    = true
  discovery_namespace_id   = module.ecs.discovery_namespace_id
  discovery_namespace_name = local.internal_namespace

  create_instance = var.judge0_instance_enabled
  ami_id          = var.judge0_ami_id
  instance_type   = var.judge0_instance_type
  image_digests   = var.judge0_image_digests

  kms_key_arn     = aws_kms_key.this.arn
  kms_key_id      = aws_kms_key.this.key_id
  alarm_topic_arn = aws_sns_topic.alarms.arn

  tags = local.tags
}

# Stage B, the callers. Everything here is EMPTY unless both
# `judge0_enabled` and `judge0_clients_enabled` are true, so a default apply
# (and a stage A1 or A2 apply) changes no service, task definition or function.
# Splats rather than `[0]`: Terraform does not reliably short-circuit an index
# on a count = 0 module, and an empty splat is exactly "no sandbox".
locals {
  judge0_clients = var.judge0_enabled && var.judge0_clients_enabled

  # API service, task worker Lambda, and (through the trigger) the on-demand
  # agent. NOT the frontend, the analysis service or the drafting Lambdas.
  judge0_client_security_group_ids = local.judge0_clients ? module.code_sandbox[*].client_security_group_id : []
  judge0_token_policy_arns         = local.judge0_clients ? module.code_sandbox[*].token_read_policy_arn : []
  judge0_client_secrets = local.judge0_clients ? {
    JUDGE0_AUTH_TOKEN = one(module.code_sandbox[*].token_secret_arn)
  } : {}
  judge0_client_environment = local.judge0_clients ? {
    JUDGE0_URL = one(module.code_sandbox[*].sandbox_url)
  } : {}
}

check "judge0_clients_need_the_module" {
  assert {
    condition     = !var.judge0_clients_enabled || var.judge0_enabled
    error_message = "judge0_clients_enabled is true but judge0_enabled is false, so there is no sandbox for the callers to reach and nothing is wired."
  }
}

# The instance lives inside the module the first switch creates, so the second
# switch alone would create nothing, silently. A check block rather than a
# cross-variable validation keeps this file valid on every Terraform version
# `required_version` admits; the plan prints the warning.
check "judge0_instance_needs_the_module" {
  assert {
    condition     = !var.judge0_instance_enabled || var.judge0_enabled
    error_message = "judge0_instance_enabled is true but judge0_enabled is false, so no sandbox instance will be created."
  }
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
