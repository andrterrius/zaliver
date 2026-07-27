import { useEffect, useState } from "react";
import {
  api,
  getApiBase,
  getToken,
  setApiBase,
  setToken,
  type Platform,
} from "./api/client";
import { PlatformSelect } from "./components/PlatformSelect";
import { AppShell } from "./components/AppShell";

export default function App() {
  const [platform, setPlatform] = useState<Platform | null>(null);
  const [token, setTok] = useState(getToken() || "secret");
  const [base, setBase] = useState(getApiBase());
  const [healthMsg, setHealthMsg] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    void api
      .health()
      .then((h) => setHealthMsg(`API ${h.version} · ${h.status}`))
      .catch((e) => setHealthMsg(e instanceof Error ? e.message : String(e)));
  }, [base, token]);

  const onChoose = async (p: Platform) => {
    setError("");
    setToken(token);
    setApiBase(base);
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
        <span className="badge">Zaliver Web</span>
        <input
          className="field"
          style={{ maxWidth: 280 }}
          value={token}
          onChange={(e) => setTok(e.target.value)}
          onBlur={() => setToken(token)}
          placeholder="API Bearer token"
        />
        <input
          className="field"
          style={{ maxWidth: 200 }}
          value={base}
          onChange={(e) => setBase(e.target.value)}
          onBlur={() => setApiBase(base)}
          placeholder="API base (пусто = same origin)"
        />
        <span className="hint">{healthMsg}</span>
        {platform ? (
          <span className="badge">
            {platform === "instagram" ? "Instagram" : "YouTube"}
          </span>
        ) : null}
      </div>
      {error ? (
        <div style={{ padding: "8px 12px" }}>
          <div className="error-banner">{error}</div>
        </div>
      ) : null}
      {platform == null ? (
        <PlatformSelect onChoose={onChoose} />
      ) : (
        <AppShell platform={platform} onBack={() => setPlatform(null)} />
      )}
    </div>
  );
}
