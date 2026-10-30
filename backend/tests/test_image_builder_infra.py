"""The native arm64 image builder: its IAM scope, its buildspec and its script.

Read from the source, the way `test_code_sandbox_infra.py` reads that module,
because none of this can be observed from an account the builder has never
run in. Each assertion names a property the module docstring or the script
header claims; a claim with no assertion is the "true in prose only" failure
this repository has recorded more than once.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
MODULE = REPO / "infra" / "modules" / "image_builder"
MAIN = (MODULE / "main.tf").read_text(encoding="utf-8")
BUILDSPEC_TEXT = (MODULE / "buildspec.yml").read_text(encoding="utf-8")
PILOT = REPO / "infra" / "environments" / "pilot"
SCRIPT = REPO / "scripts" / "build-images-remote.sh"
SCRIPT_TEXT = SCRIPT.read_text(encoding="utf-8")
VERIFY_TEXT = (REPO / "scripts" / "verify-deployment.sh").read_text(encoding="utf-8")

FIREBASE_NAMES = [
    f"NEXT_PUBLIC_FIREBASE_{name}"
    for name in ("API_KEY", "AUTH_DOMAIN", "PROJECT_ID", "STORAGE_BUCKET", "MESSAGING_SENDER_ID", "APP_ID")
]


def _code(text: str) -> str:
    """Terraform with comments removed, so prose cannot satisfy an assertion."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith(("#", "//")))


CODE = _code(MAIN)


def norm(text: str) -> str:
    """Whitespace collapsed, so `terraform fmt` alignment cannot break an assertion."""
    return re.sub(r"\s+", " ", text)


def block(kind: str, type_: str, name: str, source: str = CODE) -> str:
    """The body of `<kind> "<type>" "<name>" { ... }`, by brace counting."""
    match = re.search(rf'{kind}\s+"{re.escape(type_)}"\s+"{re.escape(name)}"\s*\{{', source)
    assert match, f'{kind} "{type_}" "{name}" is not declared'
    depth = 0
    for index in range(match.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.end() : index]
    raise AssertionError(f"unbalanced braces in {type_}.{name}")


def statements(policy: str) -> list[str]:
    """Every `statement { ... }` body in one policy document."""
    found = []
    for match in re.finditer(r"\bstatement\s*\{", policy):
        depth = 0
        for index in range(match.end() - 1, len(policy)):
            if policy[index] == "{":
                depth += 1
            elif policy[index] == "}":
                depth -= 1
                if depth == 0:
                    found.append(policy[match.end() : index])
                    break
    return found


def listed(body: str, field: str) -> list[str]:
    match = re.search(rf"\b{field}\s*=\s*\[([^\]]*)\]", body, re.DOTALL)
    return re.findall(r'"([^"]+)"', match.group(1)) if match else []


# ── The role ─────────────────────────────────────────────────────────────────

BUILD_POLICY = block("data", "aws_iam_policy_document", "build")
BUILD_STATEMENTS = statements(BUILD_POLICY)


def test_the_role_grants_exactly_these_actions_and_nothing_else() -> None:
    granted = sorted(action for body in BUILD_STATEMENTS for action in listed(body, "actions"))
    assert granted == sorted(
        [
            "ecr:GetAuthorizationToken",
            "ecr:BatchCheckLayerAvailability",
            "ecr:BatchGetImage",
            "ecr:GetDownloadUrlForLayer",
            "ecr:InitiateLayerUpload",
            "ecr:UploadLayerPart",
            "ecr:CompleteLayerUpload",
            "ecr:PutImage",
            "s3:GetObject",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
            "secretsmanager:GetSecretValue",
            "kms:Decrypt",
            "kms:Decrypt",
            "kms:Decrypt",
            "kms:GenerateDataKey",
        ]
    )
    assert all('"Deny"' not in body for body in BUILD_STATEMENTS)


def _statement(sid: str) -> str:
    matches = [norm(body) for body in BUILD_STATEMENTS if re.search(rf'sid\s*=\s*"{sid}"', body)]
    assert len(matches) == 1, sid
    return matches[0]


def test_only_the_registry_login_names_every_resource() -> None:
    wildcard = [body for body in BUILD_STATEMENTS if 'resources = ["*"]' in norm(body)]
    assert len(wildcard) == 1
    assert listed(wildcard[0], "actions") == ["ecr:GetAuthorizationToken"]


