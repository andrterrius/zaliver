import { useCallback, useEffect, useState } from "react";
import { api, type Platform } from "../api/client";
import { useJobPoll } from "../hooks/useJobPoll";
import { ProgressBar } from "../components/ProgressBar";

type Profile = {
  id: string;
  name: string;
  tags: unknown[];
  custom_data?: Record<string, unknown>;
};

const JOBS: { path: string; label: string; igOnly?: boolean }[] = [
  { path: "availability", label: "Проверка доступности" },
  { path: "instagram-register", label: "Регистрация IG", igOnly: true },
  { path: "instagram-2fa", label: "2FA IG", igOnly: true },
  { path: "warmup", label: "Прогрев" },
  { path: "promote", label: "Продвижение" },
  { path: "cookie-farm", label: "Фарм Cookie" },
  { path: "channel-setup", label: "Редактирование канала" },
];

type Props = { platform: Platform };

export function ProfilesPage({ platform }: Props) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [kind, setKind] = useState("local");
  const [baseUrl, setBaseUrl] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const { job } = useJobPoll(jobId);

  const refresh = useCallback(async () => {
    setError("");
    try {
      const res = await api.listProfiles();
      setProfiles(res.profiles);
      if (res.kind) setKind(String(res.kind));
      if (res.base_url) setBaseUrl(String(res.base_url));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const startJob = async (path: string) => {
    setError("");
    const ids = [...selected];
    if (!ids.length) {
      setError("Отметьте профили.");
      return;
    }
    const profiles_custom_data: Record<string, Record<string, unknown>> = {};
    for (const p of profiles) {
      if (ids.includes(p.id) && p.custom_data && Object.keys(p.custom_data).length) {
        profiles_custom_data[p.id] = p.custom_data;
      }
    }
    try {
      const res = await api.startProfileJob(path, {
        profile_ids: ids,
        kind,
        headless: true,
        ...(Object.keys(profiles_custom_data).length
          ? { profiles_custom_data }
          : {}),
      });
      setJobId(res.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const title =
    platform === "instagram"
      ? "Профили Instagram"
      : kind === "dolphin"
        ? "Профили Dolphin Anty"
        : "Профили антидетекта";

  return (
    <div className="stack">
      <div className="page-header">
        <h1 className="title">{title}</h1>
        <button type="button" className="btn secondary" onClick={refresh}>
          Обновить
        </button>
      </div>
      {baseUrl ? (
        <p className="hint">
          Режим: {kind}
          {baseUrl ? ` · ${baseUrl}` : ""}
        </p>
      ) : kind === "dolphin" ? (
        <p className="hint">Режим: dolphin</p>
      ) : null}
      {error ? <div className="error-banner">{error}</div> : null}
      {job ? (
        <ProgressBar
          value={job.progress.current}
          max={Math.max(1, job.progress.total)}
          label={`${job.kind}: ${job.status} ${job.progress.current}/${job.progress.total}`}
        />
      ) : null}

      <div className="row">
        {JOBS.filter((j) => !j.igOnly || platform === "instagram").map((j) => (
          <button
            key={j.path}
            type="button"
            className="btn secondary"
            onClick={() => startJob(j.path)}
          >
            {j.label}
          </button>
        ))}
      </div>

      <div className="list-panel">
        {profiles.length === 0 ? (
          <div className="list-item hint">
            Нет профилей. Убедитесь, что локальный антидетект запущен на сервере
            (по умолчанию http://127.0.0.1:18765) и в Настройках выбран «Свой».
          </div>
        ) : (
          profiles.map((p) => (
            <label
              key={p.id}
              className={`list-item ${selected.has(p.id) ? "active" : ""}`}
              style={{ display: "flex", gap: 10, cursor: "pointer" }}
            >
              <input
                type="checkbox"
                checked={selected.has(p.id)}
                onChange={() => toggle(p.id)}
              />
              <span>
                <div style={{ fontWeight: 600, color: "var(--title)" }}>
                  {p.name || p.id}
                </div>
                <div className="hint">{p.id}</div>
              </span>
            </label>
          ))
        )}
      </div>
      {job?.logs?.length ? (
        <div className="log-box">{job.logs.slice(-40).join("\n")}</div>
      ) : null}
    </div>
  );
}
