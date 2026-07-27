import { useEffect, useState } from "react";
import { api } from "../api/client";

type BrowserKind = "local" | "remote" | "dolphin";

export function SettingsPage() {
  const [browserKind, setBrowserKind] = useState<BrowserKind>("local");
  const [localBase, setLocalBase] = useState("http://127.0.0.1:18765");
  const [token, setTok] = useState("");
  const [headless, setHeadless] = useState(true);
  const [maxBrowsers, setMaxBrowsers] = useState(5);
  const [aiBase, setAiBase] = useState("");
  const [aiKey, setAiKey] = useState("");
  const [aiModel, setAiModel] = useState("");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    void (async () => {
      try {
        const s = await api.getSettings();
        const v = s.values;
        const kind = String(v["antydetect/default_browser"] ?? "local").toLowerCase();
        setBrowserKind(
          kind === "dolphin" || kind === "remote" || kind === "local"
            ? kind
            : "local",
        );
        setLocalBase(
          String(
            v["antydetect/local_api_base_url"] ||
              v["antydetect/own_base_url"] ||
              "http://127.0.0.1:18765",
          ),
        );
        setTok(String(v["antydetect/dolphin_token"] ?? ""));
        setHeadless(Boolean(v["antydetect/dolphin_headless"] ?? true));
        setMaxBrowsers(Number(v["antydetect/max_concurrent_browsers"] ?? 5));
        setAiBase(String(v["ai/base_url"] ?? ""));
        setAiKey(String(v["ai/api_key"] ?? ""));
        setAiModel(String(v["ai/model"] ?? ""));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, []);

  const save = async () => {
    setError("");
    setStatus("");
    try {
      const base = localBase.trim().replace(/\/$/, "");
      await api.patchSettings({
        "antydetect/default_browser": browserKind,
        "antydetect/local_api_base_url": base,
        "antydetect/own_base_url": base,
        "antydetect/dolphin_token": token,
        "antydetect/dolphin_headless": headless,
        "antydetect/max_concurrent_browsers": maxBrowsers,
        "ai/base_url": aiBase,
        "ai/api_key": aiKey,
        "ai/model": aiModel,
      });
      setStatus("Настройки сохранены.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const ownMode = browserKind === "local" || browserKind === "remote";

  return (
    <div className="stack">
      <h1 className="title">Настройки</h1>
      {error ? <div className="error-banner">{error}</div> : null}
      {status ? <p className="hint">{status}</p> : null}

      <section className="group stack">
        <h3 className="group-title">Антидетект</h3>
        <label className="hint">Браузер по умолчанию</label>
        <select
          className="field"
          value={browserKind}
          onChange={(e) => setBrowserKind(e.target.value as BrowserKind)}
        >
          <option value="local">Свой (локальный API на сервере)</option>
          <option value="remote">Свой (удалённый API)</option>
          <option value="dolphin">Dolphin Anty</option>
        </select>

        {ownMode ? (
          <>
            <label className="hint">URL локального антидетекта</label>
            <input
              className="field"
              value={localBase}
              onChange={(e) => setLocalBase(e.target.value)}
              placeholder="http://127.0.0.1:18765"
            />
            <p className="hint">
              На сервере антидетект должен слушать этот адрес (рядом с Zaliver API).
            </p>
          </>
        ) : (
          <>
            <label className="hint">Dolphin token</label>
            <input
              className="field"
              value={token}
              onChange={(e) => setTok(e.target.value)}
              placeholder="Токен Dolphin Anty"
            />
          </>
        )}

        <label className="check">
          <input
            type="checkbox"
            checked={headless}
            onChange={(e) => setHeadless(e.target.checked)}
          />
          Headless
        </label>
        <label className="hint">Макс. параллельных браузеров</label>
        <input
          className="field"
          style={{ maxWidth: 120 }}
          type="number"
          min={1}
          max={10}
          value={maxBrowsers}
          onChange={(e) => setMaxBrowsers(Number(e.target.value) || 1)}
        />
      </section>

      <section className="group stack">
        <h3 className="group-title">ИИ</h3>
        <p className="hint">
          OpenAI-совместимый API (OpenAI, OpenRouter, локальный сервер).
        </p>
        <label className="hint">Base URL</label>
        <input
          className="field"
          value={aiBase}
          onChange={(e) => setAiBase(e.target.value)}
          placeholder="https://api.openai.com/v1"
        />
        <label className="hint">API key</label>
        <input
          className="field"
          value={aiKey}
          onChange={(e) => setAiKey(e.target.value)}
          placeholder="Ключ API"
        />
        <label className="hint">Модель</label>
        <input
          className="field"
          value={aiModel}
          onChange={(e) => setAiModel(e.target.value)}
          placeholder="gpt-4o-mini"
        />
      </section>

      <div className="row">
        <button type="button" className="btn" onClick={save}>
          Сохранить
        </button>
      </div>
    </div>
  );
}