def test_the_push_grant_is_the_three_product_repositories_by_arn() -> None:
    push = _statement("PushAndPullTheThreeProductRepositoriesOnly")
    assert "resources = [for name in local.repositories : var.repository_arns[name]]" in push
    assert re.search(r'repositories\s*=\s*\["backend", "frontend", "analysis"\]', CODE)
    variables = (MODULE / "variables.tf").read_text(encoding="utf-8")
    body = variables[variables.index('variable "repository_arns"') :]
    body = body[: body.index("\n}") + 2]
    assert "length(var.repository_arns) == 3" in body, "a fourth repository must not widen the grant silently"


def test_the_source_read_is_get_object_under_builds_in_its_own_bucket() -> None:
    read = _statement("ReadSourceArchivesUnderBuildsOnly")
    assert listed(read, "actions") == ["s3:GetObject"]
    assert 'resources = ["${aws_s3_bucket.source.arn}/${local.source_prefix}*"]' in read
    assert re.search(r'source_prefix\s*=\s*"builds/"', CODE)
    assert "module.s3" not in CODE and "storage_bucket" not in CODE


def test_logs_go_to_its_own_group_and_the_secret_is_the_one_token() -> None:
    assert 'resources = ["${aws_cloudwatch_log_group.build.arn}:*"]' in _statement("WriteThisBuildsLogGroupOnly")
    assert "resources = [var.huggingface_token_secret_arn]" in _statement("ReadTheModelDownloadTokenOnly")


def test_every_decrypt_is_conditioned_on_the_service_it_passes_through() -> None:
    kms = [norm(body) for body in BUILD_STATEMENTS if any(a.startswith("kms:") for a in listed(body, "actions"))]
    assert len(kms) == 3
    for body in kms:
        assert 'variable = "kms:ViaService"' in body
    source = _statement("DecryptBuildArchivesThroughS3Only")
    assert listed(source, "actions") == ["kms:Decrypt"]
    assert "resources = [aws_kms_key.source.arn]" in source
    assert 'values = ["s3.${var.region}.amazonaws.com"]' in source
    for body in kms:
        if body != source:
            assert "resources = [var.kms_key_arn]" in body
    token = _statement("DecryptThatTokenThroughSecretsManagerOnly")
    assert 'variable = "kms:EncryptionContext:SecretARN"' in token
    assert "values = [var.huggingface_token_secret_arn]" in token


