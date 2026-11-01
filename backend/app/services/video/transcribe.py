"""Amazon Transcribe over one stored audio object, for a spoken answer.

WHY A MODULE OF ITS OWN
-----------------------
The video interview transcribed a whole recording and segmented it by question
marks (`video/processing`). That mode is retired (Appendix B section 1); what
survives is a candidate SPEAKING one prose answer, which is transcribed and
becomes the answer. The Transcribe call is the same service, the same
cross-region working-bucket path and the same bounded poll, parameterised on
the media format and the time budget, because a three-minute answer in webm is
not a two-hour interview in wav.

HONEST FAILURE, NEVER A FAKE TRANSCRIPT
---------------------------------------
Disabled (`transcribe_enabled = false`), a FAILED job, a timeout and a result
with no text all raise `TranscriptionError` with an operator-readable reason.
The caller records the failure as a STATE (`voice_answers.status = failed`)
and the candidate is told to type; nothing here ever substitutes text for
speech it could not read (claude.md rule 6).

THE AUDIO IS TRANSIENT. Only the transcript text is kept (Appendix B: "Store
transcript text only; audio lives in the session recording"). This module
reads it and the caller deletes it, HEAD-confirmed; the cross-region working
copies are deleted here, and a failure to delete one is logged, never raised,
because the working bucket's own expiry is the backstop and a transcript that
succeeded must not be reported as failed over a cleanup step.

Sync, like `video/storage`: the task runs it in a worker, never in a request.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.core.config import get_settings
from app.services.video import storage

logger = logging.getLogger(__name__)

#: What an operator reads when speech to text is switched off. The candidate
#: never sees this; they see that the microphone is not offered.
TRANSCRIBE_DISABLED_DETAIL = (
    "Speech to text is not configured for this deployment "
    "(TRANSCRIBE_ENABLED is false), so a spoken answer cannot be transcribed."
)

#: Every Transcribe job name starts with this: the task worker's IAM grant is
#: scoped to `transcription-job/readypick-*` (infra/environments/pilot/main.tf),
#: and a name outside it is refused by AWS with AccessDenied on the first call.
JOB_NAME_PREFIX = "readypick-voice-"

#: The media formats Transcribe reads that a browser recorder produces.
MEDIA_FORMATS: frozenset[str] = frozenset({"webm", "ogg", "mp4", "wav"})


class TranscriptionError(RuntimeError):
    """The audio could not be turned into text. The message is operator
    wording and never carries anything the candidate said."""


def is_available() -> bool:
    """Whether a spoken answer can be transcribed in this deployment."""
    return bool(get_settings().transcribe_enabled)


def _boto_config() -> Any:
    """Explicit timeouts and bounded retries: an unreachable endpoint that
    HANGS defeats every try/except around the call."""
    from botocore.config import Config  # noqa: PLC0415

    return Config(
        connect_timeout=10,
        read_timeout=30,
        retries={"max_attempts": 2, "mode": "standard"},
    )


def _transcribe_client() -> Any:
    """A Transcribe client in the TRANSCRIBE region, which is not necessarily
    the deployment region: ap-south-2 has no Transcribe endpoint at all."""
    import boto3  # noqa: PLC0415

    return boto3.client(
        "transcribe",
        region_name=get_settings().effective_transcribe_region or None,
        config=_boto_config(),
    )


def _working_s3_client() -> Any:
    """An S3 client bound to the TRANSCRIBE region, for the working bucket
    only. The product's own bucket stays on `object_storage.client()`."""
    import boto3  # noqa: PLC0415

    return boto3.client(
        "s3",
        region_name=get_settings().effective_transcribe_region or None,
        config=_boto_config(),
    )


