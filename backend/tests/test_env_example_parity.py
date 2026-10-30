"""`.env.example` and `Settings` describe the same configuration surface.

WHY THIS EXISTS
---------------
`.env.example` is the one place an operator learns what a deployment can set,
and it had drifted from `app/core/config.Settings` in BOTH directions. Keys
the product reads were missing (`INBOUND_EMAIL_DOMAIN`,
`INBOUND_WEBHOOK_SECRET`, `POSTGRES_MIGRATION_ROLE`, `COOKIE_DOMAIN`,
`SES_CONFIGURATION_SET`, the four `TRANSCRIBE_*`), several of which the pilot
Terraform sets, so a laptop could not reproduce the deployed configuration
without reading the Python. And keys the product no longer read were still
there (`SENDER_OTP_*` two weeks after the mailbox code went, the Cloud Run
block a platform after Cloud Run went, the SMS vendor keys and `OTP_*` with no reader),
each one a setting an operator could tune expecting an effect.

WHAT IS PINNED
--------------
* Every `Settings` field is documented in `.env.example` (as `KEY=value`, or
  as `# KEY=value` when leaving it unset is the point), OR it is declared in
  `INTERNAL_ONLY` under a written reason. Nothing is exempt by prefix: a
  new field is a decision, and this file is where it is recorded.
* Every key in `.env.example` is a `Settings` field, or a key declared in
  `EXTERNAL` with the process that reads it.
* The declarations are a RATCHET: a name in `INTERNAL_ONLY` that is no longer
  a field, or that is also documented, fails; so does an `EXTERNAL` key the
  file no longer carries or that has become a field.
* Every `Settings` field a Terraform environment root SETS is documented,
  never internal. A value a deployment actually sets is deployment data by
  definition, and hiding it in the internal list is how a laptop stops
  resembling the pilot.
"""
from __future__ import annotations

import pathlib
import re

from app.core.config import Settings

ROOT = pathlib.Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"
TERRAFORM_ROOTS = tuple(
    ROOT / "infra" / "environments" / env / "main.tf" for env in ("pilot", "staging", "production")
)

#: Keys `.env.example` documents that are not `Settings` fields, and who reads
#: each one. Nothing else may appear in the file.
EXTERNAL: dict[str, str] = {
    "HUGGINGFACE_TOKEN": "read by the analysis service (analysis-service/app/config.py), never the backend",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "read by the OpenTelemetry SDK and services/observability/otel.py from os.environ",
    "OTEL_SERVICE_NAME": "read by the OpenTelemetry SDK",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "read by third-party GenAI instrumentation",
    "BACKEND_INTERNAL_URL": "read by the frontend's same-origin API proxy, per request",
    "NEXT_PUBLIC_DEMO_MODE": "frontend submit handlers, not a Settings field",
}

