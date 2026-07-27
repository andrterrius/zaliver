import { useCallback, useEffect, useState } from "react";
import { api, type UploadedItem } from "../api/client";

export function UploadedPage() {
  const [items, setItems] = useState<UploadedItem[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setItems(await api.listUploaded());
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
        <h1 className="title">Залитые видео</h1>
        <button type="button" className="btn secondary" onClick={refresh} disabled={loading}>
          Обновить
        </button>
      </div>
      {error ? <div className="error-banner">{error}</div> : null}
      <div className="list-panel">
        {items.length === 0 ? (
          <div className="list-item hint">Нет записей о заливах.</div>
        ) : (
          items.map((v) => (
            <div key={v.id} className="list-item">
              <div style={{ fontWeight: 600, color: "var(--title)" }}>
                {v.title || v.video_id || "Без названия"}
              </div>
              <div className="hint">
                {v.uploaded_at} · профиль {v.profile_id || "—"}
              </div>
              {v.url ? (
                <a href={v.url} target="_blank" rel="noreferrer">
                  Открыть
                </a>
              ) : null}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
