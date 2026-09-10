import { afterEach, describe, expect, it, vi } from "vitest";
import { client, parseResponse, quietHour, roomCandidate } from "./api";

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
  it("sends snapshot-free re-entry with revision zero and accepts pending", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ status: "pending" }), { status: 200 }));
    await expect(client("token").requestReentry("room/id")).resolves.toEqual({ status: "pending" });
    const [, request] = fetchMock.mock.calls[0];
    expect(JSON.parse(String(request?.body))).toMatchObject({ expected_revision: 0, action: "request_reentry", payload: {} });
  });
  it("converts HTML time inputs to backend hour integers", () => {
    expect(quietHour("22:00")).toBe(22);
    expect(quietHour("08:30")).toBe(8);
    expect(quietHour("")).toBeNull();
  });
});
