import { useEffect, useState } from "react";
import {
  api,
  clearToken,
  getToken,
  type AuthUser,
  type Platform,
} from "./api/client";
import { PlatformSelect } from "./components/PlatformSelect";
import { AppShell } from "./components/AppShell";
import { SourcesManagerModal } from "./components/SourcesManagerModal";
import { LoginPage } from "./pages/LoginPage";
import { getStoredLocale, setStoredLocale, t, type Locale } from "./i18n";

function platformLabel(platform: Platform): string {
  if (platform === "instagram") return "Instagram";
  if (platform === "yt_inst") return "Yt+Inst";
  return "YouTube";
}

export default function App() {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [platform, setPlatform] = useState<Platform | null>(null);
  const [healthMsg, setHealthMsg] = useState("");
  const [error, setError] = useState("");
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [locale, setLocale] = useState<Locale>(getStoredLocale());

  useEffect(() => {
    void api
      .health()
      .then((h) => setHealthMsg(`API ${h.version} · ${h.status}`))
      .catch((e) => setHealthMsg(e instanceof Error ? e.message : String(e)));
  }, [user]);

  useEffect(() => {
    const token = getToken();
    if (!token) {
      setAuthChecked(true);
      return;
    }
    void api
      .me()
      .then((u) => {
        setUser(u);
        const loc = (u.locale === "en" ? "en" : "ru") as Locale;
        setStoredLocale(loc);
        setLocale(loc);
      })
      .catch(() => {
        clearToken();
        setUser(null);
      })
      .finally(() => setAuthChecked(true));
  }, []);

  const onChoose = async (p: Platform) => {
    setError("");
    try {
      await api.setPlatform(p);
      setPlatform(p);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const onLogout = async () => {
    try {
      await api.logout();
    } catch {
      /* ignore */
    }
    clearToken();
    setUser(null);
    setPlatform(null);
  };

  if (!authChecked) {
    return <div className="app-root" />;
  }

  if (!user) {
    return (
      <LoginPage
        onSuccess={(u) => {
          setUser(u);
          const loc = (u.locale === "en" ? "en" : "ru") as Locale;
          setLocale(loc);
        }}
      />
    );
  }

  return (
    <div className="app-root">
      <div className="token-bar">
        <span className="brand-chip">
          Zaliver<span>.</span>
        </span>
        <span className="badge">{user.username}</span>
        <button
          type="button"
          className="icon-btn"
          title={t("files", locale)}
          aria-label={t("files", locale)}
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
        <button type="button" className="btn secondary" onClick={onLogout}>
          {t("signOut", locale)}
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
        <AppShell
          platform={platform}
          onBack={() => setPlatform(null)}
          locale={locale}
          onLocaleChange={setLocale}
          user={user}
        />
      )}
      <SourcesManagerModal
        open={sourcesOpen}
        onClose={() => setSourcesOpen(false)}
      />
    </div>
  );
}
