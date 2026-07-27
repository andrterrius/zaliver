import { useCallback, useEffect, useState } from "react";
import { api, type VideoItem } from "../api/client";

export function ReadyPage() {
  const [items, setItems] = useState<VideoItem[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

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

  return (
    <div className="stack">
      <div className="page-header">
        <h1 className="title">Готовые видео</h1>
        <button type="button" className="btn secondary" onClick={refresh} disabled={loading}>
          Обновить
        </button>
      </div>
      {error ? <div className="error-banner">{error}</div> : null}
      <div className="list-panel">
        {items.length === 0 ? (
          <div className="list-item hint">Пока нет готовых роликов в базе.</div>
        ) : (
          items.map((v) => (
            <div key={v.id} className="list-item">
              <div style={{ fontWeight: 600, color: "var(--title)" }}>
                {v.path.split(/[/\\]/).pop()}
              </div>
              <div className="hint">{v.path}</div>
              <div className="hint">добавлено {v.added_at}</div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