#: `Settings` fields deliberately NOT in `.env.example`, grouped by the reason.
#: Every name is listed explicitly; a prefix rule would admit the next field of
#: that prefix without anybody deciding it, which is the drift this file exists
#: to stop.
INTERNAL_ONLY: dict[str, frozenset[str]] = {
    # The browser is served these from services/proctoring/config.py, so the
    # client and the server never disagree. A per-deployment value would make
    # two environments monitor candidates differently; each moves in a
    # reviewed change to config.py, never in an environment file.
    "proctoring thresholds, served to the browser from one definition": frozenset({
        "PROCTORING_AI_TEXT_MIN_CHARS", "PROCTORING_AI_TEXT_THRESHOLD",
        "PROCTORING_ANALYSIS_TIMEOUT_SECONDS", "PROCTORING_AUDIO_CHUNK_SECONDS",
        "PROCTORING_AUDIO_MAX_CHUNK_BYTES", "PROCTORING_BASELINE_ANSWERS",
        "PROCTORING_BURST_WINDOW_SECONDS",
        "PROCTORING_CONFIRMING_WINDOW_SECONDS", "PROCTORING_DISPLAY_CHECK_INTERVAL_SECONDS",
        "PROCTORING_EVENT_BATCH_MAX", "PROCTORING_FACE_ABSENT_EXTENDED_SECONDS",
        "PROCTORING_FACE_ABSENT_MODERATE_COOLDOWN_SECONDS", "PROCTORING_FACE_ABSENT_MODERATE_SECONDS",
        "PROCTORING_FACE_DISTANCE_THRESHOLD", "PROCTORING_FAST_ENTRY_MULTIPLIER",
        "PROCTORING_FAST_ENTRY_SUSTAINED_SECONDS", "PROCTORING_FOCUS_LOSS_IGNORE_UNDER_SECONDS",
        "PROCTORING_HEARTBEAT_GAP_SECONDS", "PROCTORING_HEARTBEAT_INTERVAL_SECONDS",
        "PROCTORING_IDENTITY_CHECK_INTERVAL_SECONDS", "PROCTORING_IDENTITY_CONSECUTIVE_MISMATCHES",
        "PROCTORING_INTEGRITY_FAILURE_TERMINATION_SECONDS", "PROCTORING_LOW_LIGHT_COOLDOWN_SECONDS",
        "PROCTORING_LOW_LIGHT_LUMINANCE_THRESHOLD", "PROCTORING_LOW_RATIO_MIN_LENGTH",
        "PROCTORING_LOW_RATIO_THRESHOLD", "PROCTORING_MAX_KEYSTROKE_SAMPLES",
        "PROCTORING_MAX_WARNINGS", "PROCTORING_MOUSE_SAMPLE_HZ",
        "PROCTORING_OBJECT_CONFIDENCE_THRESHOLD", "PROCTORING_OBJECT_CONSECUTIVE_FRAMES",
        "PROCTORING_OBJECT_COOLDOWN_SECONDS", "PROCTORING_OBSTRUCTION_SECONDS",
        "PROCTORING_OBSTRUCTION_VARIANCE_THRESHOLD", "PROCTORING_PAUSE_GAP_SECONDS",
        "PROCTORING_SAMPLING_FPS_CONFIRMING", "PROCTORING_SAMPLING_FPS_DEGRADED",
        "PROCTORING_SAMPLING_FPS_NORMAL", "PROCTORING_SECOND_PERSON_COOLDOWN_SECONDS",
        "PROCTORING_SECOND_VOICE_CONSECUTIVE_CHUNKS", "PROCTORING_UNIFORM_MAX_CORRECTIONS",
        "PROCTORING_UNIFORM_MAX_PAUSE_SECONDS", "PROCTORING_UNIFORM_SPAN_CHARS",
    }),
    # What a candidate is asked, for how long and at what weight. A value that
    # differed between deployments would make two candidates for one job sit
    # different assessments depending on where they were served.
    # The turn clock itself (ASSESSMENT_TIME_PROSE_SECONDS and its siblings) is
    # documented in `.env.example` by the Phase 3 conversation engine, which
    # snapshots each allocation onto the turn it opens.
    "the assessment's shape: composition, timing, weights and ceilings": frozenset({
        "ASSESSMENT_ANCHOR_MIN_CHARS", "ASSESSMENT_COMPOSITION_ATTEMPTS",
        "ASSESSMENT_EVALUATION_MIN_REASONING_WORDS",
        "ASSESSMENT_MISCONCEPTION_MIN_WORDS",
        "ASSESSMENT_QUESTION_FLOOR_CXO", "ASSESSMENT_QUESTION_FLOOR_LEADERSHIP",
        "ASSESSMENT_QUESTION_FLOOR_MANAGERIAL", "ASSESSMENT_QUESTION_FLOOR_NON_MANAGERIAL",
        "ASSESSMENT_SHARE_CODING", "ASSESSMENT_SHARE_OBJECTIVE", "ASSESSMENT_SHARE_PROSE",
        "ASSESSMENT_WEIGHT_CODING", "ASSESSMENT_WEIGHT_EVIDENCE",
        "ASSESSMENT_WEIGHT_FILL_BLANK", "ASSESSMENT_WEIGHT_MCQ_MULTI",
        "ASSESSMENT_WEIGHT_MCQ_SINGLE", "ASSESSMENT_WEIGHT_SHORT_ANSWER",
    }),
    # The sentence a candidate consents to and the version recorded beside it.
    # An environment override would detach the wording served from the version
    # stored in `candidate_consent_events`, which is the provenance the consent
    # record exists to keep.
    "consent wording and its recorded versions": frozenset({
        "ASSESSMENT_CONSENT_TEXT", "ASSESSMENT_CONSENT_VERSION", "ASSESSMENT_PRIVACY_POLICY_VERSION",
        "ASSESSMENT_TERMS_VERSION",
    }),
    # Windows promised to candidates. Each moves with an owner decision in a
    # reviewed change, never as a deployment's private setting.
    "retention and consent windows promised to candidates": frozenset({
        "ASSESSMENT_MEDIA_RETENTION_DAYS", "PROCTORING_EVENT_RETENTION_DAYS",
        "CONSENT_GRACE_DAYS", "CONSENT_INACTIVITY_MONTHS", "CONSENT_RENEWAL_MONTHS",
        "VERIFICATION_LINK_TTL_DAYS",
    }),
    # Hostile-input ceilings. `.env.example` already says the PROJECT_* limits
    # have safe defaults and need no entries; the others are the same kind.
    "ceilings on candidate-supplied files and media": frozenset({
        "PROJECT_MAX_AI_CONTEXT_CHARS", "PROJECT_MAX_ARCHIVE_DEPTH",
        "PROJECT_MAX_ARCHIVE_ENTRIES", "PROJECT_MAX_COMPRESSION_RATIO",
        "PROJECT_MAX_EVIDENCE_UNITS", "PROJECT_MAX_EXTRACTED_BYTES",
        "PROJECT_MAX_FILES", "PROJECT_MAX_FILE_BYTES",
        "PROJECT_MAX_PROJECTS_PER_CANDIDATE", "PROJECT_MAX_TEXT_CHARS_PER_FILE",
        "PROJECT_MAX_TOTAL_BYTES", "PROJECT_REPO_MAX_FILES", "PROJECT_REPO_MAX_FILE_BYTES",
        "BGV_DOCUMENT_MAX_BYTES", "BGV_DOCUMENTS_MAX_PER_TYPE",
        # VIDEO_MAX_UPLOAD_BYTES and VIDEO_MAX_DURATION_SECONDS are documented
        # with the rest of the segmented recording's limits in `.env.example`.
    }),
    # The recording pipeline's own knobs, sized against the Fargate task that
    # runs it rather than against anything a deployment chooses.
    "the video pipeline's processing and link lifetimes": frozenset({
        "VIDEO_COMPRESSION_AUDIO_BITRATE_KBPS", "VIDEO_COMPRESSION_CRF",
        "VIDEO_COMPRESSION_PRESET", "VIDEO_DOWNLOAD_URL_TTL_SECONDS",
        "VIDEO_DURATION_TOLERANCE_SECONDS", "VIDEO_FFMPEG_TIMEOUT_SECONDS",
        "VIDEO_PREVIEW_URL_TTL_SECONDS",
    }),
    # Sized against the RDS instance class and the ECS autoscaler ceiling; the
    # arithmetic is written beside the fields in config.py and moves with the
    # instance, not per environment file.
    "database pool sizing tied to the instance class": frozenset({
        "DB_MAX_OVERFLOW", "DB_POOL_RECYCLE_SECONDS", "DB_POOL_SIZE", "DB_POOL_TIMEOUT_SECONDS",
    }),
    # pgvector index tuning and the reconciliation sweep's batch.
    "retrieval index tuning": frozenset({
        "RETRIEVAL_HNSW_EF_SEARCH", "RETRIEVAL_HNSW_ITERATIVE_SCAN",
        "RETRIEVAL_HNSW_MAX_SCAN_TUPLES", "RETRIEVAL_INDEX_SWEEP_BATCH",
    }),
    # Longer than the functions' own timeouts BY DESIGN, so the function's
    # ceiling stops the work; it moves with the Terraform timeouts, not alone.
    "client timeout paired with the Terraform function timeouts": frozenset({
        "AGENT_INVOKE_READ_TIMEOUT_SECONDS",
    }),
    # Delivery, reminder and cost-telemetry constants read by the workers.
    "worker and telemetry constants": frozenset({
        "DELIVERY_MAX_RETRIES",
        "USD_TO_INR_RATE", "ASSESSMENT_COST_ALERT_INR",
    }),
}

