import { useCallback, useEffect, useState } from "react";
import { api, type Platform, type VideoItem } from "../api/client";
import { UploadPanel } from "../components/UploadPanel";

type Props = { platform: Platform };

export function ReadyPage({ platform }: Props) {
  const [items, setItems] = useState<VideoItem[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [showUpload, setShowUpload] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setItems(await api.listVideos());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const removeSelected = async () => {
    const ids = [...selected];
    if (!ids.length) {
      setError("Выберите записи.");
      return;
    }
    if (!confirm(`Удалить из базы ${ids.length} записей? Файлы на диске не трогаются.`)) {
      return;
    }
    setError("");
    try {
      const r = await api.deleteVideos(ids);
      setStatus(`Удалено: ${r.deleted}`);
      setSelected(new Set());
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const uploadPaths = items
    .filter((v) => selected.has(v.id))
    .map((v) => v.path);

  return (
    <div className="stack">
      <div className="page-header">
        <h1 className="title">Готовые видео</h1>
        <div className="row">
          <button
            type="button"
            className="btn secondary"
            onClick={refresh}
            disabled={loading}
          >
            Обновить список
          </button>
          <button type="button" className="btn secondary" onClick={removeSelected}>
            Удалить выбранные…
          </button>
          <button
            type="button"
            className="btn"
            onClick={() => setShowUpload(true)}
            disabled={!uploadPaths.length}
          >
            Залить выбранные
          </button>
        </div>
      </div>
      {error ? <div className="error-banner">{error}</div> : null}
      {status ? <p className="hint">{status}</p> : null}
      {showUpload && uploadPaths.length ? (
        <UploadPanel
          videoPaths={uploadPaths}
          platform={platform}
          onClose={() => setShowUpload(false)}
        />
      ) : null}
      <div className="list-panel">
        {items.length === 0 ? (
          <div className="list-item hint">Пока нет готовых роликов в базе.</div>
        ) : (
          items.map((v) => (
            <label
              key={v.id}
              className={`list-item ${selected.has(v.id) ? "active" : ""}`}
              style={{ display: "flex", gap: 10, cursor: "pointer" }}
            >
              <input
                type="checkbox"
                checked={selected.has(v.id)}
                onChange={() => toggle(v.id)}
              />
              <span style={{ flex: 1 }}>
                <div style={{ fontWeight: 600, color: "var(--title)" }}>
                  {v.path.split(/[/\\]/).pop()}
                </div>
                <div className="hint">добавлено {v.added_at}</div>
              </span>
            </label>
          ))
        )}
      </div>
    </div>
  );
}
