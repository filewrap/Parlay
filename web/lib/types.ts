export type UserId = string | number;
export type Role = "owner" | "moderator" | "participant";
export type RoomState = "active" | "recovering" | "ended";
export type Track = { id: string; title: string; source_url: string; youtube_id?: string; duration?: number };
export type Member = { user_id: UserId; first_name: string; photo_url?: string; role: Role; avatar?: string; outfit?: string };
export type Presence = { user_id: UserId; x: number; z: number; rotation: number };
export type Snapshot = {
  id: string; kind: "personal" | "group"; owner_id: UserId; chat_id: number | null; revision: number;
  state: RoomState; expires_at: number | null;
  settings: { capacity: number; password_required: boolean; owner_lock: boolean; queue_all: boolean; theme: string; tv_size: string };
  members: Member[]; pending_reentry: UserId[];
  playback: { track: Track | null; status: "idle" | "playing" | "paused"; position_seconds: number; server_time: number; queue: Track[] };
  permissions: { manage_settings: boolean; queue: boolean; control: boolean; moderate: boolean; close: boolean };
};
export type Auth = { token: string; expires_at: number; user: { id: number; first_name: string; photo_url?: string }; start_param: string | null };
export type ApiErrorDetail = string | { message?: string; code?: string; snapshot?: Snapshot; [key: string]: unknown };
export type ConnectionState = "connecting" | "online" | "reconnecting" | "offline";

export function asUserId(value: UserId): number {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isSafeInteger(parsed)) throw new Error("Invalid user identifier");
  return parsed;
}

declare global {
  interface Window {
    Telegram?: { WebApp: { initData: string; ready(): void; expand(): void; disableVerticalSwipes?(): void; HapticFeedback?: { impactOccurred(style: string): void } } };
    YT?: { Player: new (element: HTMLElement, options: unknown) => YouTubePlayer; PlayerState: { PLAYING: number; PAUSED: number } };
    onYouTubeIframeAPIReady?: () => void;
  }
}
export type YouTubePlayer = { playVideo(): void; pauseVideo(): void; seekTo(seconds: number, allowSeekAhead: boolean): void; mute(): void; unMute(): void; setVolume(volume: number): void; getCurrentTime(): number; destroy(): void };
