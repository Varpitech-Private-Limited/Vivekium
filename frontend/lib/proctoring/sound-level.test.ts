// The sustained-high-sound-level advisory (mission item B, 2026-11-01).
//
// Synthetic level sequences, one reading per 100 ms like the real poll. What
// is defended: the advisory fires for a level that stays well above the room's
// own floor, and for nothing else that the requirement lists (normal speech,
// dictation, a cough, a steady fan, a muted or silent microphone, auto-gain
// that lifts everything). Nothing here claims a level proves shouting: the
// copy and the record both say it does not.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath, URL } from "node:url";

import { describe, expect, it } from "vitest";

import { CLIENT_EVENTS } from "./catalog";
import {
  rmsToDbfs,
  SILENCE_DBFS,
  SOUND_LEVEL_ADVISORY,
  SoundLevelDetector,
  spectrumCharacter,
  type SoundLevelConfig,
} from "./sound-level";

const CONFIG: SoundLevelConfig = {
  loud_threshold_dbfs: -20,
  loud_margin_db: 25,
  loud_hysteresis_db: 6,
  loud_sustained_seconds: 6,
  loud_cooldown_seconds: 120,
  loud_calibration_seconds: 10,
};

const STEP_MS = 100;

/** Feed a level function over `seconds` of readings; return what was raised. */
function drive(
  detector: SoundLevelDetector,
  from: number,
  seconds: number,
  level: (secondsIn: number) => number,
  excluded = false
) {
  const raised: ReturnType<SoundLevelDetector["observe"]>[] = [];
  let at = from;
  const end = from + seconds * 1000;
  for (; at < end; at += STEP_MS) {
    const draft = detector.observe({ at, dbfs: level((at - from) / 1000), excluded });
    if (draft) raised.push(draft);
  }
  return { raised, next: at };
}

/** A calibrated detector sitting in a quiet room (floor about -60 dBFS). */
function calibrated() {
  const detector = new SoundLevelDetector(CONFIG, () => "voice_like");
  const { next } = drive(detector, 0, CONFIG.loud_calibration_seconds + 1, () => -60);
  expect(detector.noiseFloor).toBe(-60);
  return { detector, next };
}

describe("sustained loud level", () => {
  it("is raised once for a level that stays above the line for the sustained window", () => {
    const { detector, next } = calibrated();
    const { raised } = drive(detector, next, 20, () => -10);
    expect(raised).toHaveLength(1);
    const event = raised[0]!;
    expect(event.event_type).toBe("SUSTAINED_HIGH_SOUND_LEVEL");
    expect(event.duration_ms).toBeGreaterThanOrEqual(CONFIG.loud_sustained_seconds * 1000);
    expect(event.metadata).toMatchObject({ peak_dbfs: -10, floor_dbfs: -60, character: "voice_like" });
  });

  it("is not raised before the window is complete", () => {
    const { detector, next } = calibrated();
    const { raised } = drive(detector, next, CONFIG.loud_sustained_seconds - 1, () => -10);
    expect(raised).toEqual([]);
  });

  it("is not raised for ordinary speech levels in a quiet room", () => {
    const { detector, next } = calibrated();
    // Speech through a laptop microphone sits well below the -20 line.
    const { raised } = drive(detector, next, 120, (t) => -38 + 6 * Math.sin(t * 5));
    expect(raised).toEqual([]);
  });

  it("is not raised for a cough or a clap: loud, but not sustained", () => {
    const { detector, next } = calibrated();
    const { raised } = drive(detector, next, 30, (t) => (t % 10 < 1 ? -8 : -60));
    expect(raised).toEqual([]);
  });

  it("is not raised for a steady fan, because the floor is measured with it running", () => {
    const detector = new SoundLevelDetector(CONFIG);
    // A fan puts the floor at -35. The line becomes -10, not -20.
    const start = drive(detector, 0, 11, () => -35);
    expect(detector.noiseFloor).toBe(-35);
    const { raised } = drive(detector, start.next, 60, () => -33);
    expect(raised).toEqual([]);
  });

  it("is not raised when automatic gain lifts everything together", () => {
    const detector = new SoundLevelDetector(CONFIG);
    // Gain pushes a noisy room's floor up to -28; speech now peaks at -18.
    // That is above the fixed -20 only briefly and never 25 dB over the floor.
    const start = drive(detector, 0, 11, () => -28);
    const { raised } = drive(detector, start.next, 60, (t) => (t % 4 < 2 ? -18 : -28));
    expect(raised).toEqual([]);
  });

  it("is raised for sustained shouting even in a noisy room, once it clears the margin", () => {
    const detector = new SoundLevelDetector(CONFIG);
    const start = drive(detector, 0, 11, () => -45);
    const { raised } = drive(detector, start.next, 12, () => -12);
    expect(raised).toHaveLength(1);
  });
});

