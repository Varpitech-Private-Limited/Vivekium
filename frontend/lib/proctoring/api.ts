/**
 * The proctoring routes the client calls, and the shapes they exchange.
 *
 * Mirrors `backend/app/schemas/proctoring.py`. The candidate side is EVENTS
 * ONLY: an identifier, a time, a duration, a confidence and a small metadata
 * object. There is no field for a frame, an image or any text a candidate
 * typed. Two things leave the browser that are media: the audio chunk, to the
 * one route that analyses it in memory and destroys it, and the session
 * recording, which goes to the ASSESSMENT routes at the bottom of this file
 * rather than to the proctoring ones (see the note there).
 *
 * THE DEVICE PAUSE (Phase 3, 2026-09-24). A lost camera or microphone pauses
 * the assessment for a two-minute grace instead of ending it. The client
 * reports the loss as an event; the SERVER opens the pause, counts it against
 * the session's allowance and ends the session when the allowance or the
 * grace runs out. Every ingest and heartbeat response carries the resulting
 * `pause` state, and that state is the only thing the pause overlay renders
 * from: this client never decides that a session is paused, how long the grace
 * is, or how many pauses remain.
 */
import { apiFetch, apiGet, apiPost, ApiError, NETWORK_ERROR } from "@/lib/api";

import type { ClientEventType } from "./catalog";
import { parseClientConfig, type ProctoringClientConfig } from "./config";

const BASE = "/api/v2/proctoring";

/**
 * The server's statement about the device pause, carried on every ingest and
 * heartbeat response. `grace_deadline_at` is on the SERVER's clock and is
 * null when no pause is open; `pauses_used` counts the pauses this session
 * has spent, the open one included.
 */
export interface PauseState {
  paused: boolean;
  /**
   * What the candidate reads while the pause is open, composed by the server
   * (`phrasing.pause_message`): which device stopped, what to do, the time
   * allowed in words and what the next stop will mean. Null when nothing is
   * paused. The pause screen renders it verbatim, so the allowance a candidate
   * is told about is the allowance the server is counting.
   */
  message?: string | null;
  grace_deadline_at: string | null;
  pauses_used: number;
  max_pauses: number;
}

/**
 * GET /proctoring/config. The thresholds, and the rules the candidate reads
 * before agreeing. The rules are the SERVER's sentences
 * (`phrasing.candidate_rules`), composed from the same configuration that
 * enforces them, so the screen cannot describe a grace, a pause allowance or
 * a warning count the server is not applying.
 */
export interface ClientConfigOut {
  config: ProctoringClientConfig;
  max_warnings: number;
  audio_analysis_available: boolean;
  candidate_rules: string[];
}

export interface DeviceContextIn {
  user_agent: string;
  platform: string;
  screen_count: number | null;
  screen_width: number | null;
  screen_height: number | null;
  camera_width: number | null;
  camera_height: number | null;
  hardware_concurrency: number | null;
  webgl: boolean | null;
}

/** The six checks of spec 8.2, plus the measured inference rate so a slow
 *  device is recorded as degraded rather than refused. */
export interface SystemCheckIn {
  camera: boolean;
  microphone: boolean;
  browser_supported: boolean;
  fullscreen_supported: boolean;
  face_detected: boolean;
  inference_adequate: boolean;
  measured_fps: number | null;
}

export interface SessionCreateIn {
  consent: true;
  device_context: DeviceContextIn;
  system_check: SystemCheckIn;
  /** The 128-float descriptor from the system check. A vector, not an image. */
  face_descriptor: number[];
}

export type WarningPolicy = "terminate" | "continue_and_note";

export interface SessionOut {
  session_id: string;
  conversation_id: string;
  status: string;
  warnings_used: number;
  max_warnings: number;
  warning_policy: WarningPolicy;
  consented_at: string;
  config: ProctoringClientConfig;
  audio_analysis_available: boolean;
  /** The device pause as it stands when the session is (re)opened, so a page
   *  reloaded in the middle of a pause shows the pause screen at once rather
   *  than a question the server will refuse to accept an answer to. */
  pause?: PauseState | null;
}

export interface EventIn {
  event_type: ClientEventType;
  occurred_at: string;
  duration_ms?: number | null;
  confidence?: number | null;
  question_id?: string | null;
  metadata?: Record<string, unknown>;
}

export interface WarningOut {
  number: number;
  max_warnings: number;
  event_type: string;
  message: string;
  final: boolean;
}

export interface TerminationOut {
  reason_code: string;
  message: string;
}

export interface IngestOut {
  accepted: number;
  warnings_used: number;
  max_warnings: number;
  status: string;
  warning: WarningOut | null;
  termination: TerminationOut | null;
  /** Absent means the response said nothing about the pause, which is not
   *  the same as "not paused": the last statement stands. */
  pause?: PauseState | null;
}

export interface MonitoringStatus {
  camera: boolean;
  microphone: boolean;
  models: boolean;
  handlers: boolean;
}

/** Mirrors `schemas/proctoring.DetectionStatsIn`: counts and two summary
 *  figures for one heartbeat interval, never a frame. */
