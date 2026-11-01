// Whether the candidate is recording a spoken answer right now.
//
// The proctoring audio loudness detector (lib/proctoring/audio.ts) reads this
// to leave the candidate's own dictation out of its sustained-level window: a
// legitimate spoken answer must never count as a loud environment. It is a
// module-level flag plus a window event, so neither side imports the other's
// components. Only the recording phase sets it; uploading and transcribing
// capture no sound.

export const VOICE_RECORDING_EVENT = "vivekium:voice-recording";

let active = false;

export function isVoiceRecordingActive(): boolean {
  return active;
}

/** Idempotent: the event fires only when the state changes. */
export function setVoiceRecordingActive(next: boolean): void {
  if (active === next) return;
  active = next;
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(VOICE_RECORDING_EVENT, { detail: { active: next } }));
  }
}
