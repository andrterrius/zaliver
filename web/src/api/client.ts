export type Platform = "youtube" | "instagram" | "yt_inst";

const TOKEN_KEY = "zaliver_api_token";
const BASE_KEY = "zaliver_api_base";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "secret";
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token.trim());
}

export function getApiBase(): string {
  // Same-origin when UI is served by FastAPI; Vite dev uses proxy.
  return localStorage.getItem(BASE_KEY) || "";
}

export function setApiBase(base: string): void {
  localStorage.setItem(BASE_KEY, base.trim());
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

type RequestOpts = {
  auth?: boolean;
};

async function request<T>(
  path: string,
  init: RequestInit = {},
  opts: RequestOpts = {},
): Promise<T> {
  const auth = opts.auth !== false;
  const headers = new Headers(init.headers || {});
  if (!headers.has("Content-Type") && init.body) {
    headers.set("Content-Type", "application/json");
  }
  if (auth) {
    const token = getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
  }
  const res = await fetch(`${getApiBase()}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail =
        typeof data.detail === "string"
          ? data.detail
          : JSON.stringify(data.detail);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail || `HTTP ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export type Health = {
  status: string;
  version: string;
  platform: string;
  browser_jobs_enabled: boolean;
  docs_enabled: boolean;
};

export type Job = {
  id: string;
  kind: string;
  status: string;
  progress: { current: number; total: number; message: string };
  message: string;
  outputs: string[];
  error: string;
  logs: string[];
};

export type VideoItem = {
  id: number;
  path: string;
  created_at: string;
  added_at: string;
  thumb_path: string | null;
};

export type UploadedItem = {
  id: number;
  platform: string;
  title: string;
  description: string;
  url: string;
  video_id: string;
  profile_id: string;
  uploaded_at: string;
};

export const api = {
  health: () => request<Health>("/health", {}, { auth: false }),
  getPlatform: () => request<{ platform: Platform }>("/v1/platform"),
  setPlatform: (platform: Platform) =>
    request<{ platform: Platform }>("/v1/platform", {
      method: "PUT",
      body: JSON.stringify({ platform }),
    }),
  getSettings: () =>
    request<{ platform: string; values: Record<string, unknown> }>("/v1/settings"),
  patchSettings: (values: Record<string, unknown>) =>
    request<{ platform: string; values: Record<string, unknown> }>("/v1/settings", {
      method: "PATCH",
      body: JSON.stringify({ values }),
    }),
  listVideos: () => request<VideoItem[]>("/v1/library/videos?limit=200"),
  listUploaded: () => request<UploadedItem[]>("/v1/library/uploaded?limit=200"),
  listJobs: () => request<{ jobs: Job[] }>("/v1/jobs?limit=30"),
  getJob: (id: string, logTail = 200) =>
    request<Job>(`/v1/jobs/${id}?log_tail=${logTail}`),
  cancelJob: (id: string) =>
    request<Job>(`/v1/jobs/${id}/cancel`, { method: "POST" }),
  startUniquify: (body: Record<string, unknown>) =>
    request<{ id: string; kind: string; status: string }>("/v1/jobs/uniquify", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  startSlicing: (body: Record<string, unknown>) =>
    request<{ id: string; kind: string; status: string }>("/v1/jobs/slicing", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  startStitching: (body: Record<string, unknown>) =>
    request<{ id: string; kind: string; status: string }>("/v1/jobs/stitching", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listProfiles: () =>
    request<{
      kind?: string;
      base_url?: string;
      count: number;
      profiles: {
        id: string;
        name: string;
        tags: unknown[];
        custom_data?: Record<string, unknown>;
      }[];
    }>("/v1/antidetect/profiles"),
  startProfileJob: (path: string, body: Record<string, unknown>) =>
    request<{ id: string; kind: string; status: string }>(
      `/v1/jobs/profiles/${path}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),
};
