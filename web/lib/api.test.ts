import { describe, expect, it } from "vitest";
import { roomCandidate } from "./api";

describe("room selection", () => {
  it("prefers the server-validated start parameter", () => {
    Object.defineProperty(globalThis, "window", { value: { location: { search: "?room=fallback" } }, configurable: true });
    expect(roomCandidate("signed-room")).toBe("signed-room");
  });
  it("uses URL room only as a candidate", () => {
    Object.defineProperty(globalThis, "window", { value: { location: { search: "?room=fallback" } }, configurable: true });
    expect(roomCandidate()).toBe("fallback");
  });
});
