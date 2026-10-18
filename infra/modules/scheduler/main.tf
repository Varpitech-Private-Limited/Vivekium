# EventBridge Scheduler: the periodic sweeps that used to be Celery beat.
#
# WHY THIS REPLACED A PROCESS
# ---------------------------
# Beat was a container that had to be running, had to be exactly one, and whose
# failure mode was silence. Nothing fires, nothing errors, and the only symptom
# is a reconciliation sweep that quietly stopped repairing things -- which for
# this product means jobs stuck at `questions_pending_review` with no framework,
# abandoned assessments never settled, and temporary project originals never
# deleted. A managed scheduler has no process to lose and no singleton to
# guarantee.
#
# EventBridge SCHEDULER rather than an EventBridge RULE. A rule is a bus
# subscription with a target; a schedule is a first-class object with its own
# retry policy, its own flexible time window and a real one-time mode. The
# retry policy is the one that matters here: it is set to ZERO attempts, for
# the same reason the Lambda async config is, so the retry loop inside
# `app.workers.runtime` stays the only owner of retries.
#
# THE LIST IS NOT AUTHORED HERE
# -----------------------------
# `app/workers/schedule.py` is the source of truth, and
# `backend/tests/test_schedule_parity.py` reads both this file and that module
# and fails if they disagree. Two copies of one fact stay honest only when
# something compares them, which is the discipline `test_runbook_parity.py`
# already applies to the hiring weights. Adding a schedule means editing both,
# and the test is what makes that a rule rather than a habit.

locals {
  name = "${var.project}-${var.environment}"
}

data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    # Without this the role can be assumed by ANY account's scheduler that
    # learns its ARN, which is the confused-deputy shape AWS documents for
    # every service principal that assumes on a caller's behalf.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = "${local.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

# One target function, named. A scheduler role that could invoke any function
# in the account would be able to run every agent on a timer.
data "aws_iam_policy_document" "invoke" {
  statement {
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [var.target_function_arn, "${var.target_function_arn}:*"]
  }
}

resource "aws_iam_role_policy" "invoke" {
  name   = "invoke-task-worker"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.invoke.json
}

resource "aws_scheduler_schedule" "this" {
  for_each = var.schedules

  name = each.key
  # Left in the default group deliberately: a custom group is another object to
  # keep in step with the parity test for no isolation this deployment needs.
  group_name = "default"

  schedule_expression          = each.value.rate_expression
  schedule_expression_timezone = var.timezone

  # OFF. A flexible window spreads invocations to smooth load, which is right
  # for hundreds of schedules and wrong for seven: it would make "every five
  # minutes" mean "somewhere in the next fifteen", and the dashboard refresh
  # would then be stale by an amount nobody could predict.
  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = var.target_function_arn
    role_arn = aws_iam_role.this.arn

    # The dispatch payload, byte for byte what `dispatch.payload_for` builds.
    # A sweep is an ordinary task and arrives through the ordinary door: giving
    # the scheduler its own entry point would let it fire something the task
    # registry does not have, which is exactly how a beat entry outlived the
    # module it called.
    #
    # `run_id` is empty because nothing polls a sweep. The runtime skips the
    # status write on an empty id rather than writing a record no screen reads.
    input = jsonencode({
      run_id = ""
      task   = each.value.task
      args   = []
      kwargs = {}
    })

    retry_policy {
      # See the module docstring. One owner of retry, and it is the runtime.
      maximum_retry_attempts = 0
    }
  }

  state = var.enabled ? "ENABLED" : "DISABLED"

  lifecycle {
    # Solo-test sleep mode disables application sweeps while PostgreSQL is
    # stopped and re-enables them only after wake has passed health checks.
    # Terraform continues to own every schedule definition, but an ordinary
    # deploy must not turn background work back on against a sleeping database.
    ignore_changes = [state]
  }
}
