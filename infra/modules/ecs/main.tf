/**
 * ECS Fargate: the cluster, the services, and the per-service IAM roles.
 *
 * FARGATE, because spec-doc5 §D.3 names it as "the direct equivalent of the
 * Cloud Run model this platform is migrating from" -- and it is: a container,
 * a request, no host to patch. The one thing it is not equivalent on is
 * scale-to-zero, and that is called out in the autoscaling block below rather
 * than discovered later on a bill.
 *
 * ONE TASK ROLE PER SERVICE. THIS IS THE POINT OF THE MODULE.
 * ---------------------------------------------------------------
 * spec-doc5 §D.4: "IAM policies must be scoped per-service, not a shared broad
 * role -- the DATABASE_URL exposure finding from the GCP phase (a composed DSN
 * readable via a broad permission) is the exact class of mistake to design out
 * here from the start rather than harden later."
 *
 * So `var.services` is a map, and each entry gets:
 *
 *   its own TASK ROLE       what the running code can do
 *   its own EXECUTION ROLE  what the ECS agent can do on its behalf
 *   its own secret policy   from the `secrets` module, enumerated per service
 *
 * The two roles are separate because they are used at different times by
 * different principals, and conflating them is the usual mistake: the execution
 * role pulls the image and fetches secrets to INJECT, before the container
 * starts; the task role is what the application's own AWS SDK calls use. A
 * single role means the application can read every secret the platform injects,
 * whether or not it was given one.
 *
 * SECRETS ARE INJECTED BY ECS, NOT COMPOSED BY THE APPLICATION.
 * The `secrets` block on the container definition makes ECS fetch the value and
 * set the environment variable. That is what "mounted rather than composed into
 * a loggable env var where the platform allows it" means here -- the value never
 * passes through a shell, a startup script or a log line on its way in.
 *
 * THE WORKER AND BEAT RUN THE SAME IMAGE WITH A DIFFERENT COMMAND, which is why
 * `command` is per-service and the image is not. They are separate SERVICES
 * rather than separate containers in one task because they scale differently:
 * the worker scales on queue depth and beat must be exactly one, forever.
 */

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60"
    }
  }
}

locals {
  name = "${var.project}-${var.environment}"
}

resource "aws_ecs_cluster" "this" {
  name = local.name

  setting {
    # Per-task CloudWatch metrics. Off by default and worth the small cost:
    # without it, "is the worker actually saturated" is a question with no data
    # behind it, and the answer decides whether to scale or to fix.
    name  = "containerInsights"
    value = var.environment == "production" ? "enhanced" : "disabled"
  }

  tags = merge(var.tags, { Name = local.name })
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }
}

resource "aws_cloudwatch_log_group" "this" {
  for_each = var.services

  name              = "/ecs/${local.name}/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn

  tags = merge(var.tags, { Service = each.key })
}

# ── The execution role: what the ECS AGENT may do, before the container runs ─

data "aws_iam_policy_document" "execution_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  for_each = var.services

  name               = "${local.name}-${each.key}-execution"
  description        = "ECS agent: pull the image, fetch this service's secrets, write its logs. NOT what the application code runs as."
  assume_role_policy = data.aws_iam_policy_document.execution_assume.json

  tags = merge(var.tags, { Service = each.key, RoleKind = "execution" })
}

data "aws_iam_policy_document" "execution" {
  for_each = var.services

  # SCOPED TO THIS ENVIRONMENT'S REPOSITORIES, not `ecr:*`. The managed
  # `AmazonECSTaskExecutionRolePolicy` grants pull on every repository in the
  # account, which is how a staging task ends up able to pull a production
  # image.
  statement {
    sid       = "AuthToTheRegistry"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # This action has no resource form. It grants a token, not access.
  }

  statement {
    sid    = "PullOnlyThisEnvironmentsImages"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = var.ecr_repository_arns
  }

  statement {
    sid    = "WriteThisServicesLogsAndNoOthers"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.this[each.key].arn}:*"]
  }

  statement {
    sid    = "DecryptTheLogGroup"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey",
    ]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "execution" {
  for_each = var.services

  name   = "${local.name}-${each.key}-execution"
  role   = aws_iam_role.execution[each.key].id
  policy = data.aws_iam_policy_document.execution[each.key].json
}

