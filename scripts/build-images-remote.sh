#!/usr/bin/env bash
#
# Build and push the release images on the native arm64 CodeBuild builder,
# and print the digests `scripts/verify-deployment.sh` checks.
#
#   ./scripts/build-images-remote.sh pilot --analysis-tag sha-93ebfcb
#   ./scripts/build-images-remote.sh pilot --with-analysis
#   ./scripts/build-images-remote.sh pilot --commit <ref> --analysis-tag <tag>
#
# WHY THIS EXISTS
# ---------------
# The images run on arm64 Fargate and arm64 Lambda. Built on the operator's
# x86 laptop they went through QEMU, and a release spent about two hours there.
# `infra/modules/image_builder` is a CodeBuild project on a NATIVE arm64 host;
# this script is the only intended way to start it.
#
# WHAT IT BUILDS, AND FROM WHAT
# ------------------------------
# ONE EXACT COMMIT, never a working tree. It refuses a dirty tree (so nobody
# builds commit X believing their uncommitted edit is in it), and it refuses a
# commit that is not on origin/main unless --allow-non-main is passed, because
# main is the only deployable branch (owner ruling, 2026-09-25). The commit's
# `backend`, `frontend` and `analysis-service` directories are `git archive`d,
# uploaded under `builds/` in the builder's private bucket, and built there:
#
#   backend:sha-<12>, backend:sha-<12>-fn   one build pushed under two names
#   frontend:sha-<12>                       with the six NEXT_PUBLIC_FIREBASE_*
#                                           values from frontend/.env.local, and
#                                           NEXT_PUBLIC_DEMO_MODE (false unless
#                                           --public-demo is passed)
#   analysis:sha-<12>                       only with --with-analysis
#
# The tag is `sha-` plus the first twelve characters of the commit, exactly as
# every deploy and CI have always tagged. Tags are IMMUTABLE in ECR, so an
# image that already exists under the tag is REUSED and said to be, never
# rebuilt: a rebuild of the same commit could only fail at the push.
#
# The Firebase values are public web config, inlined into every browser bundle
# anyway. They are passed as build environment variables at build start and
# are deliberately in neither Terraform (state) nor the repository.
#
# WHAT IT PRINTS
# --------------
# On success, the three EXPECTED_*_DIGEST lines verify-deployment.sh reads,
# read back from ECR rather than from the build's own claims, plus the Lambda
# sibling URI for update-lambda-code.sh. The analysis digest is the image this
# run built (--with-analysis) or the one already under --analysis-tag, which
# is the tag the pilot's `analysis_image_tag` actually pins. One of the two is
# REQUIRED up front, because verify-deployment.sh refuses a missing digest and
# finding that out after the build is a wasted half hour.
#
# Every failure is loud: a refused precondition, a failed or timed-out build
# (which is STOPPED, so nothing pushes after the script has given up), and an
# image whose manifest is not a single Docker v2 manifest, which Lambda would
# refuse at update-lambda-code.sh time.
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: build-images-remote.sh <environment> (--with-analysis | --analysis-tag <tag>)
                              [--commit <ref>] [--allow-non-main] [--public-demo]
                              [--timeout-minutes <n>]
USAGE
  exit 2
}

ENVIRONMENT="${1:-}"
[ -n "$ENVIRONMENT" ] || usage
shift

COMMIT_REF="HEAD"
ALLOW_NON_MAIN=0
WITH_ANALYSIS=0
ANALYSIS_TAG=""
TIMEOUT_MINUTES=80
PUBLIC_DEMO=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --commit) [ "$#" -ge 2 ] || usage; COMMIT_REF="$2"; shift 2 ;;
    --allow-non-main) ALLOW_NON_MAIN=1; shift ;;
    --with-analysis) WITH_ANALYSIS=1; shift ;;
    --analysis-tag) [ "$#" -ge 2 ] || usage; ANALYSIS_TAG="$2"; shift 2 ;;
    --public-demo) PUBLIC_DEMO=true; shift ;;
    --timeout-minutes) [ "$#" -ge 2 ] || usage; TIMEOUT_MINUTES="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

