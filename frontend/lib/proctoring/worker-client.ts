/**
 * The main-thread side of the two inference workers.
 *
 * Owns the Worker objects, correlates replies by id, and exposes three
 * promises: `detect` (a frame in, detections out), `baseline` (a frame in,
 * the 128-float descriptor out, and the worker keeps it) and `check` (a frame
 * in, the distance to the baseline out). A frame is an ImageBitmap that is
 * TRANSFERRED, so this thread gives it up the moment it is posted and could
 * not retain it if it tried. A frame sent to both workers is created twice,
 * once per transfer.
 *
 * The worker files are BUILT by `scripts/build-proctoring-workers.mjs` into
 * `public/proctoring/` and loaded here by path. The idiomatic
 * `new Worker(new URL("./worker.ts", import.meta.url))` is a webpack feature
 * and Next 16 builds with Turbopack, which resolves that URL as a static
 * asset: measured on this repo, the build served the browser untranspiled
 * TypeScript. The script's header carries the detail.
 */
import {
  MODEL_PATHS,
  type CvDetectionsMessage,
  type CvInbound,
  type CvOutbound,
  type CvReadyMessage,
  type IdentityInbound,
  type IdentityOutbound,
} from "./workers/protocol";

interface Pending<T> {
  resolve: (value: T) => void;
  reject: (error: Error) => void;
}

class Channel<Inbound, Outbound extends { type: string; id?: number }> {
  private readonly pending = new Map<number, Pending<Outbound>>();
  private nextId = 1;
  private readyPromise: Promise<Outbound>;
  private resolveReady!: (value: Outbound) => void;
  private rejectReady!: (error: Error) => void;
  /** The last SUCCESSFUL reply arrived, for the integrity check's "models"
   *  question. An error reply does not count: a worker that answers every
   *  frame with an error is not a working model, and counting its replies made
   *  the heartbeat report `models: true` while no frame was being analysed. */
  lastReplyAt: number | null = null;
  failed: Error | null = null;

  constructor(private readonly worker: Worker) {
    this.readyPromise = new Promise((resolve, reject) => {
      this.resolveReady = resolve;
      this.rejectReady = reject;
    });
    worker.onmessage = (event: MessageEvent<Outbound>) => {
      const message = event.data;
      if (message.type !== "error") this.lastReplyAt = Date.now();
      if (message.type === "ready") {
        this.resolveReady(message);
        return;
      }
      if (message.type === "error") {
        const error = new Error((message as unknown as { message: string }).message);
        if (message.id === undefined) {
          this.failed = error;
          this.rejectReady(error);
          for (const waiting of this.pending.values()) waiting.reject(error);
          this.pending.clear();
          return;
        }
        this.pending.get(message.id)?.reject(error);
        this.pending.delete(message.id);
        return;
      }
      if (message.id !== undefined) {
        this.pending.get(message.id)?.resolve(message);
        this.pending.delete(message.id);
      }
    };
    worker.onerror = (event) => {
      this.failed = new Error(event.message);
      this.rejectReady(this.failed);
    };
  }

  ready(): Promise<Outbound> {
    return this.readyPromise;
  }

  post(message: Inbound, transfer: Transferable[] = []): void {
    this.worker.postMessage(message, transfer);
  }

  request(build: (id: number) => Inbound, transfer: Transferable[]): Promise<Outbound> {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.worker.postMessage(build(id), transfer);
    });
  }

  terminate(): void {
    this.worker.terminate();
    for (const waiting of this.pending.values()) waiting.reject(new Error("worker terminated"));
    this.pending.clear();
  }
}

/**
 * Where the built worker bundles are served from. Classic workers, because
 * `@mediapipe/tasks-vision` loads its wasm glue with `importScripts`.
 * `lib/proctoring/model-assets.test.ts` asserts these are exactly what
 * `scripts/build-proctoring-workers.mjs` writes.
 */
export const WORKER_SCRIPTS = {
  cv: "/proctoring/cv.worker.js",
  identity: "/proctoring/identity.worker.js",
} as const;

export interface InferenceReady {
  tfBackend: string;
  landmarkerDelegate: "GPU" | "CPU";
}

export class InferenceClient {
  private readonly cv: Channel<CvInbound, CvOutbound>;
  private readonly identity: Channel<IdentityInbound, IdentityOutbound>;

  constructor(labels: string[], numFaces: number) {
    this.cv = new Channel(new Worker(WORKER_SCRIPTS.cv));
    this.identity = new Channel(new Worker(WORKER_SCRIPTS.identity));
    this.cv.post({
      type: "init",
      cocoModelUrl: MODEL_PATHS.cocoSsdModel,
      mediapipeWasmPath: MODEL_PATHS.mediapipeWasm,
      faceLandmarkerModelPath: MODEL_PATHS.faceLandmarkerModel,
      numFaces,
      labels,
    });
    this.identity.post({ type: "init", weightsPath: MODEL_PATHS.faceApiWeights });
  }

  async ready(): Promise<InferenceReady> {
    const [cv] = await Promise.all([this.cv.ready(), this.identity.ready()]);
    const ready = cv as CvReadyMessage;
    return { tfBackend: ready.tfBackend, landmarkerDelegate: ready.landmarkerDelegate };
  }

  /** Whether both workers are alive and the camera worker has answered
   *  within `withinMs`. */
  responsive(withinMs: number): boolean {
    if (this.cv.failed || this.identity.failed) return false;
    return this.cv.lastReplyAt !== null && Date.now() - this.cv.lastReplyAt <= withinMs;
  }

  async detect(bitmap: ImageBitmap, timestampMs: number): Promise<CvDetectionsMessage> {
    const reply = await this.cv.request((id) => ({ type: "frame", id, bitmap, timestampMs }), [bitmap]);
    return reply as CvDetectionsMessage;
  }

  async baseline(bitmap: ImageBitmap): Promise<number[] | null> {
    const reply = await this.identity.request((id) => ({ type: "baseline", id, bitmap }), [bitmap]);
    return (reply as { descriptor: number[] | null }).descriptor;
  }

  setBaseline(descriptor: number[]): void {
    this.identity.post({ type: "set-baseline", descriptor });
  }

  async check(bitmap: ImageBitmap): Promise<number | null> {
    const reply = await this.identity.request((id) => ({ type: "check", id, bitmap }), [bitmap]);
    return (reply as { distance: number | null }).distance;
  }

  terminate(): void {
    this.cv.terminate();
    this.identity.terminate();
  }
}