# The per-service secret policy from the `secrets` module. Attached to the
# EXECUTION role, because ECS injects the value before the container starts --
# the application never calls Secrets Manager itself.
#
# ONLY THE SERVICES THAT ACTUALLY MOUNT A SECRET GET AN ATTACHMENT.
#
# `frontend` declares `secrets = {}` on purpose: the Razorpay key id it needs is
# public and arrives at runtime on the API's own subscribe and purchase
# responses, which is why it was never a `NEXT_PUBLIC_` build variable. Attaching a Secrets Manager policy
# to it anyway would be a grant nothing uses, which is the same shape as the
# GCP-phase finding this module exists to design out.
#
# Iterating over every service was also a latent apply-time failure rather than
# a mere over-grant: `secret_policy_arns` has no `frontend` key, so
# `var.secret_policy_arns["frontend"]` raised `Invalid index`. `terraform
# validate` cannot see it, because it cannot evaluate a `for_each` key against a
# map's contents; the offline plan added in spec-doc6 §13.3 is what found it.
resource "aws_iam_role_policy_attachment" "execution_secrets" {
  for_each = { for name, service in var.services : name => service if length(service.secrets) > 0 }

  role = aws_iam_role.execution[each.key].name
  # Indexed rather than looked up with a default: a service that DOES mount
  # secrets and has no scoped policy must FAIL THE PLAN rather than run with
  # none. The precondition below says so in words rather than as `Invalid
  # index`, because the index error names the map and not the mistake.
  policy_arn = var.secret_policy_arns[each.key]

  lifecycle {
    precondition {
      condition     = contains(keys(var.secret_policy_arns), each.key)
      error_message = "The service mounts secrets but has no entry in the `secrets` module's `service_secrets`. Add its exact secret list there; do not widen another service's policy to cover it."
    }
  }
}

# A mounted secret another module owns (the code sandbox token), read by the
# EXECUTION role for the same reason as the policy above: it is the ECS agent
# that fetches and injects it. Keyed by "<service>/<position>", because an ARN
# created in the same apply cannot key a for_each.
resource "aws_iam_role_policy_attachment" "execution_extra" {
  for_each = merge([
    for name, service in var.services : {
      for index, arn in service.extra_execution_policy_arns : "${name}/${index}" => {
        service = name
        arn     = arn
      }
    }
  ]...)

  role       = aws_iam_role.execution[each.value.service].name
  policy_arn = each.value.arn
}

# ── The task role: what the APPLICATION CODE may do ─────────────────────────

resource "aws_iam_role" "task" {
  for_each = var.services

  name               = "${local.name}-${each.key}-task"
  description        = "What ${each.key}'s own code can do. Separate from the execution role: conflating them lets the application read every secret the platform injects."
  assume_role_policy = data.aws_iam_policy_document.execution_assume.json

  tags = merge(var.tags, { Service = each.key, RoleKind = "task" })
}

# S3 access goes on the TASK role, because it is the application's own boto3
# client making the call. Only the services that actually store files get it --
# `beat` schedules and never touches an object.
resource "aws_iam_role_policy_attachment" "task_s3" {
  for_each = { for name, service in var.services : name => service if service.needs_s3 }

  role       = aws_iam_role.task[each.key].name
  policy_arn = var.s3_policy_arn
}

# Replacing a secret's value goes on the TASK role for the same stated reason
# S3 does: it is the application's own boto3 client making the call. The READ
# policy above is on the execution role, which fetches secrets and injects them
# before the container starts, so a write statement added there is a grant the
# code can never use. `rotate-app-db-credential.sh` failed in pilot with
# AccessDeniedException on exactly that mistake.
resource "aws_iam_role_policy_attachment" "task_secret_writer" {
  for_each = var.secret_writer_policy_arns

  role       = aws_iam_role.task[each.key].name
  policy_arn = each.value
}

# ECS Exec, for an operator opening a shell in a running task. PRODUCTION ONLY
# BY EXPLICIT OPT-IN, and off by default: a shell in a container holding
# candidate data is a real capability, and it should be a decision rather than
# an inheritance.
data "aws_iam_policy_document" "exec" {
  statement {
    effect = "Allow"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"] # SSM messaging has no resource form.
  }
}

resource "aws_iam_role_policy" "exec" {
  for_each = var.enable_execute_command ? var.services : {}

  name   = "${local.name}-${each.key}-exec"
  role   = aws_iam_role.task[each.key].id
  policy = data.aws_iam_policy_document.exec.json
}