if [ "$WITH_ANALYSIS" -eq 1 ] && [ -n "$ANALYSIS_TAG" ]; then
  echo "REFUSED: --with-analysis and --analysis-tag are alternatives. Pick one." >&2
  exit 2
fi
if [ "$WITH_ANALYSIS" -eq 0 ] && [ -z "$ANALYSIS_TAG" ]; then
  echo "REFUSED: say where the analysis digest comes from." >&2
  echo "  --with-analysis        build analysis-service at this commit too" >&2
  echo "  --analysis-tag <tag>   the tag the environment already pins (analysis_image_tag)" >&2
  exit 2
fi
if ! [[ "$TIMEOUT_MINUTES" =~ ^[1-9][0-9]*$ ]]; then
  echo "REFUSED: --timeout-minutes must be a positive whole number." >&2
  exit 2
fi

PROJECT="${PROJECT:-readypick}"
BUILDER="${PROJECT}-${ENVIRONMENT}-image-builder"
# THE REGION IS RESOLVED, NEVER ASSUMED, the same order and the same refusal
# as every other deploy script here (spec-doc6 D5): a default is a silent
# answer to a question only the operator can answer.
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
if [ -z "$REGION" ]; then
  echo "No AWS region. Set AWS_REGION (this project's pilot is ap-south-2)." >&2
  exit 2
fi

command -v aws >/dev/null 2>&1 || { echo "aws CLI is required." >&2; exit 127; }
command -v git >/dev/null 2>&1 || { echo "git is required." >&2; exit 127; }

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

# Git Bash on Windows runs the native aws.exe, which cannot open an MSYS path
# such as /tmp/x. Everything handed to aws as a FILE goes through this.
native_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

# ── The commit ───────────────────────────────────────────────────────────────

if [ -n "$(git status --porcelain)" ]; then
  echo "REFUSED: the working tree is not clean." >&2
  echo "  This script builds a COMMIT, never the tree, so an uncommitted edit or an" >&2
  echo "  untracked file would silently not be in the image. Commit or remove:" >&2
  git status --porcelain | head -20 | sed 's/^/    /' >&2
  exit 1
fi

COMMIT="$(git rev-parse --verify "${COMMIT_REF}^{commit}")"
TAG="sha-${COMMIT:0:12}"

if ! git fetch --quiet origin main; then
  echo "REFUSED: could not fetch origin/main, so whether ${COMMIT:0:12} is on it is unknown." >&2
  exit 1
fi
if git merge-base --is-ancestor "$COMMIT" origin/main; then
  ON_MAIN=1
else
  ON_MAIN=0
  if [ "$ALLOW_NON_MAIN" -ne 1 ]; then
    echo "REFUSED: ${COMMIT:0:12} is not on origin/main, and main is the only deployable" >&2
    echo "  branch. Pass --allow-non-main to build it anyway, for a test build that" >&2
    echo "  will not be deployed." >&2
    exit 1
  fi
fi

BUILDSPEC_PATH="infra/modules/image_builder/buildspec.yml"
if ! COMMIT_BUILDSPEC="$(git show "${COMMIT}:${BUILDSPEC_PATH}" 2>/dev/null)"; then
  echo "REFUSED: ${COMMIT:0:12} predates the image builder (${BUILDSPEC_PATH} is not in it)." >&2
  echo "  Build it with the local QEMU fallback in docs/operations/DEPLOY_AWS.md." >&2
  exit 1
fi

# ── The builder, discovered from the project itself ──────────────────────────

project_var() {
  aws codebuild batch-get-projects --names "$BUILDER" --region "$REGION" \
    --query "projects[0].environment.environmentVariables[?name=='$1'].value | [0]" \
    --output text | tr -d '\r'
}