def _delete_working_objects(bucket: str, object_keys: tuple[str, ...], job_name: str) -> None:
    """Delete the cross-region working copies. An AWS refusal is logged, never
    raised: the working bucket's own expiry is the backstop, and a transcript
    that succeeded must not be reported as failed over a cleanup step. A
    programming error propagates."""
    from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415

    client = _working_s3_client()
    for key in object_keys:
        try:
            client.delete_object(Bucket=bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            logger.warning(
                "voice_transcribe.working_delete_failed job=%s key=%s error=%s",
                job_name, key, type(exc).__name__,
            )


def run_transcription(
    *,
    audio_key: str,
    output_key: str,
    job_name: str,
    media_format: str,
    timeout_seconds: int,
    poll_seconds: float,
) -> dict[str, Any]:
    """Run one Transcribe job over `audio_key` and return its parsed result.

    Blocks, polling every `poll_seconds`, for at most `timeout_seconds`. When
    Transcribe runs in the product's own region the output lands in the
    product's bucket at `output_key` (the caller deletes it, HEAD-confirmed);
    otherwise the audio makes a bounded round trip through the working bucket
    and both working objects are deleted here.
    """
    settings = get_settings()
    if not settings.transcribe_enabled:
        raise TranscriptionError(TRANSCRIBE_DISABLED_DETAIL)
    if media_format not in MEDIA_FORMATS:
        raise TranscriptionError(f"media format {media_format!r} is not one Transcribe reads")
    home_bucket = (settings.s3_bucket or "").strip()
    work_bucket = settings.effective_transcribe_bucket
    remote = bool(work_bucket) and work_bucket != home_bucket
    client = _transcribe_client()
    try:
        if remote:
            # Server-side: the bytes never pass through this process.
            _working_s3_client().copy_object(
                Bucket=work_bucket,
                Key=audio_key,
                CopySource={"Bucket": home_bucket, "Key": audio_key},
                ServerSideEncryption="AES256",
            )
        client.start_transcription_job(
            TranscriptionJobName=job_name,
            LanguageCode=settings.transcribe_language_code,
            Media={"MediaFileUri": f"s3://{work_bucket}/{audio_key}"},
            MediaFormat=media_format,
            OutputBucketName=work_bucket,
            OutputKey=output_key,
        )
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        while True:
            job = client.get_transcription_job(TranscriptionJobName=job_name)[
                "TranscriptionJob"
            ]
            job_status = job["TranscriptionJobStatus"]
            if job_status == "COMPLETED":
                break
            if job_status == "FAILED":
                raise TranscriptionError(
                    "Amazon Transcribe reported the job failed: "
                    + str(job.get("FailureReason") or "no reason given")
                )
            if time.monotonic() >= deadline:
                raise TranscriptionError(
                    f"Amazon Transcribe did not finish within {int(timeout_seconds)}s."
                )
            time.sleep(max(0.0, float(poll_seconds)))
        if remote:
            raw = (
                _working_s3_client()
                .get_object(Bucket=work_bucket, Key=output_key)["Body"]
                .read()
            )
        else:
            raw = storage.get_bytes(output_key)
    finally:
        if remote:
            _delete_working_objects(work_bucket, (audio_key, output_key), job_name)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise TranscriptionError("Amazon Transcribe returned a result that is not JSON.") from exc
    if not isinstance(payload, dict):
        raise TranscriptionError("Amazon Transcribe returned a result that is not an object.")
    return payload


def transcript_text(payload: dict[str, Any]) -> str:
    """The transcript, as Transcribe wrote it, whitespace normalised.

    `results.transcripts[].transcript` is the punctuated whole; the word items
    are not reassembled here, because a re-joined item list loses the
    punctuation Transcribe already placed.
    """
    results = payload.get("results")
    if not isinstance(results, dict):
        raise TranscriptionError("Amazon Transcribe returned a result with no results block.")
    transcripts = results.get("transcripts")
    if not isinstance(transcripts, list):
        raise TranscriptionError("Amazon Transcribe returned a result with no transcripts.")
    parts = [
        str(item.get("transcript") or "")
        for item in transcripts
        if isinstance(item, dict)
    ]
    return " ".join(" ".join(parts).split())