# ── Service discovery ────────────────────────────────────────────────────────
#
# HOW ONE TASK FINDS ANOTHER WHEN NEITHER IS BEHIND THE LOAD BALANCER.
#
# The api and the worker post audio chunks to the analysis service, which has
# no target group and no public path on purpose: it is reachable only from
# inside the VPC, on the enumerated internal port the network module opens from
# the task security group to itself. A Fargate task's IP changes on every
# deploy, so "reachable" needs a NAME, and Cloud Map is what supplies one.
#
# A PRIVATE DNS namespace, attached to one VPC. It resolves nowhere else, so an
# internal service name is not a name anybody outside can even look up. ECS
# registers and deregisters the task IPs itself as tasks come and go, which is
# why `health_check_custom_config` is the right choice here: the ECS health
# check is already the authority on whether a task should receive traffic, and
# a second Route53 health check would be a different opinion about the same
# question.

resource "aws_service_discovery_private_dns_namespace" "this" {
  count = var.discovery_namespace == "" ? 0 : 1

  name        = var.discovery_namespace
  vpc         = var.vpc_id
  description = "Internal service names for ${local.name}. Resolves in this VPC and nowhere else."

  tags = merge(var.tags, { Name = var.discovery_namespace })
}

resource "aws_service_discovery_service" "this" {
  for_each = { for name, service in var.services : name => service if service.discoverable }

  name        = each.key
  description = "${each.key}.${var.discovery_namespace}, resolved to this service's running task IPs."

  dns_config {
    namespace_id = one(aws_service_discovery_private_dns_namespace.this[*].id)

    dns_records {
      type = "A"
      # SHORT, because the answer is a set of task IPs and a deploy replaces
      # them. A long TTL is a caller holding the address of a task that has
      # already been drained.
      ttl = 15
    }

    # Every healthy task, not one. The callers are the api and the worker,
    # which balance across whatever the resolver returns.
    routing_policy = "MULTIVALUE"
  }

  # ECS owns registration and deregistration. A Route53 health check here would
  # be a second opinion about a question the ECS health check already answers.
  health_check_custom_config {
    failure_threshold = 1
  }

  lifecycle {
    precondition {
      condition     = var.discovery_namespace != ""
      error_message = "A service is marked `discoverable` but no `discovery_namespace` was passed, so there is no namespace to register it in and nothing could resolve its name."
    }
  }

  tags = merge(var.tags, { Service = each.key })
}

# ── Task definitions ─────────────────────────────────────────────────────────

