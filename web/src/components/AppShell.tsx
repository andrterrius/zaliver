import { useState } from "react";
import type { Platform } from "../api/client";
import { UniquifyPage } from "../pages/UniquifyPage";
import { SlicingPage } from "../pages/SlicingPage";
import { StitchingPage } from "../pages/StitchingPage";
import { ReadyPage } from "../pages/ReadyPage";
import { UploadedPage } from "../pages/UploadedPage";
import { ProfilesPage } from "../pages/ProfilesPage";
import { ChannelEditPage } from "../pages/ChannelEditPage";
import { AiPage } from "../pages/AiPage";
import { SettingsPage } from "../pages/SettingsPage";

const NAV = [
  { id: 0, label: "Уникализация" },
  { id: 1, label: "Нарезка" },
  { id: 2, label: "Склейка" },
  { id: 3, label: "Готовые видео" },
  { id: 4, label: "Залитые видео" },
  { id: 5, label: "Профили" },
  { id: 6, label: "Редактирование каналов" },
  { id: 7, label: "ИИ" },
  { id: 8, label: "Настройки" },
] as const;

type Props = {
  platform: Platform;
  onBack: () => void;
};

export function AppShell({ platform, onBack }: Props) {
  const [tab, setTab] = useState(0);
  const navItems =
    platform === "yt_inst"
      ? NAV.filter((n) => n.id === 0 || n.id === 1 || n.id === 2 || n.id === 8)
      : NAV;

  return (
    <div className="shell">
      <aside className="side-nav">
        <div className="side-nav-brand">
          Zaliver<span>.</span>
        </div>
        <ul className="side-nav-list">
          {navItems.map(({ id, label }) => (
            <li key={label}>
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
          ← Выбор платформы
        </button>
      </aside>
      <main className="main-pane">
        {tab === 0 ? <UniquifyPage /> : null}
        {tab === 1 ? <SlicingPage /> : null}
        {tab === 2 ? <StitchingPage /> : null}
        {tab === 3 ? <ReadyPage /> : null}
        {tab === 4 ? <UploadedPage platform={platform} /> : null}
        {tab === 5 ? <ProfilesPage platform={platform} /> : null}
        {tab === 6 ? <ChannelEditPage platform={platform} /> : null}
        {tab === 7 ? <AiPage platform={platform} /> : null}
        {tab === 8 ? <SettingsPage platform={platform} /> : null}
      </main>
    </div>
  );
}
