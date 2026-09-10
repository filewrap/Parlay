import type { Snapshot } from "./types";

export function authoritativePosition(playback: Snapshot["playback"], nowSeconds = Date.now() / 1000) {
  if (playback.status !== "playing") return Math.max(0, playback.position_seconds);
  return Math.max(0, playback.position_seconds + Math.max(0, nowSeconds - playback.server_time));
}

export function shouldSeek(local: number, authoritative: number, tolerance = 2.5) {
  return Math.abs(local - authoritative) > tolerance;
}

export function formatTime(seconds: number) {
  if (!Number.isFinite(seconds)) return "0:00";
  const safe = Math.max(0, Math.floor(seconds));
  return `${Math.floor(safe / 60)}:${String(safe % 60).padStart(2, "0")}`;
}

export function expiryFromDuration(durationSeconds: number, nowMs = Date.now()) {
  return new Date(nowMs + durationSeconds * 1000);
}
