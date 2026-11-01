/**
 * Sustained high sound level: browser-local arithmetic over microphone
 * readings (mission item B, 2026-11-01).
 *
 * WHAT THIS IS NOT. A reading here is the RMS of the microphone's samples in
 * dBFS (decibels relative to digital full scale). It is NOT a sound pressure
 * level: the browser applies the device's gain, usually automatic gain
 * control, and often noise suppression, so the same shout reads differently
 * on two laptops and a quiet room can be amplified. It cannot prove shouting,
 * and it cannot prove anything about the candidate. It is therefore an
 * ADVISORY (Path C on the server): shown to the candidate as a reminder, kept
 * in the Proctoring Report as context, and never a warning, never counted
 * toward the warning limit, never a reason to end an assessment.
 *
 * HOW A LEVEL BECOMES AN ADVISORY, in the order the rules are applied:
 *   1. CALIBRATION. The median reading over the first `loud_calibration_seconds`
 *      of non-excluded time is the room's noise floor. A median, so a cough
 *      does not move it. A candidate who happened to be talking during
 *      calibration raises the floor, which only makes the detector LESS
 *      sensitive; that is the safe direction for a non-punitive advisory.
 *   2. THE LINE. Loud means at or above the larger of `loud_threshold_dbfs` and
 *      `floor + loud_margin_db`. The margin is what keeps auto-gain and a noisy
 *      room from tripping a fixed number.
 *   3. SUSTAINED. The level must stay loud for `loud_sustained_seconds`.
 *      Hysteresis: once loud, the level must fall `loud_hysteresis_db` BELOW
 *      the line to count as quiet again, so a level hovering at the line does
 *      not restart the clock on every dip.
 *   4. COOLDOWN. After an advisory, no other for `loud_cooldown_seconds`, and
 *      the same loud stretch is reported once.
 *   5. EXCLUSION. A reading taken while the candidate is dictating a spoken
 *      answer (`isVoiceRecordingActive`), or while the microphone is muted or
 *      lost, is not judged at all and breaks any stretch in progress. The
 *      server applies the same exclusions against its own records; this side
 *      only avoids showing a reminder the server would refuse.
 *
 * No timers, no media: `observe` takes a reading and a clock value, so
 * `sound-level.test.ts` drives it with a synthetic level sequence.
 */
import type { ProctoringClientConfig } from "./config";
import { seconds } from "./config";
import type { EventDraft } from "./events";

export type SoundLevelConfig = Pick<
  ProctoringClientConfig,
  | "loud_threshold_dbfs"
  | "loud_margin_db"
  | "loud_hysteresis_db"
  | "loud_sustained_seconds"
  | "loud_cooldown_seconds"
  | "loud_calibration_seconds"
>;

/** The candidate-facing sentence. Hedged on purpose: a level is not a verdict. */
export const SOUND_LEVEL_ADVISORY = "Sustained high sound level detected; please check your environment.";

/** The speech band used only to LABEL a stretch as voice-like or broadband in
 *  its record. It never decides whether an advisory is raised. */
export const SPEECH_BAND_HZ = { low: 300, high: 3400 } as const;
export const VOICE_LIKE_SHARE = 0.6;

/** Digital silence reads as this rather than minus infinity. */
export const SILENCE_DBFS = -100;

export function rmsToDbfs(rms: number): number {
  if (!(rms > 0)) return SILENCE_DBFS;
  return Math.max(SILENCE_DBFS, 20 * Math.log10(rms));
}

/**
 * Whether the energy of one spectrum sat mostly in the speech band. `bins` are
 * the dB magnitudes `AnalyserNode.getFloatFrequencyData` returns. One moment's
 * spectrum is a weak basis, and the record says "voice_like" or "broadband",
 * never "shouting".
 */
export function spectrumCharacter(
  bins: ArrayLike<number>,
  sampleRate: number,
  fftSize: number
): "voice_like" | "broadband" {
  const binHz = sampleRate / fftSize;
  let inBand = 0;
  let total = 0;
  for (let index = 0; index < bins.length; index += 1) {
    const power = Math.pow(10, bins[index] / 10);
    if (!Number.isFinite(power)) continue;
    total += power;
    const hz = index * binHz;
    if (hz >= SPEECH_BAND_HZ.low && hz <= SPEECH_BAND_HZ.high) inBand += power;
  }
  return total > 0 && inBand / total >= VOICE_LIKE_SHARE ? "voice_like" : "broadband";
}

export interface SoundReading {
  /** The caller's clock, in milliseconds. */
  at: number;
  dbfs: number;
  /** Do not judge this reading: dictation, a muted or lost microphone. */
  excluded: boolean;
}

export class SoundLevelDetector {
  private startedAt: number | null = null;
  private calibration: number[] = [];
  private floor: number | null = null;
  private loudSince: number | null = null;
  private reported = false;
  private peak = SILENCE_DBFS;
  private cooldownUntil = 0;
  private lastAt: number | null = null;

  constructor(
    private readonly config: SoundLevelConfig,
    /** Asked only at the moment an advisory is raised. */
    private readonly character: () => "voice_like" | "broadband" | null = () => null
  ) {}

  /** The calibrated noise floor in dBFS, or null while calibrating. */
  get noiseFloor(): number | null {
    return this.floor;
  }

  /** Forget the stretch in progress (a lost device, a spoken answer). The
   *  calibration and the cooldown are kept: they describe the room. */
  private breakStretch(): void {
    this.loudSince = null;
    this.reported = false;
    this.peak = SILENCE_DBFS;
  }

  observe(reading: SoundReading): EventDraft | null {
    const previous = this.lastAt;
    this.lastAt = reading.at;
    if (reading.excluded) {
      this.breakStretch();
      return null;
    }
    // A timer starved by a background tab leaves a hole in the readings. A
    // loud sample before it and a loud sample after it are not one stretch.
    const maxGapMs = seconds(this.config.loud_sustained_seconds) / 2;
    if (previous !== null && reading.at - previous > maxGapMs) this.breakStretch();

    if (this.startedAt === null) this.startedAt = reading.at;
    if (this.floor === null) {
      this.calibration.push(reading.dbfs);
      if (reading.at - this.startedAt >= seconds(this.config.loud_calibration_seconds)) {
        const sorted = [...this.calibration].sort((a, b) => a - b);
        this.floor = sorted[Math.floor(sorted.length / 2)];
        this.calibration = [];
      }
      return null;
    }

    const line = Math.max(this.config.loud_threshold_dbfs, this.floor + this.config.loud_margin_db);
    if (this.loudSince === null) {
      if (reading.dbfs < line) return null;
      this.loudSince = reading.at;
      this.peak = reading.dbfs;
    } else if (reading.dbfs < line - this.config.loud_hysteresis_db) {
      this.breakStretch();
      return null;
    }
    this.peak = Math.max(this.peak, reading.dbfs);

    const lasted = reading.at - this.loudSince;
    if (this.reported || lasted < seconds(this.config.loud_sustained_seconds)) return null;
    this.reported = true;
    if (reading.at < this.cooldownUntil) return null;
    this.cooldownUntil = reading.at + seconds(this.config.loud_cooldown_seconds);
    const character = this.character();
    return {
      event_type: "SUSTAINED_HIGH_SOUND_LEVEL",
      duration_ms: Math.round(lasted),
      metadata: {
        peak_dbfs: Math.round(this.peak * 10) / 10,
        floor_dbfs: Math.round(this.floor * 10) / 10,
        ...(character ? { character } : {}),
      },
    };
  }
}
