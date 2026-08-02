import { useEffect, useId, useRef, useState, type ReactNode } from "react";

export function formatRecentPickerLabel(value: string, maxLen = 120): string {
  const raw = String(value);
  const lines = raw
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter(Boolean);
  if (lines.length > 1) {
    let head = lines[0];
    if (head.length > maxLen) head = `${head.slice(0, maxLen - 1)}…`;
    return `${head}  ·  ${lines.length} строк`;
  }
  const oneLine = raw.split(/\r?\n/).join(" ");
  if (oneLine.length > maxLen) return `${oneLine.slice(0, maxLen - 1)}…`;
  return oneLine;
}

type PickerProps = {
  recent: string[];
  onSelect: (value: string) => void;
  disabled?: boolean;
  tooltip?: string;
};

export function RecentValuesPicker({
  recent,
  onSelect,
  disabled = false,
  tooltip = "Недавно введённые значения",
}: PickerProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  const items = recent
    .map((v) => String(v).trim())
    .filter(Boolean)
    .filter((v, i, arr) => {
      const key = v.toLowerCase();
      return arr.findIndex((x) => x.toLowerCase() === key) === i;
    });

  const enabled = !disabled && items.length > 0;

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="recent-values-picker" ref={rootRef}>
      <button
        type="button"
        className="recent-values-btn"
        title={tooltip}
        aria-label={tooltip}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        disabled={!enabled}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="recent-values-btn-icon" aria-hidden>
          ▾
        </span>
      </button>
      {open && enabled ? (
        <div className="recent-values-menu" id={menuId} role="menu">
          {items.map((value) => (
            <button
              key={value}
              type="button"
              role="menuitem"
              className="recent-values-item"
              title={value.includes("\n") || value.length > 120 ? value : undefined}
              onClick={() => {
                onSelect(value);
                setOpen(false);
              }}
            >
              {formatRecentPickerLabel(value)}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

type FieldProps = {
  recent: string[];
  onSelect: (value: string) => void;
  disabled?: boolean;
  tooltip?: string;
  children: ReactNode;
};

export function FieldWithRecent({
  recent,
  onSelect,
  disabled,
  tooltip,
  children,
}: FieldProps) {
  return (
    <div className="field-with-recent">
      <div className="field-with-recent-main">{children}</div>
      <RecentValuesPicker
        recent={recent}
        onSelect={onSelect}
        disabled={disabled}
        tooltip={tooltip}
      />
    </div>
  );
}
