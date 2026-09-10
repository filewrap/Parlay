import type { ApiErrorDetail, Auth, PendingReentry, Snapshot, Track } from "./types";

export class ApiError extends Error {
  constructor(public status: number, public detail: ApiErrorDetail) {
    super(typeof detail === "string" ? detail : detail.message || detail.code || `Request failed (${status})`);
  }
  get snapshot(): Snapshot | undefined {
    return typeof this.detail === "object" ? this.detail.snapshot : undefined;
  }
  get code(): string | undefined {
    return typeof this.detail === "object" ? this.detail.code : undefined;
  }
}

const configured = process.env.NEXT_PUBLIC_BACKEND_URL?.replace(/\/$/, "");
export const backendUrl = configured || "";

export async function parseResponse(response: Response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.error ?? body.detail ?? body;
    throw new ApiError(response.status, detail);
  }
  return body;
}

export async function authenticate(initData: string): Promise<Auth> {
  return parseResponse(await fetch(`${backendUrl}/api/auth`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ init_data: initData }) }));
}

export function client(token: string) {
  const request = async (path: string, init: RequestInit = {}) => parseResponse(await fetch(`${backendUrl}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...init.headers },
  }));
  const actionRequest = (roomId: string, revision: number, action: string, payload: Record<string, unknown> = {}) => request(`/api/rooms/${encodeURIComponent(roomId)}/actions`, { method: "POST", body: JSON.stringify({ action_id: crypto.randomUUID(), expected_revision: revision, action, payload }) });
  return {
    join: (roomId: string, password?: string): Promise<Snapshot> => request(`/api/rooms/${encodeURIComponent(roomId)}/join`, { method: "POST", body: JSON.stringify({ password: password ?? "" }) }),
    snapshot: (roomId: string): Promise<Snapshot> => request(`/api/rooms/${encodeURIComponent(roomId)}`),
    action: (roomId: string, revision: number, action: string, payload: Record<string, unknown> = {}): Promise<Snapshot> => actionRequest(roomId, revision, action, payload),
    requestReentry: (roomId: string): Promise<PendingReentry> => actionRequest(roomId, 0, "request_reentry"),
    search: (query: string): Promise<{ tracks: Track[] }> => request(`/api/search?q=${encodeURIComponent(query)}`),
    ticket: (roomId: string): Promise<{ ticket: string }> => request("/api/ws-ticket", { method: "POST", body: JSON.stringify({ room_id: roomId }) }),
    compass: (): Promise<{ recommendations: Track[] }> => request("/api/compass"),
    compassPreferences: (payload: { enabled: boolean; count: number; quiet_start: number | null; quiet_end: number | null; timezone: string }) => request("/api/compass/preferences", { method: "POST", body: JSON.stringify(payload) }),
    compassFeedback: (track_id: string, positive: boolean, event_id: string) => request("/api/compass/feedback", { method: "POST", body: JSON.stringify({ track_id, positive, event_id }) }),
    compassReset: () => request("/api/compass/reset", { method: "POST", body: "{}" }),
    compassDelete: () => request("/api/compass/delete", { method: "POST", body: "{}" }),
  };
}

export function websocketUrl(roomId: string, ticket: string) {
  const base = new URL(backendUrl || window.location.origin);
  base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
  base.pathname = `/api/rooms/${encodeURIComponent(roomId)}/ws`;
  base.search = new URLSearchParams({ ticket }).toString();
  return base.toString();
}

export function roomCandidate(startParam?: string | null) {
  if (startParam) return startParam;
  return new URLSearchParams(window.location.search).get("room") || "";
}

export function quietHour(value: string): number | null {
  if (!value) return null;
  const match = /^(\d{2}):\d{2}$/.exec(value);
  if (!match) return null;
  const hour = Number(match[1]);
  return hour >= 0 && hour <= 23 ? hour : null;
}