_ACTIVE = re.compile(r"([A-Z][A-Z0-9_]*)=")
_DOCUMENTED_UNSET = re.compile(r"#\s*([A-Z][A-Z0-9_]*)=")
_TERRAFORM_ASSIGNMENT = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


def _lines() -> list[str]:
    return ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()


def _active_keys() -> list[str]:
    return [m.group(1) for line in _lines() if (m := _ACTIVE.match(line))]


def _documented() -> set[str]:
    commented = {m.group(1) for line in _lines() if (m := _DOCUMENTED_UNSET.match(line))}
    return set(_active_keys()) | commented


def _fields() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def _internal() -> set[str]:
    return set().union(*INTERNAL_ONLY.values())


def test_every_setting_is_documented_or_declared_internal() -> None:
    missing = sorted(_fields() - _documented() - _internal())
    assert not missing, (
        "These Settings fields are neither in .env.example nor declared in "
        f"INTERNAL_ONLY with a reason: {missing}. Document a deployment setting; "
        "declare a tuning constant under the reason it is not one."
    )


def test_every_documented_key_has_a_reader() -> None:
    unread = sorted(_documented() - _fields() - set(EXTERNAL))
    assert not unread, (
        f".env.example documents keys nothing reads: {unread}. An operator sets "
        "these expecting an effect; delete them with the code that read them."
    )


