import { useEffect, useState } from "react";
import { api } from "../api/client";
import { ToggleSwitch } from "../components/ToggleSwitch";

export function SettingsPage() {
  const [localBase, setLocalBase] = useState("http://127.0.0.1:18765");
  const [headless, setHeadless] = useState(true);
  const [maxBrowsers, setMaxBrowsers] = useState(5);
  const [aiBase, setAiBase] = useState("");
  const [aiKey, setAiKey] = useState("");
  const [aiModel, setAiModel] = useState("");
  const [useGpu, setUseGpu] = useState(false);
  const [useGpuFinalize, setUseGpuFinalize] = useState(false);
  const [workers, setWorkers] = useState(
    Math.max(1, Math.min(32, navigator.hardwareConcurrency || 2)),
  );
  const [sliceFps, setSliceFps] = useState("30");
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    void (async () => {
      try {
        const s = await api.getSettings();
        const v = s.values;
        setLocalBase(
          String(
            v["antydetect/local_api_base_url"] ||
              v["antydetect/own_base_url"] ||
              "http://127.0.0.1:18765",
          ),
        );
        setHeadless(Boolean(v["antydetect/dolphin_headless"] ?? true));
        setMaxBrowsers(Number(v["antydetect/max_concurrent_browsers"] ?? 5));
        setAiBase(String(v["ai/base_url"] ?? ""));
        setAiKey(String(v["ai/api_key"] ?? ""));
        setAiModel(String(v["ai/model"] ?? ""));
        setUseGpu(Boolean(v["use_gpu_enabled"] ?? false));
        setUseGpuFinalize(Boolean(v["use_gpu_finalize_enabled"] ?? false));
        setWorkers(
          Math.max(
            1,
            Math.min(
              32,
              Number(v["num_workers"] ?? navigator.hardwareConcurrency ?? 2) || 2,
            ),
          ),
        );
        const fps = String(v["slice/fps_mode"] ?? "30") || "30";
        setSliceFps(fps === "60" ? "60" : "30");
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
        "antydetect/local_api_base_url": base,
        "antydetect/own_base_url": base,
        "antydetect/dolphin_headless": headless,
        "antydetect/max_concurrent_browsers": maxBrowsers,
        "ai/base_url": aiBase,
        "ai/api_key": aiKey,
        "ai/model": aiModel,
        use_gpu_enabled: useGpu,
        use_gpu_finalize_enabled: useGpuFinalize,
        num_workers: workers,
        "slice/fps_mode": sliceFps,
      });
      setStatus("Настройки сохранены.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="stack">
      <h1 className="title">Настройки</h1>
      {error ? <div className="error-banner">{error}</div> : null}
      {status ? <p className="hint">{status}</p> : null}

      <section className="group stack">
        <h3 className="group-title">Обработка видео</h3>
        <p className="hint">
          Общие параметры уникализации, нарезки и склейки.
        </p>
        <ToggleSwitch
          label="GPU при обработке кадров (декод, фильтры, кодирование)"
          checked={useGpu}
          onChange={setUseGpu}
        />
        <ToggleSwitch
          label="GPU при склейке и mux звука (concat, ускорение, фон/текст)"
          checked={useGpuFinalize}
          onChange={setUseGpuFinalize}
        />
        <p className="hint">
          Независимо друг от друга. Можно кадры на CPU, а склейку на GPU
          (NVENC/QSV/AMF).
        </p>
        <label className="hint">Потоков процессов</label>
        <input
          className="field"
          style={{ maxWidth: 120 }}
          type="number"
          min={1}
          max={32}
          value={workers}
          onChange={(e) =>
            setWorkers(Math.max(1, Math.min(32, Number(e.target.value) || 1)))
          }
        />
        <label className="hint">FPS нарезки</label>
        <select
          className="field"
          style={{ maxWidth: 160 }}
          value={sliceFps}
          onChange={(e) => setSliceFps(e.target.value)}
        >
          <option value="30">30 fps</option>
          <option value="60">60 fps</option>
        </select>
        <p className="hint">
          Только для нарезки. 60 fps вдвое медленнее рендера; для Shorts/Reels
          обычно достаточно 30.
        </p>
      </section>

      <section className="group stack">
        <h3 className="group-title">Антидетект</h3>

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
