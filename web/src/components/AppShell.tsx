import { useState } from "react";
import type { AuthUser, Platform } from "../api/client";
import { UniquifyPage } from "../pages/UniquifyPage";
import { SlicingPage } from "../pages/SlicingPage";
import { StitchingPage } from "../pages/StitchingPage";
import { ReadyPage } from "../pages/ReadyPage";
import { UploadedPage } from "../pages/UploadedPage";
import { ProfilesPage } from "../pages/ProfilesPage";
import { ChannelEditPage } from "../pages/ChannelEditPage";
import { AiPage } from "../pages/AiPage";
import { SettingsPage } from "../pages/SettingsPage";
import { t, type Locale } from "../i18n";

type Props = {
  platform: Platform;
  onBack: () => void;
  locale: Locale;
  onLocaleChange: (locale: Locale) => void;
  user: AuthUser;
};

export function AppShell({
  platform,
  onBack,
  locale,
  onLocaleChange,
  user,
}: Props) {
  const [tab, setTab] = useState(0);
  const nav = [
    { id: 0, label: t("navUniquify", locale) },
    { id: 1, label: t("navSlicing", locale) },
    { id: 2, label: t("navStitching", locale) },
    { id: 3, label: t("navReady", locale) },
    { id: 4, label: t("navUploaded", locale) },
    { id: 5, label: t("navProfiles", locale) },
    { id: 6, label: t("navChannels", locale) },
    { id: 7, label: t("navAi", locale) },
    { id: 8, label: t("navSettings", locale) },
  ] as const;
  const navItems =
    platform === "yt_inst"
      ? nav.filter((n) => n.id === 0 || n.id === 1 || n.id === 2 || n.id === 8)
      : nav;

  return (
    <div className="shell">
      <aside className="side-nav">
        <div className="side-nav-brand">
          Zaliver<span>.</span>
        </div>
        <ul className="side-nav-list">
          {navItems.map(({ id, label }) => (
            <li key={id}>
              <button
                type="button"
                className={tab === id ? "active" : ""}
                onClick={() => setTab(id)}
              >
                {label}
              </button>
            </li>
          ))}
        </ul>
        <button type="button" className="side-nav-back" onClick={onBack}>
          {t("backPlatform", locale)}
        </button>
      </aside>
      <main className="main-pane">
        {tab === 0 ? <UniquifyPage platform={platform} /> : null}
        {tab === 1 ? <SlicingPage platform={platform} /> : null}
        {tab === 2 ? <StitchingPage platform={platform} /> : null}
        {tab === 3 ? <ReadyPage platform={platform} /> : null}
        {tab === 4 ? <UploadedPage platform={platform} /> : null}
        {tab === 5 ? <ProfilesPage platform={platform} /> : null}
        {tab === 6 ? <ChannelEditPage platform={platform} /> : null}
        {tab === 7 ? <AiPage platform={platform} /> : null}
        {tab === 8 ? (
          <SettingsPage
            platform={platform}
            locale={locale}
            onLocaleChange={onLocaleChange}
            user={user}
          />
        ) : null}
      </main>
    </div>
  );
}
