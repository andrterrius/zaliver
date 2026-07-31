import { useEffect, useState } from "react";
import { api, type Platform, type Profile } from "../api/client";
import { ToggleSwitch } from "../components/ToggleSwitch";

type Props = { platform: Platform };

function profileLabel(p: Profile): string {
  const name = (p.name || "").trim();
  return name ? `${name}  (${p.id})` : p.id;
}

export function SettingsPage({ platform }: Props) {
  const [localBase, setLocalBase] = useState("http://127.0.0.1:18765");
  const [remoteBase, setRemoteBase] = useState("");
  const [remoteCdpHost, setRemoteCdpHost] = useState("");
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
  const [statsUsername, setStatsUsername] = useState("");
  const [ytApiKey, setYtApiKey] = useState("");
  const [searchOldest, setSearchOldest] = useState(true);
  const [igPauseHours, setIgPauseHours] = useState(3);
  const [igTabs, setIgTabs] = useState(1);
  const [igChecker, setIgChecker] = useState("");
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [showYtKey, setShowYtKey] = useState(false);
  const [showAiKey, setShowAiKey] = useState(false);

  const showYt = platform === "youtube" || platform === "yt_inst";
  const showIg = platform === "instagram" || platform === "yt_inst";

  useEffect(() => {
    void (async () => {
      try {
        const [s, profRes] = await Promise.all([
          api.getSettings(),
          showIg
            ? api.listProfiles()
            : Promise.resolve({ profiles: [] as Profile[] }),
        ]);
        const v = s.values;
        if (showIg) {
          setProfiles(profRes.profiles || []);
        }
        setLocalBase(
          String(
            v["antydetect/local_api_base_url"] ||
              v["antydetect/own_base_url"] ||
              "http://127.0.0.1:18765",
          ),
        );
        setRemoteBase(String(v["antydetect/remote_api_base_url"] ?? ""));
        setRemoteCdpHost(
          String(
            v["antydetect/remote_cdp_public_host"] ||
              v["antydetect/own_remote_cdp_host"] ||
              "",
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
        setStatsUsername(
          String(v["stats_server/username"] || v["stats_server_username"] || ""),
        );
        setYtApiKey(String(v["youtube/api_key"] ?? ""));
        setSearchOldest(Boolean(v["youtube/search_oldest_channel"] ?? true));
        setIgPauseHours(Number(v["upload_pause_hours"] ?? 3));
        setIgTabs(Number(v["instagram/tabs_per_profile"] ?? 1));
        setIgChecker(String(v["instagram/stats_checker_profile_id"] ?? ""));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, [platform]);

  const save = async () => {
    setError("");
    setStatus("");
    try {
      const base = localBase.trim().replace(/\/$/, "");
      const values: Record<string, unknown> = {
        "antydetect/local_api_base_url": base,
        "antydetect/own_base_url": base,
        "antydetect/remote_api_base_url": remoteBase.trim(),
        "antydetect/remote_cdp_public_host": remoteCdpHost.trim(),
        "antydetect/own_remote_cdp_host": remoteCdpHost.trim(),
        "antydetect/dolphin_headless": headless,
        "antydetect/max_concurrent_browsers": maxBrowsers,
        "ai/base_url": aiBase,
        "ai/api_key": aiKey,
        "ai/model": aiModel,
        use_gpu_enabled: useGpu,
        use_gpu_finalize_enabled: useGpuFinalize,
        num_workers: workers,
        "slice/fps_mode": sliceFps,
        "stats_server/username": statsUsername,
        stats_server_username: statsUsername,
      };
      if (showYt) {
        values["youtube/api_key"] = ytApiKey;
        values["youtube/search_oldest_channel"] = searchOldest;
      }
      if (showIg) {
        values.upload_pause_hours = igPauseHours;
        values["instagram/tabs_per_profile"] = igTabs;
        values["instagram/stats_checker_profile_id"] = igChecker;
      }
      await api.patchSettings(values);
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
        <h3 className="group-title">Имя пользователя</h3>
        <p className="hint">Для сервера статистики загрузок.</p>
        <input
          className="field"
          value={statsUsername}
          onChange={(e) => setStatsUsername(e.target.value)}
          placeholder="username"
        />
      </section>

      <section className="group stack">
        <h3 className="group-title">Обработка видео</h3>
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
        <label className="hint">URL удалённого API (опционально)</label>
        <input
          className="field"
          value={remoteBase}
          onChange={(e) => setRemoteBase(e.target.value)}
        />
        <label className="hint">CDP public host</label>
        <input
          className="field"
          value={remoteCdpHost}
          onChange={(e) => setRemoteCdpHost(e.target.value)}
        />
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

      {showYt ? (
        <section className="group stack">
          <h3 className="group-title">YouTube</h3>
          <label className="hint">Data API key</label>
          <div className="row">
            <input
              className="field"
              type={showYtKey ? "text" : "password"}
              value={ytApiKey}
              onChange={(e) => setYtApiKey(e.target.value)}
              placeholder="AIza…"
            />
            <button
              type="button"
              className="btn secondary"
              onClick={() => setShowYtKey((v) => !v)}
            >
              {showYtKey ? "Скрыть" : "Показать"}
            </button>
          </div>
          <label className="check">
            <input
              type="checkbox"
              checked={searchOldest}
              onChange={(e) => setSearchOldest(e.target.checked)}
            />
            Искать старый канал
          </label>
        </section>
      ) : null}

      {showIg ? (
        <section className="group stack">
          <h3 className="group-title">Instagram</h3>
          <label className="hint">Пауза между заливами (часы)</label>
          <input
            className="field"
            style={{ maxWidth: 120 }}
            type="number"
            min={0}
            max={168}
            value={igPauseHours}
            onChange={(e) => setIgPauseHours(Number(e.target.value) || 0)}
          />
          {igPauseHours === 0 ? (
            <>
              <label className="hint">Вкладок на профиль</label>
              <input
                className="field"
                style={{ maxWidth: 120 }}
                type="number"
                min={1}
                max={10}
                value={igTabs}
                onChange={(e) => setIgTabs(Number(e.target.value) || 1)}
              />
            </>
          ) : null}
          <label className="hint">Профиль для чека статистики</label>
          <select
            className="field"
            value={igChecker}
            onChange={(e) => setIgChecker(e.target.value)}
          >
            <option value="">— не выбран —</option>
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {profileLabel(p)}
              </option>
            ))}
            {igChecker && !profiles.some((p) => p.id === igChecker) ? (
              <option value={igChecker}>{igChecker} (нет в списке)</option>
            ) : null}
          </select>
          {profiles.length === 0 ? (
            <p className="hint">
              Список пуст — загрузите профили на вкладке «Профили».
            </p>
          ) : null}
        </section>
      ) : null}

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
        <div className="row">
          <input
            className="field"
            type={showAiKey ? "text" : "password"}
            value={aiKey}
            onChange={(e) => setAiKey(e.target.value)}
            placeholder="Ключ API"
          />
          <button
            type="button"
            className="btn secondary"
            onClick={() => setShowAiKey((v) => !v)}
          >
            {showAiKey ? "Скрыть" : "Показать"}
          </button>
        </div>
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
