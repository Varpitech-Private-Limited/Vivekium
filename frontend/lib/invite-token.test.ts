import { describe, expect, it } from "vitest";

import { inviteToken } from "./invite-token";

describe("inviteToken", () => {
  it("reads a plain string", () => expect(inviteToken({ invite: "abc" })).toBe("abc"));
  it("takes the first of a repeated key", () =>
    expect(inviteToken({ invite: ["abc", "def"] })).toBe("abc"));
  it("falls back to the legacy key", () =>
    expect(inviteToken({ invite_code: "xyz" })).toBe("xyz"));
  it("is empty when absent or blank", () => {
    expect(inviteToken({})).toBe("");
    expect(inviteToken({ invite: "  " })).toBe("");
    expect(inviteToken({ invite: [] })).toBe("");
  });
});
