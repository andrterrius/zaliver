import type { Platform } from "../api/client";

const CHOICES: { id: Platform; name: string; hint: string }[] = [
  { id: "youtube", name: "YouTube", hint: "Залив видео на YouTube" },
  { id: "instagram", name: "Instagram", hint: "Залив видео на Instagram" },
];

type Props = {
  onChoose: (platform: Platform) => void;
};

export function PlatformSelect({ onChoose }: Props) {
  return (
    <div className="platform-select">
      <h1>Zaliver</h1>
      <p className="sub">Выберите режим</p>
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
