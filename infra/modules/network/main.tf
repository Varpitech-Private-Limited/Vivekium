/**
 * VPC, subnets and security groups (spec-doc5 §D.4).
 *
 * THE SUBNET SPLIT IS THE SECURITY BOUNDARY, and every other module in this
 * layout depends on it being right:
 *
 *   public   NAT gateways and the load balancer. Nothing else.
 *   private  ECS tasks. Egress through NAT, no inbound from the internet.
 *   data     RDS and ElastiCache. NO ROUTE TO THE INTERNET AT ALL -- not even
 *            outbound through NAT.
 *
 * That third tier is the one worth arguing for, because two tiers is the common
 * shape and it is nearly as good. A database subnet with a NAT route can still
 * be reached OUTWARD from a compromised host, which is exactly the path an
 * exfiltration takes: the attacker does not need to reach the database from the
 * internet, they need the database's host to reach them. Removing the route
 * removes the path.
 *
 * SECURITY GROUPS REFERENCE EACH OTHER, NEVER CIDR BLOCKS. `rds` accepts 5432
 * from the ECS security group's ID, not from `10.0.0.0/16`. A CIDR rule says
 * "anything that happens to be in this address range", which includes whatever
 * gets deployed into that VPC next year; a group reference says "the thing that
 * runs the application", which is what was meant.
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

  # Three tiers x the availability zones. RDS requires a subnet group spanning
  # at least two AZs even for a single-AZ instance, so two is the floor rather
  # than a resilience preference.
  az_count = length(var.availability_zones)
}

resource "aws_vpc" "this" {
  cidr_block         = var.cidr_block
  enable_dns_support = true
  # REQUIRED, not optional. RDS and ElastiCache are reached by hostname, and
  # without DNS hostnames the endpoint attribute resolves to nothing from inside
  # the VPC -- which surfaces as an application that cannot reach its database
  # while every security group looks correct.
  enable_dns_hostnames = true

  tags = merge(var.tags, { Name = local.name })
}

# ── The default security group ───────────────────────────────────────────────
#
# EVERY VPC IS CREATED WITH ONE, AND IT ALLOWS ALL TRAFFIC BETWEEN ANYTHING
# ATTACHED TO IT. Nothing in this module attaches anything to it, which is why
# it is easy to leave alone: it is invisible until the day somebody launches a
# resource without naming a security group, and AWS silently attaches it to this
# one. At that point the resource has unrestricted access to every other
# resource that made the same mistake, and no rule anybody wrote says so.
#
# Managing it here with no rules at all makes the default deny rather than
# allow. It is not deleted, because AWS does not permit deleting it.
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id

  # No ingress and no egress blocks: an empty `aws_default_security_group`
  # revokes every rule the VPC shipped with.

  tags = merge(var.tags, { Name = "${local.name}-default-DO-NOT-USE" })
}

# ── Flow logs ────────────────────────────────────────────────────────────────
#
# THE ONLY RECORD OF WHAT ACTUALLY TALKED TO WHAT.
#
# Every other control in this module is preventive: a security group refuses a
# packet and leaves no trace of having refused it. That is the right default and
# it means a question asked after an incident -- "did anything reach the data
# subnet, and from where" -- has no answer at all without this.
#
# REJECT and ACCEPT both, not just REJECT. A rejected packet tells you somebody
# tried; an accepted one tells you what a compromised host actually reached, and
# that is the half an investigation needs.

resource "aws_cloudwatch_log_group" "flow_logs" {
  name              = "/aws/vpc/${local.name}/flow-logs"
  retention_in_days = var.flow_log_retention_days
  kms_key_id        = var.kms_key_arn

  tags = merge(var.tags, { Name = "${local.name}-flow-logs" })
}

data "aws_iam_policy_document" "flow_logs_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "flow_logs" {
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    # THIS LOG GROUP, not every log group in the account. The common form of
    # this policy uses `*`, which lets the flow log service write anywhere --
    # including over an application's log group.
    resources = ["${aws_cloudwatch_log_group.flow_logs.arn}:*"]
  }
}

resource "aws_iam_role" "flow_logs" {
  name               = "${local.name}-flow-logs"
  description        = "What the VPC flow log service may write, and where."
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume.json

  tags = merge(var.tags, { Name = "${local.name}-flow-logs" })
}

resource "aws_iam_role_policy" "flow_logs" {
  name   = "${local.name}-flow-logs"
  role   = aws_iam_role.flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs.json
}

resource "aws_flow_log" "this" {
  vpc_id                   = aws_vpc.this.id
  traffic_type             = "ALL"
  log_destination_type     = "cloud-watch-logs"
  log_destination          = aws_cloudwatch_log_group.flow_logs.arn
  iam_role_arn             = aws_iam_role.flow_logs.arn
  max_aggregation_interval = 60

  tags = merge(var.tags, { Name = local.name })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${local.name}-igw" })
}

# ── Public ───────────────────────────────────────────────────────────────────

resource "aws_subnet" "public" {
  count             = local.az_count
  vpc_id            = aws_vpc.this.id
  availability_zone = var.availability_zones[count.index]
  cidr_block        = cidrsubnet(var.cidr_block, 8, count.index)

  # NOTHING IN THESE SUBNETS WANTS AN AUTO-ASSIGNED PUBLIC IP, so the default
  # that hands one out is turned off rather than left inert.
  #
  # Exactly three things sit here and none of them is affected: the route table
  # association, the NAT gateways (which carry an explicitly allocated
  # `aws_eip`), and the load balancer (which is addressed by the ELB service).
  # Every task runs in the PRIVATE subnets and every database in the DATA ones.
  #
  # It was `true`, which changed nothing today and quietly decided the future:
  # the next thing launched into a public subnet, by a console click or by a
  # module somebody adds, would have reached the internet directly and been
  # reachable from it, with no line in any diff saying so. Turning it off makes
  # that a deliberate per-resource `associate_public_ip_address` instead.
  map_public_ip_on_launch = false

  tags = merge(var.tags, { Name = "${local.name}-public-${count.index}", Tier = "public" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = merge(var.tags, { Name = "${local.name}-public" })
}

resource "aws_route_table_association" "public" {
  count          = local.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ── NAT ──────────────────────────────────────────────────────────────────────
#
# ONE NAT GATEWAY IN STAGING, ONE PER AZ IN PRODUCTION. A NAT gateway costs
# roughly $32/month before data, so a per-AZ pair in staging is $64/month buying
# resilience for an environment whose whole purpose is to be disposable. In
# production the trade runs the other way: a single NAT is a single AZ failure
# away from every task losing egress, which means losing the model provider.

resource "aws_eip" "nat" {
  count  = var.single_nat_gateway ? 1 : local.az_count
  domain = "vpc"
  tags   = merge(var.tags, { Name = "${local.name}-nat-${count.index}" })
}

resource "aws_nat_gateway" "this" {
  count         = var.single_nat_gateway ? 1 : local.az_count
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  depends_on    = [aws_internet_gateway.this]

  tags = merge(var.tags, { Name = "${local.name}-nat-${count.index}" })
}

# ── Private (ECS tasks) ──────────────────────────────────────────────────────

resource "aws_subnet" "private" {
  count             = local.az_count
  vpc_id            = aws_vpc.this.id
  availability_zone = var.availability_zones[count.index]
  cidr_block        = cidrsubnet(var.cidr_block, 8, count.index + 10)

  tags = merge(var.tags, { Name = "${local.name}-private-${count.index}", Tier = "private" })
}

resource "aws_route_table" "private" {
  count  = local.az_count
  vpc_id = aws_vpc.this.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[var.single_nat_gateway ? 0 : count.index].id
  }

  tags = merge(var.tags, { Name = "${local.name}-private-${count.index}" })
}

resource "aws_route_table_association" "private" {
  count          = local.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# ── Data (RDS, ElastiCache) ──────────────────────────────────────────────────

resource "aws_subnet" "data" {
  count             = local.az_count
  vpc_id            = aws_vpc.this.id
  availability_zone = var.availability_zones[count.index]
  cidr_block        = cidrsubnet(var.cidr_block, 8, count.index + 20)

  tags = merge(var.tags, { Name = "${local.name}-data-${count.index}", Tier = "data" })
}

# NO ROUTE TO 0.0.0.0/0. The route table carries only the VPC-local route
# Terraform creates implicitly. See the module docstring: an outbound path from
# the data tier is the path an exfiltration takes.
resource "aws_route_table" "data" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${local.name}-data" })
}

resource "aws_route_table_association" "data" {
  count          = local.az_count
  subnet_id      = aws_subnet.data[count.index].id
  route_table_id = aws_route_table.data.id
}

# ── VPC endpoints ────────────────────────────────────────────────────────────
#
# WITHOUT THESE, EVERY IMAGE PULL AND EVERY SECRET READ GOES OUT THROUGH NAT
# AND BACK IN. That is not a security problem -- the traffic is TLS to an AWS
# endpoint either way -- it is a cost and a latency one: a task that restarts
# pulls its whole image through a metered NAT gateway.
#
# The S3 endpoint is a GATEWAY endpoint (free, a route table entry); the rest
# are INTERFACE endpoints (~$7/month each). Both kinds are here because
# resume storage is on S3 and it is the highest-volume path in the product.

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = concat(aws_route_table.private[*].id, [aws_route_table.data.id])

  tags = merge(var.tags, { Name = "${local.name}-s3" })
}

resource "aws_security_group" "endpoints" {
  name        = "${local.name}-endpoints"
  description = "Interface VPC endpoints"
  vpc_id      = aws_vpc.this.id

  # The description is deliberately unchanged from before the extra groups
  # existed: an inline rule whose description changes is revoked and
  # re-authorised, and with the list empty this block must plan as no change.
  ingress {
    description     = "HTTPS from the application tier"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = concat([aws_security_group.ecs.id], var.endpoint_client_security_group_ids)
  }

  tags = merge(var.tags, { Name = "${local.name}-endpoints" })
}

resource "aws_vpc_endpoint" "interface" {
  for_each = toset(var.interface_endpoints)

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = merge(var.tags, { Name = "${local.name}-${each.value}" })
}

# ── Security groups ──────────────────────────────────────────────────────────

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public load balancer"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTPS from the internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Port 80 exists ONLY to redirect. The listener returns a 301 and never
  # forwards, so nothing is served in the clear.
  ingress {
    description = "HTTP, redirected to HTTPS"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "To the application tier"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.cidr_block]
  }

  tags = merge(var.tags, { Name = "${local.name}-alb" })
}

resource "aws_security_group" "ecs" {
  name        = "${local.name}-ecs"
  description = "ECS Fargate tasks"
  vpc_id      = aws_vpc.this.id

  # NO INBOUND RULE HERE. It is a separate resource below, because a rule
  # referencing `aws_security_group.alb` from inside this block and an ALB rule
  # referencing this group would be a cycle Terraform cannot resolve.

  egress {
    description = "Outbound: the model provider, the vendor APIs, SMTP"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${local.name}-ecs" })
}

# ONE RULE PER LISTENING PORT, never a range. `for_each` over the ports rather
# than `from_port = 3000, to_port = 8000`, because a range also opens the 4999
# ports in between -- and nothing in this VPC has decided anything about those.
resource "aws_vpc_security_group_ingress_rule" "ecs_from_alb" {
  for_each = toset([for port in var.application_ports : tostring(port)])

  security_group_id            = aws_security_group.ecs.id
  description                  = "Application port ${each.value}, from the load balancer only"
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.alb.id
}

# TASK TO TASK, on the enumerated internal ports only. The analysis service
# has no target group and no public path; the api and worker reach it by its
# Cloud Map name inside the VPC, and this rule is the only thing that lets that
# packet through. A group referencing ITSELF is deliberately narrow: it admits
# the tasks that carry this group and nothing else in the address range.
resource "aws_vpc_security_group_ingress_rule" "ecs_internal" {
  for_each = toset([for port in var.internal_service_ports : tostring(port)])

  security_group_id            = aws_security_group.ecs.id
  description                  = "Internal port ${each.value}, from other tasks in this group only"
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
  ip_protocol                  = "tcp"
  referenced_security_group_id = aws_security_group.ecs.id
}

resource "aws_security_group" "rds" {
  name        = "${local.name}-rds"
  description = "PostgreSQL"
  vpc_id      = aws_vpc.this.id

  # A GROUP REFERENCE, NOT A CIDR. "The thing that runs the application", not
  # "anything that happens to be in this address range" -- which would include
  # whatever gets deployed into this VPC next year.
  ingress {
    description     = "PostgreSQL from the application tier"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }

  # NO EGRESS RULE AT ALL. A database does not initiate connections.  State it
  # as an explicit empty rule set: an inline block with an empty CIDR is
  # normalised away by EC2 and otherwise produces permanent Terraform drift.
  egress = []

  tags = merge(var.tags, { Name = "${local.name}-rds" })
}

resource "aws_security_group" "redis" {
  name        = "${local.name}-redis"
  description = "ElastiCache Redis"
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "Redis from the application tier"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.ecs.id]
  }

  # Explicitly empty for the same reason as RDS above.
  egress = []

  tags = merge(var.tags, { Name = "${local.name}-redis" })
}
