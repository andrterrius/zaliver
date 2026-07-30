import { useId } from "react";

export type RangeValue = { lo: number; hi: number };

type Props = {
  min: number;
  max: number;
  step?: number;
  value: RangeValue;
  onChange: (value: RangeValue) => void;
  decimals?: number;
  suffix?: string;
  disabled?: boolean;
};

function clamp(n: number, lo: number, hi: number) {
  return Math.min(hi, Math.max(lo, n));
}

function fmt(n: number, decimals: number) {
  return n.toFixed(decimals);
}

export function RangeSlider({
  min,
  max,
  step = 1,
  value,
  onChange,
  decimals = 0,
  suffix = "",
  disabled,
}: Props) {
  const id = useId();
  const span = Math.max(1e-9, max - min);
  const loPct = ((value.lo - min) / span) * 100;
  const hiPct = ((value.hi - min) / span) * 100;

  const setLo = (raw: number) => {
    const lo = clamp(raw, min, value.hi);
    onChange({ lo, hi: value.hi });
  };
  const setHi = (raw: number) => {
    const hi = clamp(raw, value.lo, max);
    onChange({ lo: value.lo, hi });
  };

  return (
    <div className={`range-slider ${disabled ? "disabled" : ""}`}>
      <div className="range-slider-track-wrap">
        <div className="range-slider-track" />
        <div
          className="range-slider-fill"
          style={{ left: `${loPct}%`, width: `${Math.max(0, hiPct - loPct)}%` }}
        />
        <input
          id={`${id}-lo`}
          className="range-slider-input"
          type="range"
          min={min}
          max={max}
          step={step}
          value={value.lo}
          disabled={disabled}
          onChange={(e) => setLo(Number(e.target.value))}
          aria-label="Минимум"
        />
        <input
          id={`${id}-hi`}
          className="range-slider-input"
          type="range"
          min={min}
          max={max}
          step={step}
          value={value.hi}
          disabled={disabled}
          onChange={(e) => setHi(Number(e.target.value))}
          aria-label="Максимум"
        />
      </div>
      <span className="range-slider-label">
        {fmt(value.lo, decimals)}…{fmt(value.hi, decimals)}
        {suffix}
      </span>
    </div>
  );
}