describe("hysteresis, cooldown and breaks", () => {
  it("keeps one stretch together across small dips below the line", () => {
    const { detector, next } = calibrated();
    // Hovering around the -20 line: dips to -24 stay inside the 6 dB band.
    const { raised } = drive(detector, next, 12, (t) => (Math.floor(t * 2) % 2 === 0 ? -15 : -24));
    expect(raised).toHaveLength(1);
  });

  it("restarts the clock when the level falls clearly below the line", () => {
    const { detector, next } = calibrated();
    const first = drive(detector, next, 5, () => -10);
    const gap = drive(detector, first.next, 2, () => -60);
    const second = drive(detector, gap.next, 5, () => -10);
    expect([...first.raised, ...gap.raised, ...second.raised]).toEqual([]);
  });

  it("raises one advisory per stretch and not again inside the cooldown", () => {
    const { detector, next } = calibrated();
    const first = drive(detector, next, 10, () => -10);
    expect(first.raised).toHaveLength(1);
    const quiet = drive(detector, first.next, 5, () => -60);
    // A second loud stretch 15 seconds later is inside the 120 second cooldown.
    const second = drive(detector, quiet.next, 10, () => -10);
    expect(second.raised).toEqual([]);
    // After the cooldown a new stretch is reported again.
    const rest = drive(detector, second.next, 130, () => -60);
    const third = drive(detector, rest.next, 10, () => -10);
    expect(third.raised).toHaveLength(1);
  });

  it("does not join two loud readings separated by a hole in the timer", () => {
    const { detector, next } = calibrated();
    const first = detector.observe({ at: next, dbfs: -10, excluded: false });
    // A background tab starved the poll for thirty seconds.
    const second = detector.observe({ at: next + 30_000, dbfs: -10, excluded: false });
    expect([first, second]).toEqual([null, null]);
  });
});

describe("what is not judged", () => {
  it("ignores a spoken answer entirely, however loud, and breaks the stretch", () => {
    const { detector, next } = calibrated();
    const before = drive(detector, next, 4, () => -10);
    const dictating = drive(detector, before.next, 30, () => -8, true);
    expect([...before.raised, ...dictating.raised]).toEqual([]);
    // The stretch before dictation does not carry across it.
    const after = drive(detector, dictating.next, 3, () => -10);
    expect(after.raised).toEqual([]);
  });

  it("does not calibrate on excluded time", () => {
    const detector = new SoundLevelDetector(CONFIG);
    drive(detector, 0, 30, () => -10, true);
    expect(detector.noiseFloor).toBeNull();
  });

  it("never judges a silent or muted microphone", () => {
    const { detector, next } = calibrated();
    const { raised } = drive(detector, next, 60, () => SILENCE_DBFS);
    expect(raised).toEqual([]);
  });

  it("raises nothing while still calibrating", () => {
    const detector = new SoundLevelDetector(CONFIG);
    const { raised } = drive(detector, 0, 9, () => -5);
    expect(raised).toEqual([]);
    expect(detector.noiseFloor).toBeNull();
  });
});

describe("level arithmetic", () => {
  it("reads full scale as zero and silence as the floor value", () => {
    expect(rmsToDbfs(1)).toBe(0);
    expect(rmsToDbfs(0.1)).toBeCloseTo(-20, 5);
    expect(rmsToDbfs(0)).toBe(SILENCE_DBFS);
    expect(rmsToDbfs(Number.NaN)).toBe(SILENCE_DBFS);
  });

  it("labels energy in the speech band as voice-like and the rest as broadband", () => {
    const fft = 2048;
    const rate = 48_000;
    const bins = new Float32Array(fft / 2).fill(-120);
    for (let i = 0; i < bins.length; i += 1) {
      const hz = (i * rate) / fft;
      if (hz >= 500 && hz <= 2000) bins[i] = -30;
    }
    expect(spectrumCharacter(bins, rate, fft)).toBe("voice_like");
    expect(spectrumCharacter(new Float32Array(fft / 2).fill(-40), rate, fft)).toBe("broadband");
  });
});

describe("the copy and the server contract", () => {
  it("uses the hedged sentence and no accusation", () => {
    expect(SOUND_LEVEL_ADVISORY).toBe(
      "Sustained high sound level detected; please check your environment."
    );
    expect(SOUND_LEVEL_ADVISORY).not.toMatch(/shout|cheat|violation|warning/i);
  });

  it("is an advisory on Path C, never the shared warning counter", () => {
    expect(CLIENT_EVENTS.SUSTAINED_HIGH_SOUND_LEVEL).toBe("C");
  });

  it("sends only the metadata keys and character values the server keeps", () => {
    const frontend = fileURLToPath(new URL("../..", import.meta.url));
    const source = readFileSync(
      join(frontend, "..", "backend", "app", "services", "proctoring", "ingestion.py"),
      "utf8"
    );
    const keys = source.match(/LOUD_LEVEL_KEYS = \(([^)]*)\)/);
    const characters = source.match(/LOUD_CHARACTERS = frozenset\(\{([^}]*)\}\)/);
    expect(keys && characters, "the server's allowed fields moved").toBeTruthy();
    const serverKeys = [...(keys as RegExpMatchArray)[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
    const serverCharacters = [...(characters as RegExpMatchArray)[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);

    for (const character of ["voice_like", "broadband"] as const) {
      const detector = new SoundLevelDetector(CONFIG, () => character);
      const start = drive(detector, 0, 11, () => -60);
      const { raised } = drive(detector, start.next, 10, () => -10);
      const metadata = raised[0]!.metadata as Record<string, unknown>;
      for (const key of Object.keys(metadata)) {
        expect([...serverKeys, "character"], key).toContain(key);
      }
      expect(serverCharacters).toContain(metadata.character);
    }
  });
});
