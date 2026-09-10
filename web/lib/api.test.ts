import { afterEach, describe, expect, it, vi } from "vitest";
import { parseResponse, quietHour, roomCandidate } from "./api";

afterEach(() => vi.restoreAllMocks());

describe("room selection", () => {
  it("prefers the server-validated start parameter", () => {
    Object.defineProperty(globalThis, "window", { value: { location: { search: "?room=fallback" } }, configurable: true });
    expect(roomCandidate("signed-room")).toBe("signed-room");
  });
  it("uses URL room only as an admission-neutral candidate", () => {
    Object.defineProperty(globalThis, "window", { value: { location: { search: "?room=fallback" } }, configurable: true });
    expect(roomCandidate(null)).toBe("fallback");
  });
});

describe("backend wire compatibility", () => {
  it("reads structured backend errors and stale snapshots", async () => {
    const snapshot = { id: "room", revision: 3 };
    const response = new Response(JSON.stringify({ error: { code: "stale_revision", message: "Refresh", snapshot } }), { status: 409 });
    await expect(parseResponse(response)).rejects.toMatchObject({ status: 409, code: "stale_revision", snapshot });
  });
  it("converts HTML time inputs to backend hour integers", () => {
    expect(quietHour("22:00")).toBe(22);
    expect(quietHour("08:30")).toBe(8);
    expect(quietHour("")).toBeNull();
  });
});
