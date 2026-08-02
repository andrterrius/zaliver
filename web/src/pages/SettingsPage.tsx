import { useEffect, useState } from "react";
import {
  api,
  type AuthUser,
  type Platform,
  type Profile,
} from "../api/client";
import { ToggleSwitch } from "../components/ToggleSwitch";
import {
  getStoredLocale,
  setStoredLocale,
  t,
  type Locale,
} from "../i18n";

type Props = {
  platform: Platform;
  locale: Locale;
  onLocaleChange: (locale: Locale) => void;
  user: AuthUser;
};

function profileLabel(p: Profile): string {
  const name = (p.name || "").trim();
  return name ? `${name}  (${p.id})` : p.id;
}

export function SettingsPage({
  platform,
  locale,
  onLocaleChange,
  user,
}: Props) {
  const [localBase, setLocalBase] = useState("http://127.0.0.1:18765");
  const [localToken, setLocalToken] = useState("secret");
  const [remoteBase, setRemoteBase] = useState("");
  const [remoteCdpHost, setRemoteCdpHost] = useState("");
  const [headless, setHeadless] = useState(true);
  const [maxBrowsers, setMaxBrowsers] = useState(5);
  const [aiBase, setAiBase] = useState("");
  const [aiKey, setAiKey] = useState("");
  const [aiModel, setAiModel] = useState("");
  const [useGpu, setUseGpu] = useState(false);
  const [useGpuFinalize, setUseGpuFinalize] = useState(false);
  const [sliceFps, setSliceFps] = useState("30");
  const [statsUsername, setStatsUsername] = useState("");
  const [ytApiKey, setYtApiKey] = useState("");
  const [searchOldest, setSearchOldest] = useState(false);
  const [igPauseHours, setIgPauseHours] = useState(3);
  const [igTabs, setIgTabs] = useState(1);
  const [igChecker, setIgChecker] = useState("");
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [showYtKey, setShowYtKey] = useState(false);
  const [showAiKey, setShowAiKey] = useState(false);
  const [showLocalToken, setShowLocalToken] = useState(false);
  const [newPassword, setNewPassword] = useState("");
  const [newUserName, setNewUserName] = useState("");
  const [newUserPass, setNewUserPass] = useState("");
  const [users, setUsers] = useState<AuthUser[]>([]);

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
        setLocalToken(String(v["antydetect/local_api_token"] ?? "secret"));
        setRemoteBase(String(v["antydetect/remote_api_base_url"] ?? ""));
        setRemoteCdpHost(
          String(
            v["antydetect/remote_cdp_public_host"] ||
              v["antydetect/own_remote_cdp_host"] ||
              "",
          ),
        );
        setHeadless(Boolean(v["antydetect/dolphin_headless"] ?? true));
        setMaxBrowsers(
          Math.max(
            1,
            Math.min(5, Number(v["antydetect/max_concurrent_browsers"] ?? 5) || 5),
          ),
        );
        setAiBase(String(v["ai/base_url"] ?? ""));
        setAiKey(String(v["ai/api_key"] ?? ""));
        setAiModel(String(v["ai/model"] ?? ""));
        setUseGpu(Boolean(v["use_gpu_enabled"] ?? false));
        setUseGpuFinalize(Boolean(v["use_gpu_finalize_enabled"] ?? false));
        const fps = String(v["slice/fps_mode"] ?? "30") || "30";
        setSliceFps(fps === "60" ? "60" : "30");
        setStatsUsername(
          String(v["stats_server/username"] || v["stats_server_username"] || ""),
        );
        setYtApiKey(String(v["youtube/api_key"] ?? ""));
        setSearchOldest(Boolean(v["youtube/search_oldest_channel"] ?? false));
        setIgPauseHours(Number(v["upload_pause_hours"] ?? 3));
        setIgTabs(Number(v["instagram/tabs_per_profile"] ?? 1));
        setIgChecker(String(v["instagram/stats_checker_profile_id"] ?? ""));
        if (user.is_admin) {
          try {
            setUsers(await api.listUsers());
          } catch {
            /* ignore */
          }
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, [platform, user.is_admin]);

  const save = async () => {
    setError("");
    setStatus("");
    try {
      const base = localBase.trim().replace(/\/$/, "");
      const values: Record<string, unknown> = {
        "antydetect/local_api_base_url": base,
        "antydetect/own_base_url": base,
        "antydetect/local_api_token": localToken,
        "antydetect/remote_api_base_url": remoteBase.trim(),
        "antydetect/remote_cdp_public_host": remoteCdpHost.trim(),
        "antydetect/own_remote_cdp_host": remoteCdpHost.trim(),
        "antydetect/dolphin_headless": headless,
        "antydetect/max_concurrent_browsers": Math.max(1, Math.min(5, maxBrowsers)),
        "ai/base_url": aiBase,
        "ai/api_key": aiKey,
        "ai/model": aiModel,
        use_gpu_enabled: useGpu,
        use_gpu_finalize_enabled: useGpuFinalize,
        "slice/fps_mode": sliceFps,
        "stats_server/username": statsUsername,
        stats_server_username: statsUsername,
        "ui/locale": locale,
      };
      if (showYt) {
        values["youtube/api_key"] = ytApiKey;
        values["youtube/search_oldest_channel"] = searchOldest;
      }
      if (showIg) {
        values.upload_pause_hours = igPauseHours;
        values.upload_pause_minutes = Math.max(0, Math.floor(igPauseHours) * 60);
        values["instagram/tabs_per_profile"] = igTabs;
        values["instagram/stats_checker_profile_id"] = igChecker;
      }
      await api.patchSettings(values);
      const mePatch: { locale: string; password?: string } = { locale };
      if (newPassword.trim()) {
        mePatch.password = newPassword.trim();
      }
      await api.patchMe(mePatch);
      setNewPassword("");
      setStoredLocale(locale);
      onLocaleChange(locale);
      setStatus(t("saved", locale));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const createUser = async () => {
    setError("");
    try {
      await api.createUser({
        username: newUserName.trim(),
        password: newUserPass,
        locale: getStoredLocale(),
      });
      setNewUserName("");
      setNewUserPass("");
      setUsers(await api.listUsers());
      setStatus(t("saved", locale));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="stack">
      <h1 className="title">{t("settings", locale)}</h1>
      {error ? <div className="error-banner">{error}</div> : null}
      {status ? <p className="hint">{status}</p> : null}

      <section className="group stack">
        <h3 className="group-title">{t("account", locale)}</h3>
        <p className="hint">{user.username}</p>
        <label className="hint">{t("language", locale)}</label>
        <select
          className="field"
          style={{ maxWidth: 200 }}
          value={locale}
          onChange={(e) =>
            onLocaleChange(e.target.value === "en" ? "en" : "ru")
          }
        >
          <option value="ru">{t("localeRu", locale)}</option>
          <option value="en">{t("localeEn", locale)}</option>
        </select>
        <label className="hint">{t("newPassword", locale)}</label>
        <input
          className="field"
          type="password"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          autoComplete="new-password"
          placeholder="••••••••"
        />
      </section>

      {user.is_admin ? (
        <section className="group stack">
          <h3 className="group-title">{t("users", locale)}</h3>
          <ul className="hint">
            {users.map((u) => (
              <li key={u.username}>
                {u.username}
                {u.is_admin ? " (admin)" : ""}
              </li>
            ))}
          </ul>
          <label className="hint">{t("createUser", locale)}</label>
          <div className="row">
            <input
              className="field"
              value={newUserName}
              onChange={(e) => setNewUserName(e.target.value)}
              placeholder={t("username", locale)}
            />
            <input
              className="field"
              type="password"
              value={newUserPass}
              onChange={(e) => setNewUserPass(e.target.value)}
              placeholder={t("password", locale)}
            />
            <button type="button" className="btn secondary" onClick={createUser}>
              {t("createUser", locale)}
            </button>
          </div>
        </section>
      ) : null}

      <section className="group stack">
        <h3 className="group-title">Имя пользователя</h3>
        <input
          className="field"
          value={statsUsername}
          onChange={(e) => setStatsUsername(e.target.value)}
          placeholder="username"
        />
      </section>

      <section className="group stack">
        <h3 className="group-title">{t("processing", locale)}</h3>
        <p className="hint">{t("processingHint", locale)}</p>
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
        <label className="hint">Bearer-токен API антидетекта</label>
        <p className="hint">
          Нужен для режима <code>serve</code> (по умолчанию <code>secret</code>).
          Десктоп Qt без токена можно оставить пустым.
        </p>
        <div className="row">
          <input
            className="field"
            type={showLocalToken ? "text" : "password"}
            value={localToken}
            onChange={(e) => setLocalToken(e.target.value)}
            placeholder="secret"
            autoComplete="off"
          />
          <button
            type="button"
            className="btn secondary"
            onClick={() => setShowLocalToken((v) => !v)}
          >
            {showLocalToken ? "Скрыть" : "Показать"}
          </button>
        </div>
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
        <label className="hint">{t("maxBrowsers", locale)}</label>
        <p className="hint">{t("browsersHint", locale)}</p>
        <input
          className="field"
          style={{ maxWidth: 120 }}
          type="number"
          min={1}
          max={5}
          value={maxBrowsers}
          onChange={(e) =>
            setMaxBrowsers(Math.max(1, Math.min(5, Number(e.target.value) || 1)))
          }
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
          {t("save", locale)}
        </button>
      </div>
    </div>
  );
}
