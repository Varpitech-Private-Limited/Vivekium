"""Every proctoring threshold, in one frozen object, from Settings.

WHY A SECOND OBJECT WHEN SETTINGS ALREADY HOLDS THE VALUES
----------------------------------------------------------
`core/config.Settings` is where an operator moves a number. This is where the
pipeline READS them: one `ProctoringConfig` per process, built once, with the
browser-side subset projected by `client_config()` so the client and the server
are provably working from the same figures. A detector in the browser that
carried its own confidence threshold would drift from the one the server
records, and the report would describe a rule nobody was actually applying.

Nothing here is a literal. Every field is read from a `proctoring_*` setting,
and `tests/test_proctoring_config.py` asserts that no module under
`services/proctoring/` contains a numeric threshold of its own.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from functools import lru_cache
from typing import Any

from app.core.config import get_settings

__all__ = ["ProctoringConfig", "get_config", "client_config", "CLIENT_FIELDS"]


@dataclass(frozen=True)
class ProctoringConfig:
    max_warnings: int
    object_confidence_threshold: float
    object_consecutive_frames: int
    object_cooldown_seconds: int
    second_person_cooldown_seconds: int
    face_distance_threshold: float
    identity_check_interval_seconds: int
    identity_consecutive_mismatches: int
    obstruction_seconds: int
    obstruction_variance_threshold: float
    face_absent_moderate_seconds: int
    face_absent_moderate_cooldown_seconds: int
    face_absent_extended_seconds: int
    focus_loss_ignore_under_seconds: float
    display_check_interval_seconds: int
    audio_chunk_seconds: int
    audio_max_chunk_bytes: int
    second_voice_consecutive_chunks: int
    second_voice_min_seconds: float
    speech_min_seconds: float
    speech_highlight_threshold: int
    loud_threshold_dbfs: float
    loud_margin_db: float
    loud_hysteresis_db: float
    loud_sustained_seconds: int
    loud_cooldown_seconds: int
    loud_calibration_seconds: int
    report_overdue_minutes: int
    analysis_service_url: str
    analysis_timeout_seconds: float
    heartbeat_interval_seconds: int
    heartbeat_gap_seconds: int
    integrity_failure_termination_seconds: int
    device_max_pauses: int
    device_grace_seconds: int
    device_glitch_seconds: int
    sampling_fps_normal: int
    sampling_fps_confirming: int
    confirming_window_seconds: int
    sampling_fps_degraded: int
    low_light_luminance_threshold: float
    low_light_cooldown_seconds: int
    baseline_answers: int
    fast_entry_multiplier: float
    fast_entry_sustained_seconds: int
    uniform_span_chars: int
    uniform_max_corrections: int
    uniform_max_pause_seconds: float
    low_ratio_min_length: int
    low_ratio_threshold: float
    pause_gap_seconds: float
    burst_window_seconds: int
    mouse_sample_hz: int
    max_keystroke_samples: int
    event_batch_max: int
    ai_text_detection_enabled: bool
    ai_text_threshold: float
    ai_text_min_chars: int
    event_retention_days: int

    @property
    def audio_analysis_available(self) -> bool:
        """Whether a second voice CAN be detected at all in this deployment.

        False is reported, never hidden: the report says audio monitoring was
        unavailable rather than saying nothing was heard.
        """
        return bool(self.analysis_service_url)


#: The subset the browser needs to run its detectors and its capture. Every
#: name here is also a field above; the projection is by name so a threshold
#: added to the client list without being added to the config fails at import.
CLIENT_FIELDS: tuple[str, ...] = (
    "max_warnings",
    "object_confidence_threshold",
    "object_consecutive_frames",
    "face_distance_threshold",
    "identity_check_interval_seconds",
    "obstruction_seconds",
    "obstruction_variance_threshold",
    "face_absent_moderate_seconds",
    "face_absent_extended_seconds",
    "focus_loss_ignore_under_seconds",
    "display_check_interval_seconds",
    "audio_chunk_seconds",
    "audio_max_chunk_bytes",
    "heartbeat_interval_seconds",
    "integrity_failure_termination_seconds",
    # The browser holds a camera or microphone loss back for the glitch window
    # before reporting it. The grace and the pause allowance are deliberately
    # NOT client fields: the browser decides neither, and reads both from the
    # server's `pause` state on every ingest and heartbeat response
    # (`schemas.proctoring.PauseOut`), with the grace as a server deadline.
    "device_glitch_seconds",
    "sampling_fps_normal",
    "sampling_fps_confirming",
    "confirming_window_seconds",
    "sampling_fps_degraded",
    "low_light_luminance_threshold",
    "low_light_cooldown_seconds",
    "loud_threshold_dbfs",
    "loud_margin_db",
    "loud_hysteresis_db",
    "loud_sustained_seconds",
    "loud_cooldown_seconds",
    "loud_calibration_seconds",
    "mouse_sample_hz",
    "max_keystroke_samples",
    "event_batch_max",
)


@lru_cache(maxsize=1)
def get_config() -> ProctoringConfig:
    settings = get_settings()
    values: dict[str, Any] = {
        field.name: getattr(settings, f"proctoring_{field.name}")
        for field in fields(ProctoringConfig)
    }
    return ProctoringConfig(**values)


def client_config() -> dict[str, Any]:
    """The browser-side thresholds, keyed exactly as the client reads them."""
    config = get_config()
    return {name: getattr(config, name) for name in CLIENT_FIELDS}


_missing = set(CLIENT_FIELDS) - {field.name for field in fields(ProctoringConfig)}
if _missing:  # pragma: no cover - an import-time contract, exercised by tests
    raise ImportError(f"CLIENT_FIELDS names thresholds the config lacks: {sorted(_missing)}")
