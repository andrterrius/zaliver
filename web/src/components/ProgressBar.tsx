type Props = {
  value: number;
  max?: number;
  label?: string;
};

export function ProgressBar({ value, max = 100, label }: Props) {
  const pct = max > 0 ? Math.max(0, Math.min(100, (value / max) * 100)) : 0;
  return (
    <div className="progress" role="progressbar" aria-valuenow={pct}>
      <span style={{ width: `${pct}%` }} />
      {label ? <div className="progress-label">{label}</div> : null}
    </div>
  );
}
