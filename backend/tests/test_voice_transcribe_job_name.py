"""A spoken-answer Transcribe job name must fall inside the task worker's IAM grant.

The pilot grant is `transcription-job/readypick-*`. A job named `voice-...` was
refused by AWS with AccessDenied on the first call, so every spoken answer
failed in production. This pins the name to the grant read from the Terraform
source, so the two cannot drift apart silently.
"""
from __future__ import annotations

import pathlib
import re

from app.services.video import transcribe

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_job_name_prefix_is_inside_the_iam_grant() -> None:
    main_tf = (REPO / "infra" / "environments" / "pilot" / "main.tf").read_text(encoding="utf-8")
    patterns = re.findall(r"transcription-job/([A-Za-z0-9._-]*)\*", main_tf)
    assert patterns, "the Transcribe IAM grant moved; update this test with it"
    assert any(transcribe.JOB_NAME_PREFIX.startswith(prefix) for prefix in patterns)


def test_the_task_names_its_job_with_the_prefix() -> None:
    source = (REPO / "backend" / "app" / "workers" / "tasks_voice.py").read_text(encoding="utf-8")
    assert "transcribe.JOB_NAME_PREFIX" in source
    assert 'f"voice-' not in source