resource "aws_ecs_task_definition" "this" {
  for_each = var.services

  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution[each.key].arn
  task_role_arn            = aws_iam_role.task[each.key].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  # An empty ephemeral volume per writable path. This is what lets a container
  # that only needs to write a library cache keep `readonly_root = true`:
  # everything except the named path stays immutable for the life of the task.
  # Fargate has no tmpfs, so these are task volumes rather than memory ones.
  dynamic "volume" {
    for_each = { for path in each.value.writable_paths : path => replace(trim(path, "/"), "/", "-") }
    content {
      name = volume.value
    }
  }

  container_definitions = jsonencode(concat(
    # THE INIT CONTAINER, and only where one is asked for. It runs as root,
    # takes ownership of every mounted scratch path for the application's uid
    # and exits; the application container does not start until it has
    # succeeded. `user = "0"` overrides the image's own USER for THIS container
    # alone, so the application container still runs unprivileged.
    #
    # It reuses the SAME image rather than pulling a second one: a task that
    # needs two images to start has two ways to fail, and this one only ever
    # runs chown.
    each.value.writable_paths_uid == null || length(each.value.writable_paths) == 0 ? [] : [
      {
        name       = "${each.key}-init"
        image      = each.value.image
        user       = "0"
        essential  = false
        entryPoint = ["sh", "-c"]
        command = [
          join(" && ", [
            for path in each.value.writable_paths :
            "chown ${each.value.writable_paths_uid}:${each.value.writable_paths_uid} ${path} && chmod 1777 ${path}"
          ])
        ]
        mountPoints = [
          for path in each.value.writable_paths : {
            sourceVolume  = replace(trim(path, "/"), "/", "-")
            containerPath = path
            readOnly      = false
          }
        ]
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.this[each.key].name
            "awslogs-region"        = var.region
            "awslogs-stream-prefix" = "${each.key}-init"
          }
        }
      }
    ],
    [
      {
        name = each.key
        # BY DIGEST WHERE ONE IS SUPPLIED, otherwise by SHA tag. spec-doc5 §D.6
        # asks for verification "by image digest, not by CI exit code"; pinning
        # the digest in the task definition is what makes the verification
        # meaningful, because the running task cannot then be a different image
        # that happens to share a tag.
        image     = each.value.image
        command   = each.value.command
        essential = true

        # SIGTERM to SIGKILL, and NOT a runtime ceiling: an on-demand task runs
        # until its process exits and ECS imposes no limit on that. This is the
        # window a task gets when something else stops it, and Fargate caps it at
        # 120 seconds. Raising it from the 30-second default is what gives an
        # interrupted assessment a chance to finish the piece it is on rather
        # than leaving a conversation scored with no report written.
        stopTimeout = each.value.stop_timeout

        portMappings = each.value.port == null ? [] : [
          {
            containerPort = each.value.port
            protocol      = "tcp"
          }
        ]

        # Plain values only. Anything sensitive is in `secrets` below, which ECS
        # fetches and injects -- so the value never passes through a shell, a
        # startup script or a log line.
        # `PORT` IS DERIVED FROM THE DECLARED PORT, NOT LEFT TO THE IMAGE.
        #
        # Both images read PORT and both default it to something: the backend's
        # entrypoint to 8000, the frontend's Dockerfile to 8080, which is a Cloud
        # Run convention left over from the previous platform. Nothing on ECS
        # injects it, so the container listened on 8080 while the task definition,
        # the target group and the health check all said 3000.
        #
        # The symptom was as unhelpful as it sounds: the task RUNS, the log says
        # `Ready`, and the load balancer reports "Health checks failed" with no
        # error anywhere, because nothing was listening where anybody looked.
        #
        # Setting it here makes the task definition's `port` the single source of
        # truth, so a container cannot listen anywhere else. An explicit `PORT` in
        # a service's own `environment` still wins, because `merge` puts it last:
        # that is an override, and an override should be possible.
        environment = [
          for key, value in merge(
            var.common_environment,
            each.value.port == null ? {} : { PORT = tostring(each.value.port) },
            each.value.environment,
          ) :
          { name = key, value = tostring(value) }
        ]

        secrets = [
          for key, arn in each.value.secrets : { name = key, valueFrom = arn }
        ]

        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.this[each.key].name
            "awslogs-region"        = var.region
            "awslogs-stream-prefix" = each.key
          }
        }

        # A HEALTH CHECK ONLY WHERE THERE IS SOMETHING TO CHECK. The on-demand
        # agent has no HTTP endpoint and is not meant to stay alive: it runs one
        # dispatched task and exits, so a health check on it would report
        # unhealthy at exactly the moment it succeeded.
        healthCheck = each.value.health_path == null ? null : {
          command     = ["CMD-SHELL", "curl -fsS http://localhost:${each.value.port}${each.value.health_path} || exit 1"]
          interval    = 30
          timeout     = 5
          retries     = 3
          startPeriod = 60
        }

        mountPoints = [
          for path in each.value.writable_paths : {
            sourceVolume  = replace(trim(path, "/"), "/", "-")
            containerPath = path
            readOnly      = false
          }
        ]

        # Root filesystem read-only where the workload allows it. The backend
        # writes nothing to disk by design -- resume bytes never persist on the
        # application filesystem -- so this is enforcing an invariant the code
        # already claims rather than adding a constraint.
        readonlyRootFilesystem = each.value.readonly_root
        linuxParameters = {
          # An init process to reap zombies. Still wanted: the resume and project
          # parsers shell out to nothing, but the Python runtime and the AWS SDK
          # both spawn helper threads and processes, and a container running as
          # PID 1 without an init leaves anything they orphan behind.
          initProcessEnabled = true
        }

        # WAIT FOR THE CHOWN. Without this the application container races the
        # init container and usually loses, which would make the whole fix look
        # intermittent -- the worst failure mode there is for an auth bug.
        dependsOn = each.value.writable_paths_uid == null || length(each.value.writable_paths) == 0 ? null : [
          {
            containerName = "${each.key}-init"
            condition     = "SUCCESS"
          }
        ]
      }
    ],
  ))

  tags = merge(var.tags, { Service = each.key })
}