export interface DetectionStats {
  frames_processed: number;
  frames_failed: number;
  measured_fps: number | null;
  inference_ms_mean: number | null;
  phone_best_score: number | null;
}

export interface HeartbeatIn {
  identity_matched: boolean | null;
  monitoring: MonitoringStatus;
  detection?: DetectionStats | null;
}

export interface HeartbeatOut {
  status: string;
  warnings_used: number;
  server_time: string;
  interval_seconds: number;
  termination: TerminationOut | null;
  pause?: PauseState | null;
}

export interface AudioChunkOut {
  analysed: boolean;
  status: string;
  warnings_used: number;
  warning: WarningOut | null;
  termination: TerminationOut | null;
}

/**
 * The configuration and the candidate rules. Refuses a response without the
 * rules rather than rendering a consent screen with nothing on it: agreeing to
 * rules nobody was shown is not agreement, and a hard-coded copy here would be
 * a second author of sentences the server already owns.
 */
export async function fetchClientConfig(): Promise<ClientConfigOut> {
  const raw = await apiGet<Record<string, unknown>>(`${BASE}/config`);
  const rules = raw.candidate_rules;
  if (
    !Array.isArray(rules) ||
    rules.length === 0 ||
    !rules.every((rule) => typeof rule === "string" && rule.trim().length > 0)
  ) {
    throw new Error("The assessment rules could not be loaded. Please try again.");
  }
  if (typeof raw.max_warnings !== "number" || typeof raw.audio_analysis_available !== "boolean") {
    throw new Error("The monitoring settings could not be loaded. Please try again.");
  }
  return {
    config: parseClientConfig(raw.config),
    max_warnings: raw.max_warnings,
    audio_analysis_available: raw.audio_analysis_available,
    candidate_rules: rules as string[],
  };
}

export async function createSession(linkId: string, body: SessionCreateIn): Promise<SessionOut> {
  const session = await apiPost<SessionOut>(`${BASE}/links/${linkId}/session`, body);
  return { ...session, config: parseClientConfig(session.config) };
}

export function postEvents(sessionId: string, events: EventIn[]): Promise<IngestOut> {
  return apiPost<IngestOut>(`${BASE}/sessions/${sessionId}/events`, { events });
}

export function postHeartbeat(sessionId: string, body: HeartbeatIn): Promise<HeartbeatOut> {
  return apiPost<HeartbeatOut>(`${BASE}/sessions/${sessionId}/heartbeat`, body);
}

/**
 * The warning the candidate just acknowledged. The server holds the question
 * timer from the moment it issued the warning until this call (capped on its
 * side), which replaces the client-measured pause length that used to travel on
 * the next answer: a pause length the client reports is a number the client
 * chose.
 */
export async function acknowledgeWarning(sessionId: string): Promise<void> {
  await apiPost<unknown>(`${BASE}/sessions/${sessionId}/warnings/ack`);
}

/**
 * The audio chunk, as multipart. `apiFetch` rather than `apiUpload` because the
 * response has to be read whether or not it was 2xx: a 409 here means the
 * session ended and the caller needs the message, not a generic failure.
 *
 * Nothing but the audio travels. Whether speech in it overlapped a spoken
 * answer is judged by the SERVER against its own voice-answer stamps, over a
 * window wide enough to cover the chunk's own length and its trip here, so no
 * time this device measured decides it.
 */
export async function postAudioChunk(sessionId: string, chunk: Blob): Promise<AudioChunkOut> {
  const form = new FormData();
  form.append("chunk", chunk, "chunk.webm");
  const response = await apiFetch(`${BASE}/sessions/${sessionId}/audio`, {
    method: "POST",
    body: form,
  });
  const text = await response.text();
  const payload: unknown = text ? JSON.parse(text) : null;
  if (!response.ok) {
    throw new ApiError(response.status, payload);
  }
  return payload as AudioChunkOut;
}

/** A 409 from any session route: the session is over on the server. */
export function isSessionEnded(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 409;
}

/** A request the server refused as malformed. Retrying cannot repair it. */
export function isClientFault(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status >= 400 &&
    error.status < 500 &&
    error.status !== 409 &&
    error.status !== 429
  );
}

// ── The proctored session's recording (owner ruling 2026-09-22, Phase 3) ────
//
// These call the ASSESSMENT routes, not the proctoring ones, and the prefix
// difference is the design rather than an accident: the recording is an
// assessment artifact stored on `video_recordings`, and the proctoring service
// neither writes it nor reads it.
//
// THE RECORDING TRAVELS IN SEGMENTS OF PARTS. It used to be held whole in the
// browser and posted once at the end, and the API read up to a gibibyte of it
// into memory in one request. Now each segment is one multipart upload on the
// server: parts of at least `part_min_bytes` (the object store's floor for
// every part but the last) and at most `part_max_bytes` are PUT as they fill,
// and a new segment starts whenever the devices are reacquired after a loss,
// because a recorder bound to a camera that went away cannot continue into the
// one that came back. The server stitches the segments in order when the
// recording is finalized. No bucket name and no object key crosses this
// boundary, in either direction.

