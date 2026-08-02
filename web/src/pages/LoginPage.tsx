import { useEffect, useState } from "react";
import { api, ApiError, setToken, type AuthUser } from "../api/client";
import { getStoredLocale, setStoredLocale, t, type Locale } from "../i18n";

type Props = {
  onSuccess: (user: AuthUser) => void;
};

export function LoginPage({ onSuccess }: Props) {
  const locale = getStoredLocale();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const res = await api.login(username.trim(), password);
      setToken(res.token);
      const loc = (res.user.locale === "en" ? "en" : "ru") as Locale;
      setStoredLocale(loc);
      onSuccess(res.user);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError(t("loginError", locale));
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-page">
      <form className="login-card stack" onSubmit={submit}>
        <div className="brand-chip login-brand">
          Zaliver<span>.</span>
        </div>
        <h1 className="title">{t("loginTitle", locale)}</h1>
        <label className="hint">{t("username", locale)}</label>
        <input
          className="field"
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          required
        />
        <label className="hint">{t("password", locale)}</label>
        <input
          className="field"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
        {error ? <div className="error-banner">{error}</div> : null}
        <button className="btn" type="submit" disabled={busy}>
          {t("signIn", locale)}
        </button>
      </form>
    </div>
  );
}
