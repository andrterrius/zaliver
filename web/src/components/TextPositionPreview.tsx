import { useRef } from "react";

type Props = {
  anchorX: number;
  anchorY: number;
  text: string;
  textColor: string;
  glowColor: string;
  glowEnabled: boolean;
  onChange: (x: number, y: number) => void;
};

function clamp01(n: number) {
  return Math.min(1, Math.max(0, n));
}

export function TextPositionPreview({
  anchorX,
  anchorY,
  text,
  textColor,
  glowColor,
  glowEnabled,
  onChange,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);

  const move = (clientX: number, clientY: number) => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return;
    onChange(clamp01((clientX - r.left) / r.width), clamp01((clientY - r.top) / r.height));
  };

  return (
    <div
      ref={ref}
      className="text-preview"
      onPointerDown={(e) => {
        (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
        move(e.clientX, e.clientY);
      }}
      onPointerMove={(e) => {
        if (e.buttons === 0) return;
        move(e.clientX, e.clientY);
      }}
    >
      <span
        className="text-preview-label"
        style={{
          left: `${anchorX * 100}%`,
          top: `${anchorY * 100}%`,
          color: textColor,
          textShadow: glowEnabled
            ? `0 0 8px ${glowColor}, 0 0 16px ${glowColor}`
            : "0 1px 2px rgba(0,0,0,0.6)",
        }}
      >
        {text.trim() || "Текст"}
      </span>
    </div>
  );
}
