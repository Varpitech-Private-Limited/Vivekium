/**
 * Public demo is on only for the exact env string "true".
 * The helper is read on each call so a test can stub the flag.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

async function readDemoMode(value: string | undefined): Promise<boolean> {
  vi.resetModules();
  vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", value);
  const { demoModeEnabled } = await import("./demo-mode");
  return demoModeEnabled();
}

describe("demoModeEnabled", () => {
  it("is true only when NEXT_PUBLIC_DEMO_MODE is the string true", async () => {
    expect(await readDemoMode("true")).toBe(true);
  });

  it.each([
    ["missing", undefined],
    ["empty", ""],
    ["false", "false"],
    ["FALSE", "FALSE"],
    ["True", "True"],
    ["TRUE", "TRUE"],
    ["1", "1"],
    ["yes", "yes"],
    ["a leading space", " true"],
    ["a trailing space", "true "],
  ] as const)("is false when NEXT_PUBLIC_DEMO_MODE is %s", async (_label, value) => {
    expect(await readDemoMode(value)).toBe(false);
  });

  it("reads NEXT_PUBLIC_DEMO_MODE again on each call", async () => {
    vi.resetModules();
    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "true");
    const { demoModeEnabled } = await import("./demo-mode");
    expect(demoModeEnabled()).toBe(true);

    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "false");
    expect(demoModeEnabled()).toBe(false);

    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", undefined);
    expect(demoModeEnabled()).toBe(false);

    vi.stubEnv("NEXT_PUBLIC_DEMO_MODE", "true");
    expect(demoModeEnabled()).toBe(true);
  });
});