const ASSESSMENTS_BASE = "/api/v2/assessments";

/**
 * POST /conversations/links/{link_id}/session-media/start. The server decides
 * the recording's kind and the encoder ceilings: the bitrates and the frame
 * size are the server's numbers, so the recording's size per minute is bounded
 * by a setting rather than by whichever camera the candidate owns.
 */
export interface SessionMediaStartOut {
  conversation_id: string;
  recording_id: string;
  status: string;
  part_min_bytes: number;
  part_max_bytes: number;
  video_bits_per_second: number;
  audio_bits_per_second: number;
  /** The recording's frame size ceiling. The camera the detectors sample is
   *  left at its own size; only the recorded copy is scaled. */
  max_width: number;
  max_height: number;
}

const MEDIA_START_NUMBERS = [
  "part_min_bytes",
  "part_max_bytes",
  "video_bits_per_second",
  "audio_bits_per_second",
  "max_width",
  "max_height",
] as const;

/**
 * Refuses a response missing any ceiling rather than substituting one. A
 * default here would be a second copy of a server setting, and an encoder
 * running on a number nobody configured is a recording whose size nobody
 * bounded.
 */
export function parseSessionMediaStart(raw: unknown): SessionMediaStartOut {
  if (!raw || typeof raw !== "object") {
    throw new Error("The recording could not be opened: the response was empty.");
  }
  const source = raw as Record<string, unknown>;
  for (const field of ["conversation_id", "recording_id", "status"] as const) {
    if (typeof source[field] !== "string" || !source[field]) {
      throw new Error(`The recording could not be opened: "${field}" is missing.`);
    }
  }
  for (const field of MEDIA_START_NUMBERS) {
    const value = source[field];
    if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
      throw new Error(`The recording could not be opened: "${field}" is missing.`);
    }
  }
  const parsed = source as unknown as SessionMediaStartOut;
  if (parsed.part_min_bytes > parsed.part_max_bytes) {
    throw new Error("The recording could not be opened: its part sizes disagree.");
  }
  return parsed;
}

export async function startSessionMedia(linkId: string): Promise<SessionMediaStartOut> {
  const raw = await apiPost<unknown>(
    `${ASSESSMENTS_BASE}/conversations/links/${linkId}/session-media/start`
  );
  return parseSessionMediaStart(raw);
}

/** POST /conversations/{id}/recording/segments, and the answer to a part
 *  or a completion. The ordinal is the server's; the browser addresses parts
 *  by the id and never learns where the bytes land. */
export interface RecordingSegmentOut {
  segment_id: string;
  ordinal: number;
}

/** `sourceFormat` is the MediaRecorder's MIME type. The server keeps only its
 *  subtype, through a fixed extension table, so nothing here can reach an
 *  object key. */
export function openRecordingSegment(
  conversationId: string,
  sourceFormat: string
): Promise<RecordingSegmentOut> {
  return apiPost<RecordingSegmentOut>(
    `${ASSESSMENTS_BASE}/conversations/${conversationId}/recording/segments`,
    { source_format: sourceFormat }
  );
}

/**
 * PUT one part of one segment, as the raw bytes. `apiFetch` rather than the
 * JSON helpers, because the body is media and the status has to be read
 * whether or not it was 2xx: a 409 means the recording is closed on the server
 * and no retry will reopen it.
 *
 * `final` marks the segment's LAST part. The object store refuses a part under
 * its floor unless it is the last one, and it would say so only when the
 * segment is completed, after the session is over and nothing can be re-sent;
 * so the server refuses a short part up front unless it is marked, and a part
 * marked final closes the segment to any later part number.
 */
export async function uploadRecordingPart(
  conversationId: string,
  segmentId: string,
  partNumber: number,
  body: Blob,
  final: boolean
): Promise<void> {
  const query = final ? "?final=true" : "";
  let response: Response;
  try {
    response = await apiFetch(
      `${ASSESSMENTS_BASE}/conversations/${conversationId}/recording/segments/${segmentId}/parts/${partNumber}${query}`,
      {
        method: "PUT",
        body,
        headers: { "Content-Type": "application/octet-stream" },
      }
    );
  } catch (cause) {
    // `fetch` rejects only when the request never reached the server. Named
    // as the transport failure it is, the way `lib/api` names it, so the
    // recorder can tell "try again" from "the server said no".
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new ApiError(NETWORK_ERROR, null, "The recording part could not reach the server.");
  }
  if (!response.ok) {
    throw new ApiError(response.status, null);
  }
}

export async function completeRecordingSegment(
  conversationId: string,
  segmentId: string
): Promise<void> {
  await apiPost<unknown>(
    `${ASSESSMENTS_BASE}/conversations/${conversationId}/recording/segments/${segmentId}/complete`
  );
}

/** Every segment is complete: the server may stitch, compress and store. */
export async function finalizeRecording(conversationId: string): Promise<void> {
  await apiPost<unknown>(
    `${ASSESSMENTS_BASE}/conversations/${conversationId}/recording/finalize`
  );
}