if [ "$(aws codebuild batch-get-projects --names "$BUILDER" --region "$REGION" \
    --query 'length(projects)' --output text | tr -d '\r')" != "1" ]; then
  echo "REFUSED: no CodeBuild project ${BUILDER} in ${REGION}." >&2
  echo "  Apply infra/environments/${ENVIRONMENT} with image_builder_enabled = true first." >&2
  exit 1
fi

SOURCE_BUCKET="$(project_var SOURCE_BUCKET)"
BACKEND_URI="$(project_var BACKEND_REPOSITORY_URI)"
FRONTEND_URI="$(project_var FRONTEND_REPOSITORY_URI)"
ANALYSIS_URI="$(project_var ANALYSIS_REPOSITORY_URI)"
TOKEN_SECRET_ARN="$(project_var HUGGINGFACE_TOKEN_SECRET_ARN)"
for value in "$SOURCE_BUCKET" "$BACKEND_URI" "$FRONTEND_URI" "$ANALYSIS_URI" "$TOKEN_SECRET_ARN"; do
  if [ -z "$value" ] || [ "$value" = "None" ]; then
    echo "REFUSED: ${BUILDER} is missing an environment variable this script reads. Re-apply the module." >&2
    exit 1
  fi
done

# THE BUILDSPEC THAT RUNS IS THE APPLIED ONE, so it must be the one in the
# commit. Otherwise a buildspec edit that was never applied reads as having
# been built, and a stale one runs without anybody noticing.
APPLIED_BUILDSPEC="$(aws codebuild batch-get-projects --names "$BUILDER" --region "$REGION" \
  --query 'projects[0].source.buildspec' --output text | tr -d '\r')"
if [ "$APPLIED_BUILDSPEC" != "$(printf '%s' "$COMMIT_BUILDSPEC" | tr -d '\r')" ]; then
  echo "REFUSED: the buildspec applied to ${BUILDER} differs from ${BUILDSPEC_PATH} at ${COMMIT:0:12}." >&2
  echo "  terraform apply infra/environments/${ENVIRONMENT} from this commit, then run again." >&2
  exit 1
fi

# ── What is already in ECR ───────────────────────────────────────────────────

# Prints "<digest> <manifest media type>" for an existing tag, nothing for an
# absent one, and FAILS on any other error: a throttle or a permission problem
# must not read as "not built yet".
image_detail() {
  local uri="$1" tag="$2" out err
  err="$(mktemp)"
  if out="$(aws ecr describe-images --repository-name "${uri#*/}" --image-ids "imageTag=${tag}" \
      --region "$REGION" --query 'imageDetails[0].[imageDigest,imageManifestMediaType]' \
      --output text 2>"$err")"; then
    rm -f "$err"
    printf '%s' "$out" | tr -d '\r' | tr '\t' ' '
    return 0
  fi
  if grep -q 'ImageNotFoundException' "$err"; then
    rm -f "$err"
    return 0
  fi
  echo "describe-images failed for ${uri}:${tag}:" >&2
  cat "$err" >&2
  rm -f "$err"
  return 1
}

backend_main="$(image_detail "$BACKEND_URI" "$TAG")"
backend_fn="$(image_detail "$BACKEND_URI" "${TAG}-fn")"
frontend_existing="$(image_detail "$FRONTEND_URI" "$TAG")"

if [ -n "$backend_main" ] && [ -n "$backend_fn" ]; then
  BUILD_BACKEND=false
elif [ -z "$backend_main" ] && [ -z "$backend_fn" ]; then
  BUILD_BACKEND=true
else
  echo "REFUSED: exactly one of backend:${TAG} and backend:${TAG}-fn exists." >&2
  echo "  They are one build pushed under two names; with immutable tags the missing" >&2
  echo "  one cannot be pushed beside the other by this builder. Investigate first." >&2
  exit 1
