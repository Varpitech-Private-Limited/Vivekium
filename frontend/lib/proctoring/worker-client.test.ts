// The main-thread side of the inference workers: initialisation errors and
// error replies (mission item A, 2026-11-01).
//
// A model that fails to load must reject `ready()` (the system check turns
// that into "the monitoring could not start"), and a worker that answers every
// frame with an error must NOT count as a responsive model, or the heartbeat
// would report `models: true` while no frame was being analysed.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { InferenceClient, WORKER_SCRIPTS } from "./worker-client";

class FakeWorker {
  static instances: FakeWorker[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  posted: unknown[] = [];
  constructor(public readonly url: string) {
    FakeWorker.instances.push(this);
  }
  postMessage(message: unknown): void {
    this.posted.push(message);
  }
  terminate(): void {
    // nothing to release
  }
  reply(data: unknown): void {
    this.onmessage?.({ data } as MessageEvent);
  }
}

function build() {
  const client = new InferenceClient(["cell phone", "person"], 2);
  const cv = FakeWorker.instances.find((w) => w.url === WORKER_SCRIPTS.cv)!;
  const identity = FakeWorker.instances.find((w) => w.url === WORKER_SCRIPTS.identity)!;
  return { client, cv, identity };
}

beforeEach(() => {
  FakeWorker.instances = [];
  vi.stubGlobal("Worker", FakeWorker);
  vi.useFakeTimers();
  vi.setSystemTime(Date.UTC(2026, 10, 1, 10, 0, 0));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("initialisation", () => {
  it("is ready only when both workers say so, and reports the backend", async () => {
    const { client, cv, identity } = build();
    cv.reply({ type: "ready", tfBackend: "webgl", landmarkerDelegate: "GPU" });
    identity.reply({ type: "ready" });
    await expect(client.ready()).resolves.toEqual({ tfBackend: "webgl", landmarkerDelegate: "GPU" });
  });

  it("rejects when a model fails to load, so the candidate is told monitoring cannot start", async () => {
    const { client, cv, identity } = build();
    cv.reply({ type: "error", message: "model.json could not be fetched" });
    identity.reply({ type: "ready" });
    await expect(client.ready()).rejects.toThrow("model.json could not be fetched");
    expect(client.responsive(60_000)).toBe(false);
  });

  it("rejects when the worker script itself fails to load (a missing bundle)", async () => {
    const { client, cv, identity } = build();
    identity.reply({ type: "ready" });
    cv.onerror?.({ message: "Failed to load /proctoring/cv.worker.js" } as ErrorEvent);
    await expect(client.ready()).rejects.toThrow("Failed to load");
    expect(client.responsive(60_000)).toBe(false);
  });
});

describe("responsiveness", () => {
  it("is true after a successful detection and stays true inside the window", async () => {
    const { client, cv } = build();
    const pending = client.detect({ close: () => undefined } as ImageBitmap, 0);
    const id = (cv.posted[1] as { id: number }).id;
    cv.reply({
      type: "detections", id, objects: [], faces: 1, persons: 1, faceObservations: [],
      luminance: 100, variance: 40, inferenceMs: 30,
    });
    await pending;
    expect(client.responsive(10_000)).toBe(true);
    vi.advanceTimersByTime(11_000);
    expect(client.responsive(10_000)).toBe(false);
  });

  it("is NOT kept alive by a worker that answers every frame with an error", async () => {
    const { client, cv } = build();
    const pending = client.detect({ close: () => undefined } as ImageBitmap, 0);
    const id = (cv.posted[1] as { id: number }).id;
    cv.reply({ type: "error", id, message: "inference failed" });
    await expect(pending).rejects.toThrow("inference failed");
    // No success has ever been seen, so the models are not responsive.
    expect(client.responsive(10_000)).toBe(false);
  });
});
