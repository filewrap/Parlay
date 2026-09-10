import { describe, expect, it } from "vitest";
import { authoritativePosition, expiryFromDuration, formatTime, shouldSeek } from "./timeline";

const playback = { track: null, status: "playing" as const, position_seconds: 12, server_time: 100, queue: [] };
describe("authoritative timeline", () => {
  it("anchors playing position to server time", () => expect(authoritativePosition(playback, 105)).toBe(17));
  it("does not advance paused playback", () => expect(authoritativePosition({ ...playback, status: "paused" }, 105)).toBe(12));
  it("uses a drift tolerance", () => { expect(shouldSeek(10, 11)).toBe(false); expect(shouldSeek(10, 13)).toBe(true); });
  it("formats elapsed time", () => expect(formatTime(125.9)).toBe("2:05"));
  it("previews expiry from duration", () => expect(expiryFromDuration(300, 0).toISOString()).toBe("1970-01-01T00:05:00.000Z"));
});
