import { useEffect, useState, type ReactNode } from "react";

type Props = {
  lines: string[];
  emptyHint?: string;
  title?: string;
  children?: ReactNode;
};

/** Job log panel with visual clear (hides lines up to the clear point). */
export function JobLogBox({
  lines,
  emptyHint = "Лог появится после старта задачи…",
  title = "Лог",
  children,
}: Props) {
  const [hiddenCount, setHiddenCount] = useState(0);

  useEffect(() => {
    if (lines.length < hiddenCount) setHiddenCount(0);
  }, [lines.length, hiddenCount]);

  const visible = lines.slice(hiddenCount);
  const text = visible.length ? visible.join("\n") : emptyHint;
  const canClear = visible.length > 0;

  return (
    <section className="group">
      <div className="page-header" style={{ marginBottom: 8 }}>
        <h3 className="group-title" style={{ margin: 0, flex: 1 }}>
          {title}
        </h3>
        <button
          type="button"
          className="btn secondary"
          disabled={!canClear}
          onClick={() => setHiddenCount(lines.length)}
        >
          Очистить
        </button>
      </div>
      <div className="log-box">{text}</div>
      {children}
    </section>
  );
}

export function mergeJobLogLines(
  jobLogs?: string[] | null,
  uploadLogs?: string[] | null
): string[] {
  return [
    ...(jobLogs || []),
    ...(uploadLogs || []).map((l) => `[upload] ${l}`),
  ];
}
