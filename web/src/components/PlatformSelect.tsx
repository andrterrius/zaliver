import type { Platform } from "../api/client";

const CHOICES: { id: Platform; name: string; hint: string; index: string }[] = [
  {
    id: "youtube",
    name: "YouTube",
    hint: "Залив видео на YouTube",
    index: "01",
  },
  {
    id: "instagram",
    name: "Instagram",
    hint: "Залив видео на Instagram",
    index: "02",
  },
  {
    id: "yt_inst",
    name: "Yt+Inst",
    hint: "Одно видео на YouTube и Instagram (2 вкладки)",
    index: "03",
  },
];

type Props = {
  onChoose: (platform: Platform) => void;
};

export function PlatformSelect({ onChoose }: Props) {
  return (
    <div className="platform-select">
      <div className="platform-brand">
        <div className="brand-mark" aria-hidden />
        <h1>Zaliver</h1>
      </div>
      <p className="sub">Выберите режим работы</p>
      <div className="platform-cards">
        {CHOICES.map((c) => (
          <div
            key={c.id}
            className="platform-card"
            onClick={() => onChoose(c.id)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") onChoose(c.id);
            }}
          >
            <div className="card-index">{c.index}</div>
            <h2>{c.name}</h2>
            <p>{c.hint}</p>
            <button
              type="button"
              className="btn"
              onClick={(e) => {
                e.stopPropagation();
                onChoose(c.id);
              }}
            >
              Открыть
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
