export type Platform = "youtube" | "instagram" | "yt_inst";

const TOKEN_KEY = "zaliver_api_token";
const BASE_KEY = "zaliver_api_base";

export type AuthUser = {
  username: string;
  locale: string;
  is_admin?: boolean;
};

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(token: string): void {
  const t = token.trim();
  if (!t) {
    localStorage.removeItem(TOKEN_KEY);
    return;
  }
  localStorage.setItem(TOKEN_KEY, t);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

/** Always same-origin; custom API base UI removed. */
export function getApiBase(): string {
  try {
    localStorage.removeItem(BASE_KEY);
  } catch {
    /* ignore */
  }
  return "";
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
    if (res.status === 401 && auth) {
      clearToken();
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
  linked_upload_job_id?: string;
  upload_followup_active?: boolean;
  upload_followup_min_ready?: number;
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
  session_id?: number;
  view_count?: number | null;
  like_count?: number | null;
  comment_count?: number | null;
  stats_updated_at?: string | null;
  stats_unavailable?: boolean;
  stats_unavailable_data_api?: boolean;
  age_restricted?: boolean | null;
};

export type UploadSession = {
  id: number;
  started_at: string;
  planned_videos: number;
  processed_videos: number;
  uploaded_ok: number;
  ended_at: string | null;
  status: string;
};

export type AiPrompt = {
  id: string;
  title: string;
  text: string;
  builtin: boolean;
};

export type TitleVariable = {
  token: string;
  example: string;
  description: string;
};

export type RecentValues = {
  platform: string;
  upload_titles: string[];
  channel_name_fields: string[];
  channel_descriptions: string[];
  channel_link_titles: string[];
  channel_link_urls: string[];
  video_default_title_fields: string[];
  promote_comment_fields: string[];
};

export type Profile = {
  id: string;
  name: string;
  tags: unknown[];
  custom_data?: Record<string, unknown>;
};

export const api = {
  health: () => request<Health>("/health", {}, { auth: false }),
  login: (username: string, password: string) =>
    request<{ token: string; user: AuthUser }>(
      "/v1/auth/login",
      {
        method: "POST",
        body: JSON.stringify({ username, password }),
      },
      { auth: false },
    ),
  logout: () => request<{ ok: boolean }>("/v1/auth/logout", { method: "POST" }),
  me: () => request<AuthUser>("/v1/auth/me"),
  patchMe: (body: { locale?: string; password?: string }) =>
    request<AuthUser>("/v1/auth/me", {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  listUsers: () => request<AuthUser[]>("/v1/auth/users"),
  createUser: (body: {
    username: string;
    password: string;
    locale?: string;
    is_admin?: boolean;
  }) =>
    request<AuthUser>("/v1/auth/users", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteUser: (username: string) =>
    request<AuthUser>(`/v1/auth/users/${encodeURIComponent(username)}`, {
      method: "DELETE",
    }),
  getPlatform: () => request<{ platform: Platform }>("/v1/platform"),
  setPlatform: (platform: Platform) =>
    request<{ platform: Platform }>("/v1/platform", {
      method: "PUT",
      body: JSON.stringify({ platform }),
    }),
  getSettings: (keys?: string) =>
    request<{ platform: string; values: Record<string, unknown> }>(
      keys
        ? `/v1/settings?keys=${encodeURIComponent(keys)}`
        : "/v1/settings",
    ),
  patchSettings: (values: Record<string, unknown>) =>
    request<{ platform: string; values: Record<string, unknown> }>("/v1/settings", {
      method: "PATCH",
      body: JSON.stringify({ values }),
    }),
  listVideos: () => request<VideoItem[]>("/v1/library/videos?limit=200"),
  getOutputDirs: (platform?: Platform) =>
    request<{
      root: string;
      platform: string;
      dirs: Record<string, string>;
    }>(
      platform
        ? `/v1/library/output-dirs?platform=${encodeURIComponent(platform)}`
        : "/v1/library/output-dirs",
    ),
  listSources: (path = "", kind: "media" | "video" | "audio" | "all" = "media") =>
    request<{
      root: string;
      path: string;
      parent: string | null;
      disk_total: number | null;
      disk_used: number | null;
      disk_free: number | null;
      entries: Array<{
        name: string;
        path: string;
        is_dir: boolean;
        size: number | null;
        abs_path: string | null;
        created_at: string | null;
      }>;
    }>(
      `/v1/library/sources?path=${encodeURIComponent(path)}&kind=${encodeURIComponent(kind)}`,
    ),
  listOutput: (path = "", kind: "media" | "video" | "audio" | "all" = "all") =>
    request<{
      root: string;
      path: string;
      parent: string | null;
      disk_total: number | null;
      disk_used: number | null;
      disk_free: number | null;
      entries: Array<{
        name: string;
        path: string;
        is_dir: boolean;
        size: number | null;
        abs_path: string | null;
        created_at: string | null;
      }>;
    }>(
      `/v1/library/output?path=${encodeURIComponent(path)}&kind=${encodeURIComponent(kind)}`,
    ),
  deleteOutput: (paths: string[]) =>
    request<{ deleted: number }>("/v1/library/output/delete", {
      method: "POST",
      body: JSON.stringify({ paths }),
    }),
  downloadLibrary: async (
    area: "sources" | "output",
    paths: string[],
  ): Promise<void> => {
    const headers = new Headers();
    headers.set("Content-Type", "application/json");
    const token = getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const res = await fetch(
      `${getApiBase()}/v1/library/${area}/download`,
      {
        method: "POST",
        headers,
        body: JSON.stringify({ paths }),
      },
    );
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
    const blob = await res.blob();
    let filename = area === "output" ? "zaliver-output.zip" : "zaliver-files.zip";
    const cd = res.headers.get("Content-Disposition") || "";
    const m =
      /filename\*=UTF-8''([^;]+)|filename="([^"]+)"|filename=([^;]+)/i.exec(cd);
    if (m) {
      const raw = decodeURIComponent((m[1] || m[2] || m[3] || "").trim());
      if (raw) filename = raw;
    }
    const url = URL.createObjectURL(blob);
    try {
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } finally {
      URL.revokeObjectURL(url);
    }
  },
    uploadSources: async (files: File[], subdir = "uploads") => {
    const body = new FormData();
    for (const f of files) body.append("files", f);
    const headers = new Headers();
    const token = getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const res = await fetch(
      `${getApiBase()}/v1/library/sources/upload?subdir=${encodeURIComponent(subdir)}`,
      { method: "POST", headers, body },
    );
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
    return (await res.json()) as { paths: string[]; relative: string[] };
  },
  deleteSources: (paths: string[]) =>
    request<{ deleted: number }>("/v1/library/sources/delete", {
      method: "POST",
      body: JSON.stringify({ paths }),
    }),
  mkdirSources: (parent: string, name: string) =>
    request<{ path: string }>("/v1/library/sources/mkdir", {
      method: "POST",
      body: JSON.stringify({ parent, name }),
    }),
  mkdirOutput: (parent: string, name: string) =>
    request<{ path: string }>("/v1/library/output/mkdir", {
      method: "POST",
      body: JSON.stringify({ parent, name }),
    }),
  deleteVideos: (ids: number[]) =>
    request<{ deleted: number }>("/v1/library/videos/delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  listUploaded: (sessionId?: number | null) => {
    const q =
      sessionId != null && sessionId > 0
        ? `?limit=500&session_id=${sessionId}`
        : "?limit=500";
    return request<UploadedItem[]>(`/v1/library/uploaded${q}`);
  },
  listSessions: () =>
    request<UploadSession[]>("/v1/library/sessions?limit=200"),
  deleteUploaded: (body: {
    ids?: number[];
    filter?: "" | "unavailable" | "age_restricted";
  }) =>
    request<{ deleted: number }>("/v1/library/uploaded/delete", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  refreshUploadedStats: (body: Record<string, unknown> = {}) =>
    request<{ id: string; kind: string; status: string }>(
      "/v1/library/uploaded/refresh-stats",
      { method: "POST", body: JSON.stringify(body) },
    ),
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
  startUpload: (body: Record<string, unknown>) =>
    request<{ id: string; kind: string; status: string }>("/v1/jobs/upload", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  enqueueUpload: (
    jobId: string,
    body: { video_paths: string[]; title?: string; description?: string },
  ) =>
    request<{ enqueued: number; job_id: string }>(
      `/v1/jobs/upload/${encodeURIComponent(jobId)}/enqueue`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  uploadProducerDone: (jobId: string) =>
    request<{ ok: boolean; job_id: string }>(
      `/v1/jobs/upload/${encodeURIComponent(jobId)}/producer-done`,
      { method: "POST", body: "{}" },
    ),
  listProfiles: () =>
    request<{
      kind?: string;
      base_url?: string;
      count: number;
      profiles: Profile[];
    }>("/v1/antidetect/profiles"),
  startProfileJob: (path: string, body: Record<string, unknown>) =>
    request<{ id: string; kind: string; status: string }>(
      `/v1/jobs/profiles/${path}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),
  getAiPrompts: () =>
    request<{ prompts: AiPrompt[] }>("/v1/ai/prompts"),
  putAiPrompts: (prompts: AiPrompt[]) =>
    request<{ prompts: AiPrompt[] }>("/v1/ai/prompts", {
      method: "PUT",
      body: JSON.stringify({ prompts }),
    }),
  createAiPrompt: (title = "", text = "") =>
    request<AiPrompt>("/v1/ai/prompts", {
      method: "POST",
      body: JSON.stringify({ title, text }),
    }),
  deleteAiPrompt: (id: string) =>
    request<{ deleted: number }>(`/v1/ai/prompts/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  aiGenerate: (body: {
    prompt_id?: string;
    prompt_text?: string;
    reply_lines?: number;
  }) =>
    request<{ text: string }>("/v1/ai/generate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  titleVariables: () =>
    request<{
      variables: TitleVariable[];
      example: string;
      max_youtube_title_length: number;
    }>("/v1/title-variables"),
  listRecentValues: () => request<RecentValues>("/v1/recent-values"),
};
