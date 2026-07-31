import { useEffect, useState } from "react";
import {
  api,
  getToken,
  setToken,
  type Platform,
} from "./api/client";
import { PlatformSelect } from "./components/PlatformSelect";
import { AppShell } from "./components/AppShell";
import { SourcesManagerModal } from "./components/SourcesManagerModal";

function platformLabel(platform: Platform): string {
  if (platform === "instagram") return "Instagram";
  if (platform === "yt_inst") return "Yt+Inst";
  return "YouTube";
}

export default function App() {
  const [platform, setPlatform] = useState<Platform | null>(null);
  const [token, setTok] = useState(getToken() || "secret");
  const [healthMsg, setHealthMsg] = useState("");
  const [error, setError] = useState("");
  const [sourcesOpen, setSourcesOpen] = useState(false);

  useEffect(() => {
    void api
      .health()
      .then((h) => setHealthMsg(`API ${h.version} · ${h.status}`))
      .catch((e) => setHealthMsg(e instanceof Error ? e.message : String(e)));
  }, [token]);

  const onChoose = async (p: Platform) => {
    setError("");
    setToken(token);
    try {
      await api.setPlatform(p);
      setPlatform(p);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="app-root">
      <div className="token-bar">
        <span className="brand-chip">
          Zaliver<span>.</span>
        </span>
        <input
          className="field"
          style={{ maxWidth: 260 }}
          value={token}
          onChange={(e) => setTok(e.target.value)}
          onBlur={() => setToken(token)}
          placeholder="API Bearer token"
        />
        <button
          type="button"
          className="icon-btn"
          title="Файлы на сервере (исходники и результаты)"
          aria-label="Файлы на сервере"
          onClick={() => setSourcesOpen(true)}
        >
          <svg
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            aria-hidden="true"
          >
            <path
              d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2h7.5A2.5 2.5 0 0 1 21 9.5v7A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5v-9Z"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinejoin="round"
            />
          </svg>
        </button>
        <span className="hint" style={{ marginLeft: "auto" }}>
          {healthMsg}
        </span>
        {platform ? (
          <span className="badge accent">{platformLabel(platform)}</span>
        ) : null}
      </div>
      {error ? (
        <div style={{ padding: "8px 16px" }}>
          <div className="error-banner">{error}</div>
        </div>
      ) : null}
      {platform == null ? (
        <PlatformSelect onChoose={onChoose} />
      ) : (
        <AppShell platform={platform} onBack={() => setPlatform(null)} />
      )}
      <SourcesManagerModal
        open={sourcesOpen}
        onClose={() => setSourcesOpen(false)}
      />
    </div>
  );
}
