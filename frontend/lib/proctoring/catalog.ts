/**
 * The events this client may emit, mirrored from
 * `backend/app/services/proctoring/catalog.py` (the entries with
 * `client_emittable=True`) with the consequence path the server assigns to
 * each.
 *
 * THE SERVER DECIDES. The path is carried here for ONE reason: an event on
 * Path A or Path P is flushed the moment it is queued rather than on the next
 * batch tick (`isImmediate`), because the session is about to end or pause and
 * the candidate should learn that from a screen and not from a request that
 * lands seconds later. The client never acts on a path itself; it requests,
 * the server issues.
 *
 * `lib/proctoring/model-assets.test.ts` reads the Python catalog and asserts
 * this table names exactly the client-emittable identifiers, so an event
 * added on one side without the other fails a test rather than a request.
 */

export type ConsequencePath = "A" | "B" | "C" | "P";

export const CLIENT_EVENTS = {
  // Path A: proctoring itself defeated; immediate termination.
  CAMERA_OBSTRUCTED: "A",
  FACE_ABSENT_EXTENDED: "A",
  INTEGRITY_CHECK_FAILED: "A",
  // Path P (Phase 3, 2026-09-24): a lost camera or microphone PAUSES the
  // assessment for the server's grace instead of ending it. These four were
  // Path A until then (the microphone's stream failure had no event at all);
  // a third loss, or no recovery inside the grace, is what ends a session
  // now, and the server derives both.
  CAMERA_PERMISSION_LOST: "P",
  MIC_PERMISSION_LOST: "P",
  CAMERA_STREAM_FAILED: "P",
  MIC_STREAM_FAILED: "P",
  // Path B: the shared three-warning counter.
  FULLSCREEN_EXITED: "B",
  WINDOW_FOCUS_LOST: "B",
  DEVICE_DETECTED_PHONE: "B",
  DEVICE_DETECTED_LAPTOP: "B",
  DEVICE_DETECTED_SCREEN: "B",
  SECOND_PERSON_DETECTED: "B",
  FACE_ABSENT_MODERATE: "B",
  MULTIPLE_DISPLAYS_DETECTED: "B",
  // Path C: logged only.
  FACE_ABSENT_BRIEF: "C",
  IDENTITY_CHECK_MISMATCH: "C",
  LOW_LIGHT: "C",
  BLOCKED_ACTION_ATTEMPTED: "C",
  SESSION_QUALITY_DEGRADED: "C",
  INTEGRITY_CHECK_WARNING: "C",
  // An advisory, deliberately not Path B: a sustained microphone level is
  // context, never a warning (`sound-level.ts`).
  SUSTAINED_HIGH_SOUND_LEVEL: "C",
  // A stream that came back inside `device_glitch_seconds`: logged, and it
  // costs no pause.
  CAMERA_STREAM_INTERRUPTED: "C",
  MIC_STREAM_INTERRUPTED: "C",
  // Every lost device is live again. Logged on Path C; the server reads it to
  // close the open pause (see `device-watch.ts` for when it is sent).
  DEVICE_RECOVERED: "C",
} as const satisfies Record<string, ConsequencePath>;

export type ClientEventType = keyof typeof CLIENT_EVENTS;

/**
 * Whether the queue posts this event the moment it is queued rather than on
 * the next batch tick. Path A because the session is ending; Path P because
 * the assessment is being paused and the candidate should see the grace
 * begin now, not three seconds after their camera went dark; and the recovery
 * because the pause should lift the moment the device is back.
 */
export function isImmediate(eventType: ClientEventType): boolean {
  const path = CLIENT_EVENTS[eventType];
  return path === "A" || path === "P" || eventType === "DEVICE_RECOVERED";
}

/**
 * The COCO classes the specification says to act on (section 3.1) and the
 * event each one requests. `book` is listed there too, but the catalog has no
 * event for it, so it is discarded in the worker with the other classes
 * rather than reported under a type that means something else.
 */
export const OBJECT_EVENT_FOR_LABEL: Readonly<Record<string, ClientEventType>> = {
  "cell phone": "DEVICE_DETECTED_PHONE",
  laptop: "DEVICE_DETECTED_LAPTOP",
  tv: "DEVICE_DETECTED_SCREEN",
};

/** The class whose count, with the face count, decides a second person. */
export const PERSON_LABEL = "person";
