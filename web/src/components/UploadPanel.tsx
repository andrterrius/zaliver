import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Profile } from "../api/client";
import { useJobPoll } from "../hooks/useJobPoll";
import { usePersistedJobId } from "../hooks/usePersistedJobId";
import { ProgressBar } from "./ProgressBar";
import { JobLogBox } from "./JobLogBox";
import { TitleVariablesHint } from "./TitleVariablesHint";

type Props = {
  videoPaths: string[];
  onClose?: () => void;
  defaultOpen?: boolean;
};

export function UploadPanel({
  videoPaths,
  onClose,
  defaultOpen = true,
}: Props) {
  const [open, setOpen] = useState(defaultOpen);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [headless, setHeadless] = useState(true);
  const [maxBrowsers, setMaxBrowsers] = useState(3);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [jobId, setJobId] = usePersistedJobId("upload");
  const { job } = useJobPoll(jobId);

  useEffect(() => {
    void (async () => {
      try {
        const res = await api.listProfiles();
        setProfiles(res.profiles || []);
        const s = await api.getSettings();
        setHeadless(Boolean(s.values["antydetect/dolphin_headless"] ?? true));
        setMaxBrowsers(
          Number(s.values["antydetect/max_concurrent_browsers"] ?? 3) || 3,
        );
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, []);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return profiles;
    return profiles.filter(
      (p) =>
        p.id.toLowerCase().includes(q) ||
        (p.name || "").toLowerCase().includes(q) ||
        JSON.stringify(p.tags || [])
          .toLowerCase()
          .includes(q),
    );
  }, [profiles, search]);

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const start = useCallback(async () => {
    setError("");
    const ids = [...selected];
    if (!ids.length) {
      setError("Выберите профили.");
      return;
    }
    if (!videoPaths.length) {
      setError("Нет путей к видео.");
      return;
    }
    try {
      const res = await api.startUpload({
        profile_ids: ids,
        video_paths: videoPaths,
        title,
        description,
        headless,
        max_concurrent_browsers: maxBrowsers,
        kind: "local",
      });
      setJobId(res.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [selected, videoPaths, title, description, headless, maxBrowsers]);

  if (!open) {
    return (
      <button type="button" className="btn secondary" onClick={() => setOpen(true)}>
        Залить…
      </button>
    );
  }

  return (
    <section className="group stack">
      <div className="page-header">
        <h3 className="group-title">Залив</h3>
        <button
          type="button"
          className="btn secondary"
          onClick={() => {
            setOpen(false);
            onClose?.();
          }}
        >
          Скрыть
        </button>
      </div>
      {error ? <div className="error-banner">{error}</div> : null}
      <p className="hint">Видео: {videoPaths.length}</p>
      <label className="hint">
        Название <TitleVariablesHint onInsert={(t) => setTitle((v) => v + t)} />
      </label>
      <input
        className="field"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Название ({date}, {index}…)"
      />
      <label className="hint">Описание</label>
      <textarea
        className="field"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        rows={3}
      />
      <label className="check">
        <input
          type="checkbox"
          checked={headless}
          onChange={(e) => setHeadless(e.target.checked)}
        />
        Headless
      </label>
      <label className="hint">Параллельных браузеров</label>
      <input
        className="field"
        style={{ maxWidth: 100 }}
        type="number"
        min={1}
        max={10}
        value={maxBrowsers}
        onChange={(e) => setMaxBrowsers(Number(e.target.value) || 1)}
      />
      <label className="hint">Поиск профилей</label>
      <input
        className="field"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="имя / id / тег"
      />
      <div className="row">
        <button
          type="button"
          className="btn secondary"
          onClick={() => setSelected(new Set(filtered.map((p) => p.id)))}
        >
          Выделить видимые
        </button>
        <button
          type="button"
          className="btn secondary"
          onClick={() => setSelected(new Set())}
        >
          Снять
        </button>
      </div>
      <div className="list-panel" style={{ maxHeight: 220, overflow: "auto" }}>
        {filtered.map((p) => (
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
        ))}
      </div>
      <div className="row">
        <button type="button" className="btn" onClick={start}>
          Старт залива
        </button>
      </div>
      {job ? (
        <>
          <ProgressBar
            value={job.progress.current}
            max={Math.max(1, job.progress.total)}
            label={`${job.status} ${job.progress.current}/${job.progress.total}`}
          />
          {job.logs?.length ? (
            <JobLogBox lines={job.logs.slice(-30)} emptyHint="" />
          ) : null}
        </>
      ) : null}
    </section>
  );
}
