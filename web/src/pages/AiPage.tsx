import { useCallback, useEffect, useState } from "react";
import { api, type AiPrompt, type Platform } from "../api/client";

type Props = { platform: Platform };

export function AiPage({ platform }: Props) {
  const [prompts, setPrompts] = useState<AiPrompt[]>([]);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  const refresh = useCallback(async () => {
    setError("");
    try {
      const r = await api.getAiPrompts();
      setPrompts(r.prompts);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [platform, refresh]);

  const updateLocal = (id: string, patch: Partial<AiPrompt>) => {
    setPrompts((prev) =>
      prev.map((p) => (p.id === id ? { ...p, ...patch } : p)),
    );
  };

  const saveAll = async () => {
    setError("");
    setStatus("");
    try {
      const r = await api.putAiPrompts(prompts);
      setPrompts(r.prompts);
      setStatus("Промпты сохранены.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const add = async () => {
    try {
      const p = await api.createAiPrompt();
      setPrompts((prev) => [...prev, p]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const remove = async (id: string) => {
    if (!confirm("Удалить промпт?")) return;
    try {
      await api.deleteAiPrompt(id);
      setPrompts((prev) => prev.filter((p) => p.id !== id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const exportJson = () => {
    const builtin = prompts.filter((p) => p.builtin);
    const custom = prompts.filter((p) => !p.builtin);
    const blob = new Blob(
      [
        JSON.stringify(
          {
            version: 1,
            builtin: builtin.map(({ id, title, text }) => ({ id, title, text })),
            custom: custom.map(({ id, title, text }) => ({ id, title, text })),
          },
          null,
          2,
        ),
      ],
      { type: "application/json" },
    );
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "zaliver-prompts.json";
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const importJson = async (file: File) => {
    try {
      const data = JSON.parse(await file.text()) as {
        builtin?: { id: string; title: string; text: string }[];
        custom?: { id: string; title: string; text: string }[];
      };
      const byId = new Map(prompts.map((p) => [p.id, p]));
      for (const b of data.builtin || []) {
        const cur = byId.get(b.id);
        if (cur) byId.set(b.id, { ...cur, text: b.text ?? cur.text });
      }
      const custom = (data.custom || []).map((c) => ({
        id: c.id,
        title: c.title || "Промпт",
        text: c.text || "",
        builtin: false,
      }));
      const next = [
        ...[...byId.values()].filter((p) => p.builtin),
        ...custom,
      ];
      const r = await api.putAiPrompts(next);
      setPrompts(r.prompts);
      setStatus("Импорт выполнен.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const builtin = prompts.filter((p) => p.builtin);
  const custom = prompts.filter((p) => !p.builtin);

  return (
    <div className="stack">
      <div className="page-header">
        <h1 className="title">ИИ</h1>
        <div className="row">
          <button type="button" className="btn secondary" onClick={exportJson}>
            Экспорт
          </button>
          <label className="btn secondary" style={{ display: "inline-block" }}>
            Импорт
            <input
              type="file"
              accept="application/json,.json"
              style={{ display: "none" }}
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) void importJson(f);
                e.target.value = "";
              }}
            />
          </label>
          <button type="button" className="btn secondary" onClick={add}>
            Добавить промпт
          </button>
          <button type="button" className="btn" onClick={saveAll}>
            Сохранить
          </button>
        </div>
      </div>
      <p className="hint">
        Встроенные промпты заданы в программе и не удаляются. Ниже можно
        добавлять свои. Параметры подключения — в «Настройки» → «ИИ».
      </p>
      {error ? <div className="error-banner">{error}</div> : null}
      {status ? <p className="hint">{status}</p> : null}

      <h3 className="group-title">Встроенные</h3>
      <div className="prompt-grid">
        {builtin.map((p) => (
          <div key={p.id} className="group stack">
            <div className="hint">{p.title}</div>
            <textarea
              className="field"
              rows={8}
              value={p.text}
              onChange={(e) => updateLocal(p.id, { text: e.target.value })}
              placeholder="Промпт…"
            />
          </div>
        ))}
      </div>

      <h3 className="group-title">
        {custom.length
          ? "Свои промпты"
          : "Свои промпты — пока нет, нажмите «Добавить промпт»"}
      </h3>
      <div className="prompt-grid">
        {custom.map((p) => (
          <div key={p.id} className="group stack">
            <div className="row">
              <input
                className="field"
                value={p.title}
                onChange={(e) => updateLocal(p.id, { title: e.target.value })}
                placeholder="Название…"
              />
              <button
                type="button"
                className="btn danger"
                onClick={() => remove(p.id)}
              >
                ×
              </button>
            </div>
            <textarea
              className="field"
              rows={8}
              value={p.text}
              onChange={(e) => updateLocal(p.id, { text: e.target.value })}
              placeholder="Промпт…"
            />
          </div>
        ))}
      </div>
    </div>
  );
}