def test_the_role_holds_no_deploy_or_escalation_permission() -> None:
    for forbidden in ("iam:", "ecs:", "lambda:", "sts:", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject", "ecr:*"):
        assert forbidden not in BUILD_POLICY, forbidden


def test_only_codebuild_for_this_account_and_this_project_may_assume_the_role() -> None:
    trust = norm(block("data", "aws_iam_policy_document", "assume"))
    assert 'identifiers = ["codebuild.amazonaws.com"]' in trust
    assert 'variable = "aws:SourceAccount" values = [var.account_id]' in trust
    assert 'variable = "aws:SourceArn" values = [local.project_arn]' in trust
    assert 'project_arn = "arn:aws:codebuild:${var.region}:${var.account_id}:project/${local.name}"' in CODE


# ── The project ──────────────────────────────────────────────────────────────

PROJECT = block("resource", "aws_codebuild_project", "this")
PROJECT_NORM = norm(PROJECT)


@pytest.mark.parametrize(
    "setting",
    [
        'type = "ARM_CONTAINER"',
        "compute_type = var.compute_type",
        "image = var.build_image",
        "privileged_mode = true",
        'type = "NO_ARTIFACTS"',
        "concurrent_build_limit = 1",
        'type = "S3"',
        'buildspec = file("${path.module}/buildspec.yml")',
        "group_name = aws_cloudwatch_log_group.build.name",
    ],
)
def test_the_project_is_a_native_arm_privileged_build_with_the_module_buildspec(setting: str) -> None:
    assert setting in PROJECT_NORM


def test_the_image_and_compute_defaults_are_the_reviewed_ones() -> None:
    variables = (MODULE / "variables.tf").read_text(encoding="utf-8")
    assert 'default = "aws/codebuild/amazonlinux2-aarch64-standard:3.0"' in norm(variables)
    assert 'default = "BUILD_GENERAL1_LARGE"' in norm(variables)


def test_privileged_mode_exists_only_in_the_image_builder() -> None:
    """The Docker daemon needs it here. Nothing else in the platform may ask."""
    for path in (REPO / "infra").rglob("*.tf"):
        if ".terraform" in path.parts or "offline-plan" in str(path):
            continue
        if re.search(r"privileged_mode\s*=\s*true", _code(path.read_text(encoding="utf-8"))):
            assert path.parent == MODULE, path


def test_the_project_names_its_variables_and_never_a_value() -> None:
    names = re.findall(r'name\s*=\s*"([A-Z_]+)"', PROJECT)
    assert sorted(names) == sorted(
        [
            "REGISTRY",
            "BACKEND_REPOSITORY_URI",
            "FRONTEND_REPOSITORY_URI",
            "ANALYSIS_REPOSITORY_URI",
            "SOURCE_BUCKET",
            "HUGGINGFACE_TOKEN_SECRET_ARN",
        ]
    )
    assert "SECRETS_MANAGER" not in PROJECT, "the token resolves only when a build asks for the analysis image"


def test_no_firebase_value_is_in_terraform() -> None:
    for path in (REPO / "infra").rglob("*.tf*"):
        if ".terraform" in path.parts:
            continue
        assert "NEXT_PUBLIC_FIREBASE" not in path.read_text(encoding="utf-8"), path


# ── The bucket and the log group ─────────────────────────────────────────────


def test_the_bucket_is_private_versioned_and_expires_in_fourteen_days() -> None:
    access = block("resource", "aws_s3_bucket_public_access_block", "source")
    for flag in ("block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"):
        assert re.search(rf"{flag}\s*=\s*true", access), flag
    assert 'status = "Enabled"' in block("resource", "aws_s3_bucket_versioning", "source")
    lifecycle = block("resource", "aws_s3_bucket_lifecycle_configuration", "source")
    assert "days = var.source_retention_days" in lifecycle
    assert "noncurrent_version_expiration" in lifecycle
    variables = (MODULE / "variables.tf").read_text(encoding="utf-8")
    body = variables[variables.index('variable "source_retention_days"') :]
    assert re.search(r"default\s*=\s*14\b", body[: body.index("\n}") + 2])
    tls = norm(block("data", "aws_iam_policy_document", "source_bucket"))
    assert 'effect = "Deny"' in tls and 'variable = "aws:SecureTransport"' in tls


def test_the_log_group_has_a_retention_and_the_environment_key() -> None:
    group = norm(block("resource", "aws_cloudwatch_log_group", "build"))
    assert "retention_in_days = var.log_retention_days" in group
    assert "kms_key_id = var.kms_key_arn" in group


# ── The buildspec ────────────────────────────────────────────────────────────

BUILDSPEC = yaml.safe_load(BUILDSPEC_TEXT)
BUILD_COMMANDS = "\n".join(BUILDSPEC["phases"]["build"]["commands"])
ALL_COMMANDS = "\n".join(
    command for phase in BUILDSPEC["phases"].values() for command in phase["commands"]
)


def _buildx_invocations() -> list[str]:
    joined = BUILD_COMMANDS.replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines() if "docker buildx build" in line]


def test_the_buildspec_is_valid_and_runs_under_bash() -> None:
    assert str(BUILDSPEC["version"]) == "0.2"
    assert BUILDSPEC["env"]["shell"] == "bash"
    for phase in BUILDSPEC["phases"].values():
        for command in phase["commands"]:
            assert command.lstrip().startswith("set -euo pipefail"), command[:60]


def test_every_image_is_a_single_docker_manifest_with_no_attestation() -> None:
    invocations = _buildx_invocations()
    assert len(invocations) == 3
    for line in invocations:
        assert "--platform linux/arm64" in line
        assert "--provenance=false" in line and "--sbom=false" in line
        assert "oci-mediatypes=false" in line and "push=true" in line
        assert "--load" not in line and " --push" not in line


def test_the_backend_is_one_push_under_the_tag_and_the_lambda_sibling() -> None:
    (backend,) = [line for line in _buildx_invocations() if line.endswith(" backend")]
    assert '\\"name=${BACKEND_REPOSITORY_URI}:${IMAGE_TAG},${BACKEND_REPOSITORY_URI}:${IMAGE_TAG}-fn\\"' in backend