# ── Services ─────────────────────────────────────────────────────────────────

# An `on_demand` entry is DELIBERATELY ABSENT from here. It has a task
# definition, a task role and a log group, and nothing that keeps a container
# running: `readypick-assessment-trigger` calls RunTask against the family when
# work arrives, and the task stops when the process exits. That is what makes
# the long AI work cost only what it uses.
resource "aws_ecs_service" "this" {
  for_each = { for name, service in var.services : name => service if !service.on_demand }

  name            = "${local.name}-${each.key}"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this[each.key].arn
  desired_count   = each.value.desired_count
  launch_type     = "FARGATE"

  enable_execute_command = var.enable_execute_command

  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = concat([var.ecs_security_group_id], each.value.extra_security_group_ids)
    # PRIVATE SUBNETS, so no public IP. Egress is through NAT, which is why the
    # network module has one.
    assign_public_ip = false
  }

  # The Cloud Map registration, for a service nothing routes to from outside.
  dynamic "service_registries" {
    for_each = each.value.discoverable ? [aws_service_discovery_service.this[each.key].arn] : []
    content {
      registry_arn = service_registries.value
    }
  }

  dynamic "load_balancer" {
    for_each = each.value.target_group_arn == null ? [] : [each.value.target_group_arn]
    content {
      target_group_arn = load_balancer.value
      container_name   = each.key
      container_port   = each.value.port
    }
  }

  # A rolling deploy with a circuit breaker. `rollback = true` is what makes
  # this different from watching it fail: a deployment whose tasks will not
  # become healthy is reverted automatically rather than leaving the service
  # half-migrated while somebody notices.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # 100/200 gives a genuine zero-downtime rolling deploy: the new tasks come up
  # alongside the old ones and nothing is drained until they are healthy.
  deployment_minimum_healthy_percent = each.value.min_healthy_percent
  deployment_maximum_percent         = each.value.max_percent

  # A new service's tasks need time to pass a health check before the LB starts
  # counting failures against them.
  health_check_grace_period_seconds = each.value.target_group_arn == null ? null : 90

  propagate_tags = "SERVICE"
  tags           = merge(var.tags, { Service = each.key })

  lifecycle {
    # CI updates the task definition with a new image; Terraform must not then
    # revert it to whatever the last apply pinned. This is the standard
    # arrangement when a pipeline deploys and Terraform owns the shape.
    ignore_changes = [task_definition, desired_count]
  }
}

# ── Autoscaling ──────────────────────────────────────────────────────────────
#
# FARGATE DOES NOT SCALE TO ZERO, and that is the one place it is not equivalent
# to Cloud Run. `min_capacity` is at least 1 for every service, so this platform
# has a floor cost Cloud Run did not -- stated here rather than discovered on a
# bill. A service that genuinely should idle at zero belongs on Lambda or on a
# scheduled task, not on a Fargate service with a min of 0 that cannot exist.

resource "aws_appautoscaling_target" "this" {
  for_each = { for name, service in var.services : name => service if !service.on_demand && service.max_count > service.desired_count }

  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.this[each.key].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = each.value.desired_count
  max_capacity       = each.value.max_count

  lifecycle {
    # Desired count is already an operational setting (the ECS service below
    # ignores it).  The pilot power controller uses AWS's supported temporary
    # suspension flag while services are intentionally at zero.  Terraform
    # must not resume scaling during a deploy and thereby wake a sleeping
    # environment.  The configured min/max remain Terraform-owned and are not
    # ignored; launch-mode restores the suspension flags to false.
    ignore_changes = [suspended_state]
  }
}

resource "aws_appautoscaling_policy" "cpu" {
  for_each = aws_appautoscaling_target.this

  name               = "${local.name}-${each.key}-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = each.value.resource_id
  scalable_dimension = each.value.scalable_dimension
  service_namespace  = each.value.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value = 65

    # SCALE OUT FAST, SCALE IN SLOWLY. The asymmetry is deliberate: scaling out
    # late costs latency a candidate feels mid-assessment, while scaling in
    # early costs a few minutes of an instance nobody is using.
    scale_out_cooldown = 60
    scale_in_cooldown  = 300
  }
}