def test_no_key_is_set_twice() -> None:
    keys = _active_keys()
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    assert not duplicates, f"set more than once in .env.example: {duplicates}"


def test_the_internal_declarations_do_not_outlive_their_fields() -> None:
    stale = sorted(_internal() - _fields())
    assert not stale, f"INTERNAL_ONLY names fields that no longer exist: {stale}"
    both = sorted(_internal() & _documented())
    assert not both, f"declared internal AND documented in .env.example: {both}"
    groups = [name for names in INTERNAL_ONLY.values() for name in names]
    assert len(groups) == len(set(groups)), "a field is declared under two reasons"


def test_the_external_declarations_do_not_outlive_their_keys() -> None:
    assert not (set(EXTERNAL) & _fields()), "an EXTERNAL key is a Settings field now; drop it from EXTERNAL"
    gone = sorted(set(EXTERNAL) - _documented())
    assert not gone, f"EXTERNAL names keys .env.example no longer carries: {gone}"


def test_a_setting_a_deployment_sets_is_documented() -> None:
    """Deployment data is what an environment actually sets. A field any
    Terraform root assigns must be in .env.example, never internal."""
    assigned: set[str] = set()
    for root in TERRAFORM_ROOTS:
        assert root.exists(), f"{root.relative_to(ROOT)} is missing; port this check rather than skipping it"
        assigned |= set(_TERRAFORM_ASSIGNMENT.findall(root.read_text(encoding="utf-8")))
    set_by_terraform = assigned & _fields()
    assert set_by_terraform, "no Terraform root sets a Settings field; this check would be vacuous"
    hidden = sorted(set_by_terraform - _documented())
    assert not hidden, f"a deployment sets these but .env.example does not document them: {hidden}"


def test_the_retired_keys_stay_retired() -> None:
    """The keys this parity change removed, named so a revert is caught with
    the reason rather than only as an unread key. The Cloud Run project keys
    are kept out by `test_gcp_and_celery_wording_removed.py` tree-wide, and the
    SMS vendor keys by `test_login_otp_removed.py`."""
    retired = {
        "OTP_TTL_MINUTES", "OTP_MAX_ATTEMPTS", "OTP_COOLDOWN_MINUTES",
        "SENDER_OTP_TTL_SECONDS", "SENDER_OTP_MAX_ATTEMPTS", "SENDER_OTP_RESEND_COOLDOWN_SECONDS",
        "LLM_KEY_ENCRYPTION_SECRET", "POSTGRES_PASSWORD", "SQL_TIER", "REDIS_SIZE",
    }
    back = sorted((_documented() | _fields()) & retired)
    assert not back, f"retired configuration is back: {back}"