def test_the_frontend_gets_the_six_firebase_args_from_the_environment_only() -> None:
    (frontend,) = [line for line in _buildx_invocations() if line.endswith(" frontend")]
    args = re.findall(r"--build-arg (\S+)", frontend)
    assert args == [*FIREBASE_NAMES, "NEXT_PUBLIC_DEMO_MODE"], (
        "a value on the command line would be in the build log"
    )
    assert "NEXT_PUBLIC_DEMO_MODE must be true or false." in ALL_COMMANDS
    dockerfile = (REPO / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    assert re.findall(r"^ARG (NEXT_PUBLIC_FIREBASE_\w+)", dockerfile, re.MULTILINE) == FIREBASE_NAMES
    assert re.search(r"^ARG NEXT_PUBLIC_DEMO_MODE=false$", dockerfile, re.MULTILINE)
    assert "--public-demo" in SCRIPT_TEXT
    assert "NEXT_PUBLIC_DEMO_MODE" in SCRIPT_TEXT


def test_the_analysis_token_is_a_buildkit_secret_never_a_build_arg() -> None:
    (analysis,) = [line for line in _buildx_invocations() if line.endswith(" analysis-service")]
    assert "--secret id=huggingface_token,env=HUGGINGFACE_TOKEN" in analysis
    assert "HUGGINGFACE" not in " ".join(re.findall(r"--build-arg \S+", analysis))


def test_the_buildspec_refuses_a_non_native_host_and_a_tag_that_names_another_commit() -> None:
    assert 'if [ "$arch" != "aarch64" ]' in ALL_COMMANDS
    assert 'if [ "${IMAGE_TAG:-}" != "sha-${SOURCE_COMMIT:0:12}" ]' in ALL_COMMANDS
    assert "^[0-9a-f]{40}$" in ALL_COMMANDS


# ── The script ───────────────────────────────────────────────────────────────


def test_the_script_prints_exactly_the_digest_names_verify_deployment_reads() -> None:
    read = set(re.findall(r"\$\{(EXPECTED_[A-Z]+_DIGEST):-\}", VERIFY_TEXT))
    printed = set(re.findall(r'^echo "export (EXPECTED_[A-Z]+_DIGEST)=', SCRIPT_TEXT, re.MULTILINE))
    assert read == printed == {"EXPECTED_BACKEND_DIGEST", "EXPECTED_FRONTEND_DIGEST", "EXPECTED_ANALYSIS_DIGEST"}


def test_the_script_tags_exactly_as_ci_does() -> None:
    assert 'TAG="sha-${COMMIT:0:12}"' in SCRIPT_TEXT
    workflow = (REPO / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    assert 'tag=sha-${GITHUB_SHA::12}' in workflow


def test_the_script_names_the_module_buildspec_and_the_module_project() -> None:
    assert 'BUILDSPEC_PATH="infra/modules/image_builder/buildspec.yml"' in SCRIPT_TEXT
    assert 'BUILDER="${PROJECT}-${ENVIRONMENT}-image-builder"' in SCRIPT_TEXT
    assert re.search(r'name\s*=\s*"\$\{var\.project\}-\$\{var\.environment\}-image-builder"', CODE)


def test_the_script_never_sources_the_env_file_and_stops_a_build_it_gives_up_on() -> None:
    code = "\n".join(line for line in SCRIPT_TEXT.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r'(^|\s)(\.|source)\s+"?\$\{?ENV_LOCAL', code)
    assert "aws codebuild stop-build" in code
    assert "git archive --format=zip" in code


def test_the_script_is_valid_bash() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not on PATH, so the script's syntax cannot be checked here")
    result = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


# ── The script's refusals, run for real against a throwaway repository ──────


@pytest.fixture
def sandbox(tmp_path: pathlib.Path) -> dict[str, object]:
    bash = shutil.which("bash")
    git = shutil.which("git")
    if bash is None or git is None:
        pytest.skip("bash and git are needed to run the script")

    # A fake `aws` that records every call. The refusals under test all happen
    # before the builder is contacted, so a codebuild call here is a failure.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "aws-calls.log"
    aws = fake_bin / "aws"
    aws.write_bytes(f'#!/usr/bin/env bash\necho "$*" >> "{calls.as_posix()}"\nexit 1\n'.encode())
    aws.chmod(0o755)

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "AWS_REGION": "xx-test-1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }

    def git_run(*args: str, cwd: pathlib.Path = work) -> None:
        subprocess.run([git, *args], cwd=cwd, env=env, check=True, capture_output=True, timeout=60)

    subprocess.run([git, "init", "--bare", "-b", "main", str(origin)], env=env, check=True, capture_output=True)
    subprocess.run([git, "clone", str(origin), str(work)], env=env, check=True, capture_output=True)
    git_run("symbolic-ref", "HEAD", "refs/heads/main")
    (work / "scripts").mkdir()
    shutil.copy(SCRIPT, work / "scripts" / "build-images-remote.sh")
    git_run("add", ".")
    git_run("commit", "-m", "base")
    git_run("push", "origin", "main")

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [bash, "scripts/build-images-remote.sh", *args],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    return {"run": run, "git": git_run, "work": work, "calls": calls}


def _codebuild_calls(sandbox: dict[str, object]) -> str:
    calls = sandbox["calls"]
    assert isinstance(calls, pathlib.Path)
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


def test_it_refuses_to_guess_where_the_analysis_digest_comes_from(sandbox: dict[str, object]) -> None:
    result = sandbox["run"]("pilot")  # type: ignore[operator]
    assert result.returncode == 2
    assert "say where the analysis digest comes from" in result.stderr


def test_it_refuses_a_dirty_tree(sandbox: dict[str, object]) -> None:
    work = sandbox["work"]
    assert isinstance(work, pathlib.Path)
    (work / "uncommitted.txt").write_text("not in any commit\n", encoding="utf-8")
    result = sandbox["run"]("pilot", "--analysis-tag", "sha-000000000000")  # type: ignore[operator]
    assert result.returncode == 1
    assert "the working tree is not clean" in result.stderr
    assert "codebuild" not in _codebuild_calls(sandbox)


def test_it_refuses_a_commit_not_on_origin_main(sandbox: dict[str, object]) -> None:
    git_run = sandbox["git"]
    work = sandbox["work"]
    assert isinstance(work, pathlib.Path)
    git_run("checkout", "-b", "feature")  # type: ignore[operator]
    (work / "feature.txt").write_text("unmerged\n", encoding="utf-8")
    git_run("add", ".")  # type: ignore[operator]
    git_run("commit", "-m", "feature")  # type: ignore[operator]
    result = sandbox["run"]("pilot", "--analysis-tag", "sha-000000000000")  # type: ignore[operator]
    assert result.returncode == 1
    assert "is not on origin/main" in result.stderr
    assert "codebuild" not in _codebuild_calls(sandbox)

    # The override gets past that refusal, and the next one still stands: a
    # commit with no buildspec predates the builder and is never started.
    result = sandbox["run"]("pilot", "--analysis-tag", "sha-000000000000", "--allow-non-main")  # type: ignore[operator]
    assert result.returncode == 1
    assert "predates the image builder" in result.stderr
    assert "codebuild" not in _codebuild_calls(sandbox)


# ── The pilot wiring ─────────────────────────────────────────────────────────


def test_the_pilot_wires_the_builder_behind_its_switch() -> None:
    pilot = _code((PILOT / "main.tf").read_text(encoding="utf-8"))
    call = pilot[pilot.index('module "image_builder"') :]
    call = norm(call[: call.index("\n}") + 2])
    assert "count = var.image_builder_enabled ? 1 : 0" in call
    assert "repository_arns = module.ecr.repository_arns" in call
    assert 'huggingface_token_secret_arn = module.secrets.secret_arns["HUGGINGFACE_TOKEN"]' in call
    # Purely additive: nothing outside the call reads the module except the output.
    assert pilot.count("module.image_builder") == 0
    outputs = (PILOT / "outputs.tf").read_text(encoding="utf-8")
    assert "module.image_builder[0].project_name" in outputs


def test_the_switch_defaults_on_and_the_offline_plan_exercises_the_module() -> None:
    variables = (PILOT / "variables.tf").read_text(encoding="utf-8")
    body = variables[variables.index('variable "image_builder_enabled"') :]
    assert re.search(r"default\s*=\s*true", body[: body.index("\n}") + 2])
    tfvars = (REPO / "infra" / "environments" / "offline-plan.tfvars").read_text(encoding="utf-8")
    assert re.search(r"^image_builder_enabled\s*=\s*true", tfvars, re.MULTILINE)
    assert re.search(r'^image_builder_bucket_name\s*=\s*"readypick-never-created-', tfvars, re.MULTILINE)


def test_the_buildspec_is_pinned_to_lf() -> None:
    """The script compares the applied buildspec with the commit's byte for byte."""
    attributes = (REPO / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^\*\.yml\s+text\s+eol=lf", attributes, re.MULTILINE)