fi
if [ -n "$frontend_existing" ]; then BUILD_FRONTEND=false; else BUILD_FRONTEND=true; fi
BUILD_ANALYSIS=false
if [ "$WITH_ANALYSIS" -eq 1 ]; then
  analysis_existing="$(image_detail "$ANALYSIS_URI" "$TAG")"
  if [ -n "$analysis_existing" ]; then
    echo "analysis:${TAG} is already in ECR; it is reused, not rebuilt."
  else
    BUILD_ANALYSIS=true
  fi
else
  analysis_existing="$(image_detail "$ANALYSIS_URI" "$ANALYSIS_TAG")"
  if [ -z "$analysis_existing" ]; then
    echo "REFUSED: analysis:${ANALYSIS_TAG} is not in ECR." >&2
    exit 1
  fi
fi
[ "$BUILD_BACKEND" = true ] || echo "backend:${TAG} and ${TAG}-fn are already in ECR; reused, not rebuilt."
[ "$BUILD_FRONTEND" = true ] || echo "frontend:${TAG} is already in ECR; reused, not rebuilt."

# ── The build ────────────────────────────────────────────────────────────────

if [ "$BUILD_BACKEND$BUILD_FRONTEND$BUILD_ANALYSIS" != "falsefalsefalse" ]; then
  WORK="$(mktemp -d)"
  trap 'rm -rf "$WORK"' EXIT

  overrides="{\"name\":\"SOURCE_COMMIT\",\"value\":\"${COMMIT}\",\"type\":\"PLAINTEXT\"}"
  overrides="${overrides},{\"name\":\"IMAGE_TAG\",\"value\":\"${TAG}\",\"type\":\"PLAINTEXT\"}"
  overrides="${overrides},{\"name\":\"BUILD_BACKEND\",\"value\":\"${BUILD_BACKEND}\",\"type\":\"PLAINTEXT\"}"
  overrides="${overrides},{\"name\":\"BUILD_FRONTEND\",\"value\":\"${BUILD_FRONTEND}\",\"type\":\"PLAINTEXT\"}"
  overrides="${overrides},{\"name\":\"BUILD_ANALYSIS\",\"value\":\"${BUILD_ANALYSIS}\",\"type\":\"PLAINTEXT\"}"

  if [ "$BUILD_FRONTEND" = true ]; then
    ENV_LOCAL="${ROOT}/frontend/.env.local"
    [ -f "$ENV_LOCAL" ] || { echo "REFUSED: ${ENV_LOCAL} is missing; the frontend needs its Firebase web config." >&2; exit 1; }
    for name in API_KEY AUTH_DOMAIN PROJECT_ID STORAGE_BUCKET MESSAGING_SENDER_ID APP_ID; do
      var="NEXT_PUBLIC_FIREBASE_${name}"
      # READ, never sourced: sourcing would execute whatever the file holds.
      line="$(grep -E "^(export[[:space:]]+)?${var}=" "$ENV_LOCAL" | tail -n 1 || true)"
      value="$(printf '%s' "${line#*=}" | tr -d '\r')"
      value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
      # Firebase web config is letters, digits and . _ : - and nothing else. A
      # value outside that is not Firebase config, and it is also the one
      # thing that could break out of the JSON built below.
      if ! [[ "$value" =~ ^[A-Za-z0-9._:-]+$ ]]; then
        echo "REFUSED: ${var} in frontend/.env.local is empty or not a Firebase web config value." >&2
        exit 1
      fi
      overrides="${overrides},{\"name\":\"${var}\",\"value\":\"${value}\",\"type\":\"PLAINTEXT\"}"
    done
    # true or false only. The buildspec refuses anything else, and an empty
    # value would override the image default of false.
    overrides="${overrides},{\"name\":\"NEXT_PUBLIC_DEMO_MODE\",\"value\":\"${PUBLIC_DEMO}\",\"type\":\"PLAINTEXT\"}"
  fi

  if [ "$BUILD_ANALYSIS" = true ]; then
    # The ARN, not the token. CodeBuild resolves it with the builder's role.
    overrides="${overrides},{\"name\":\"HUGGINGFACE_TOKEN\",\"value\":\"${TOKEN_SECRET_ARN}\",\"type\":\"SECRETS_MANAGER\"}"
  fi

  ARCHIVE="${WORK}/source.zip"
  git archive --format=zip -o "$ARCHIVE" "$COMMIT" backend frontend analysis-service
  KEY="builds/${COMMIT}-$(date -u +%Y%m%dT%H%M%SZ).zip"
  echo "==> uploading ${COMMIT:0:12} ($(wc -c <"$ARCHIVE" | tr -d ' ') bytes) to s3://${SOURCE_BUCKET}/${KEY}"
  aws s3 cp "$(native_path "$ARCHIVE")" "s3://${SOURCE_BUCKET}/${KEY}" --region "$REGION" --only-show-errors

  REQUEST="${WORK}/start-build.json"
  printf '{"projectName":"%s","sourceLocationOverride":"%s/%s","environmentVariablesOverride":[%s]}' \
    "$BUILDER" "$SOURCE_BUCKET" "$KEY" "$overrides" >"$REQUEST"

  BUILD_ID="$(aws codebuild start-build --cli-input-json "file://$(native_path "$REQUEST")" \
    --region "$REGION" --query 'build.id' --output text | tr -d '\r')"
  echo "==> started ${BUILD_ID}"
  echo "    backend=${BUILD_BACKEND} frontend=${BUILD_FRONTEND} analysis=${BUILD_ANALYSIS}"

  # Stopping is the only safe way to give up: a build the script has stopped
  # watching would still push, and its image would appear with nobody having
  # read whether it succeeded.
  stop_build() {
    echo "$1 Stopping ${BUILD_ID} so it cannot push later." >&2
    if ! aws codebuild stop-build --id "$BUILD_ID" --region "$REGION" \
        --query 'build.buildStatus' --output text >&2; then
      echo "COULD NOT STOP ${BUILD_ID}. It may still push; stop it in the console." >&2
    fi
  }

  deadline=$(( $(date +%s) + TIMEOUT_MINUTES * 60 ))
  last_phase=""
  poll_errors=0
  status=""
  while :; do
    # A throttled or dropped poll is not a build result. Five in a row is.
    if ! line="$(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" \
        --query 'builds[0].[buildStatus,currentPhase]' --output text | tr -d '\r')"; then
      poll_errors=$((poll_errors + 1))
      if [ "$poll_errors" -ge 5 ]; then
        stop_build "Could not read the build's status five times in a row."
        status="UNREADABLE"
        break
      fi
      sleep 20
      continue
    fi
    poll_errors=0
    status="${line%%$'\t'*}"
    phase="${line#*$'\t'}"
    if [ "$phase" != "$last_phase" ]; then
      echo "    $(date -u +%H:%M:%S) ${phase}"
      last_phase="$phase"
    fi
    [ "$status" = "IN_PROGRESS" ] || break
    if [ "$(date +%s)" -ge "$deadline" ]; then
      stop_build "TIMED OUT after ${TIMEOUT_MINUTES} minutes."
      status="STOPPED_BY_SCRIPT"
      break
    fi
    sleep 20
  done

  if [ "$status" != "SUCCEEDED" ]; then
    echo >&2
    echo "BUILD ${status}: ${BUILD_ID}" >&2
    aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" \
      --query 'builds[0].phases[?phaseStatus!=null && phaseStatus!=`SUCCEEDED`].[phaseType,phaseStatus,contexts[0].message]' \
      --output text >&2 || true
    read -r group stream < <(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" \
      --query 'builds[0].logs.[groupName,streamName]' --output text | tr -d '\r') || true
    if [ -n "${stream:-}" ] && [ "$stream" != "None" ]; then
      echo >&2
      echo "Last lines of ${group} ${stream}:" >&2
      aws logs get-log-events --log-group-name "$group" --log-stream-name "$stream" \
        --region "$REGION" --limit 60 --query 'events[].message' --output text >&2 || true
    fi
    echo >&2
    echo "Nothing from this run is deployable. Fix the cause and run again; images" >&2
    echo "this run DID push are reused next time, never rebuilt." >&2
    exit 1
  fi
fi

# ── Read back what is in ECR, and check it is what Lambda will accept ────────

DOCKER_V2="application/vnd.docker.distribution.manifest.v2+json"
failures=0

# Prints the digest. Returns non-zero for a missing image, or for an image this
# run built whose manifest is not a single Docker v2 manifest (what this
# builder produces and what Lambda requires). An image REUSED from an older
# build keeps whatever it was pushed as, so only this run's are held to it.
read_digest() {
  local uri="$1" tag="$2" built="$3" detail media
  detail="$(image_detail "$uri" "$tag")" || return 1
  if [ -z "$detail" ]; then
    echo "MISSING after the build: ${uri}:${tag}" >&2
    return 1
  fi
  printf '%s' "${detail%% *}"
  media="${detail#* }"
  if [ "$built" = true ] && [ "$media" != "$DOCKER_V2" ]; then
    echo "WRONG MANIFEST: ${uri}:${tag} is ${media}, not ${DOCKER_V2}." >&2
    return 1
  fi
}

if [ "$WITH_ANALYSIS" -eq 1 ]; then
  ANALYSIS_REF="$TAG"
else
  ANALYSIS_REF="$ANALYSIS_TAG"
fi
BACKEND_DIGEST="$(read_digest "$BACKEND_URI" "$TAG" "$BUILD_BACKEND")" || failures=$((failures + 1))
FN_DIGEST="$(read_digest "$BACKEND_URI" "${TAG}-fn" "$BUILD_BACKEND")" || failures=$((failures + 1))
FRONTEND_DIGEST="$(read_digest "$FRONTEND_URI" "$TAG" "$BUILD_FRONTEND")" || failures=$((failures + 1))
ANALYSIS_DIGEST="$(read_digest "$ANALYSIS_URI" "$ANALYSIS_REF" "$BUILD_ANALYSIS")" || failures=$((failures + 1))

# THE LAMBDA SIBLING IS THE SAME BYTES. The Lambda manifest is the one that must
# be Docker v2 (always checked, reused or not), and a sibling that differs from
# the ECS image means the functions and the tasks run different code.
fn_media="$(image_detail "$BACKEND_URI" "${TAG}-fn")"
fn_media="${fn_media#* }"
if [ "$fn_media" != "$DOCKER_V2" ]; then
  echo "WRONG MANIFEST: backend:${TAG}-fn is ${fn_media}; Lambda refuses anything but ${DOCKER_V2}." >&2
  failures=$((failures + 1))
fi
if [ "$BUILD_BACKEND" = true ] && [ "$BACKEND_DIGEST" != "$FN_DIGEST" ]; then
  echo "MISMATCH: backend:${TAG} is ${BACKEND_DIGEST} and ${TAG}-fn is ${FN_DIGEST}; one build must be one digest." >&2
  failures=$((failures + 1))
fi

if [ "$failures" -gt 0 ]; then
  echo "${failures} problem(s) with what is in ECR. Do not deploy these images." >&2
  exit 1
fi

echo
echo "Built from ${COMMIT} ($([ "$ON_MAIN" -eq 1 ] && echo "on origin/main" || echo "NOT on origin/main: not deployable"))."
echo
echo "IMAGE_TAG=${TAG}"
echo "ANALYSIS_IMAGE_TAG=${ANALYSIS_REF}"
echo "LAMBDA_IMAGE_URI=${BACKEND_URI}:${TAG}-fn"
echo
echo "export EXPECTED_BACKEND_DIGEST=${BACKEND_DIGEST}"
echo "export EXPECTED_FRONTEND_DIGEST=${FRONTEND_DIGEST}"
echo "export EXPECTED_ANALYSIS_DIGEST=${ANALYSIS_DIGEST}"
