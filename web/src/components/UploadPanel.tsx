import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Platform, type Profile } from "../api/client";
import { useJobPoll } from "../hooks/useJobPoll";
import { usePersistedJobId } from "../hooks/usePersistedJobId";
import { usePaintSelectList } from "../hooks/usePaintSelectList";
import { useRecentValues } from "../hooks/useRecentValues";
import { ProgressBar } from "./ProgressBar";
import { JobLogBox } from "./JobLogBox";
import { TitleVariablesHint } from "./TitleVariablesHint";
import { FieldWithRecent } from "./RecentValuesField";

type Props = {
  videoPaths: string[];
  onClose?: () => void;
  defaultOpen?: boolean;
  platform?: Platform;
};

export function UploadPanel({
  videoPaths,
  onClose,
  defaultOpen = true,
  platform = "youtube",
}: Props) {
  const [open, setOpen] = useState(defaultOpen);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [maxBrowsers, setMaxBrowsers] = useState(5);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [jobId, setJobId] = usePersistedJobId("upload");
  const { job } = useJobPoll(jobId);
  const { recent, refresh: refreshRecent } = useRecentValues(platform, open);

  useEffect(() => {
    void (async () => {
      try {
        const res = await api.listProfiles();
        setProfiles(res.profiles || []);
        const s = await api.getSettings();
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

  const paint = useCallback((id: string, paintSelect: boolean) => {
    setSelected((prev) => {
      const has = prev.has(id);
      if (paintSelect && has) return prev;
      if (!paintSelect && !has) return prev;
      const next = new Set(prev);
      if (paintSelect) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  const { listRef, onPointerDown } = usePaintSelectList({
    isSelected: (key) => selected.has(key),
    paint,
  });

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
        headless: true,
        max_concurrent_browsers: maxBrowsers,
        kind: "local",
      });
      setJobId(res.id);
      void refreshRecent();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [selected, videoPaths, title, description, maxBrowsers, refreshRecent]);

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
      <FieldWithRecent recent={recent.upload_titles} onSelect={setTitle}>
        <input
          className="field"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Название ({date}, {index}…)"
        />
      </FieldWithRecent>
      <label className="hint">Описание</label>
      <textarea
        className="field"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        rows={3}
      />
      <label className="hint">Параллельных браузеров</label>
      <input
        className="field"
        style={{ maxWidth: 100 }}
        type="number"
        min={1}
        max={5}
        value={maxBrowsers}
        onChange={(e) =>
          setMaxBrowsers(Math.max(1, Math.min(5, Number(e.target.value) || 1)))
        }
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
      <div
        ref={listRef}
        className="list-panel source-browser-list--select"
        style={{ maxHeight: 220, overflow: "auto" }}
        onPointerDown={onPointerDown}
      >
        {filtered.map((p) => (
          <div
            key={p.id}
            data-entry-path={p.id}
            className={`list-item ${selected.has(p.id) ? "active" : ""}`}
            style={{ display: "flex", gap: 10, cursor: "pointer" }}
          >
            <input
              type="checkbox"
              className="source-browser-check"
              checked={selected.has(p.id)}
              readOnly
              tabIndex={-1}
            />
            <span style={{ flex: 1 }}>
              <div style={{ fontWeight: 600, color: "var(--title)" }}>
                {p.name || p.id}
              </div>
              <div className="hint">{p.id}</div>
            </span>
          </div>
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
