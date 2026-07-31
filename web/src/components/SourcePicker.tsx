import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";

export type SourceKind = "media" | "video" | "audio" | "all";

type Entry = {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
  abs_path: string | null;
};

type Props = {
  label: string;
  value: string[];
  onChange: (paths: string[]) => void;
  kind?: SourceKind;
  accept?: string;
  multiple?: boolean;
};

function basename(p: string): string {
  const norm = p.replace(/\\/g, "/");
  const i = norm.lastIndexOf("/");
  return i >= 0 ? norm.slice(i + 1) : norm;
}

function formatSize(n: number | null): string {
  if (n == null || Number.isNaN(n)) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function SourcePicker({
  label,
  value,
  onChange,
  kind = "media",
  accept = "video/*,audio/*",
  multiple = true,
}: Props) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [cwd, setCwd] = useState("");
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [root, setRoot] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const loadDir = useCallback(
    async (path: string) => {
      setBusy(true);
      setError("");
      try {
        const res = await api.listSources(path, kind);
        setRoot(res.root);
        setCwd(res.path);
        setParent(res.parent);
        setEntries(res.entries);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [kind],
  );

  useEffect(() => {
    if (!open) return;
    setSelected(new Set());
    void loadDir("");
  }, [open, loadDir]);

  const names = useMemo(() => value.map(basename), [value]);

  const toggleFile = (abs: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (!multiple) {
        return next.has(abs) ? new Set() : new Set([abs]);
      }
      if (next.has(abs)) next.delete(abs);
      else next.add(abs);
      return next;
    });
  };

  const confirmServer = () => {
    const paths = [...selected];
    if (!paths.length) {
      setError("Выберите хотя бы один файл.");
      return;
    }
    onChange(multiple ? [...new Set([...value, ...paths])] : paths);
    setOpen(false);
  };

  const onLocalFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true);
    setError("");
    try {
      const list = [...files];
      const res = await api.uploadSources(list);
      onChange(
        multiple
          ? [...new Set([...value, ...res.paths])]
          : res.paths.slice(0, 1),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const removeAt = (idx: number) => {
    onChange(value.filter((_, i) => i !== idx));
  };

  return (
    <div className="source-picker stack">
      <label className="hint">{label}</label>
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <button
          type="button"
          className="btn secondary"
          disabled={busy}
          onClick={() => fileRef.current?.click()}
        >
          С компьютера
        </button>
        <button
          type="button"
          className="btn secondary"
          disabled={busy}
          onClick={() => setOpen(true)}
        >
          Исходники на сервере
        </button>
        {value.length ? (
          <button
            type="button"
            className="btn secondary"
            onClick={() => onChange([])}
          >
            Очистить
          </button>
        ) : null}
      </div>
      <input
        ref={fileRef}
        type="file"
        accept={accept}
        multiple={multiple}
        hidden
        onChange={(e) => void onLocalFiles(e.target.files)}
      />
      {error && !open ? <div className="error-banner">{error}</div> : null}
      {names.length ? (
        <ul className="source-picked-list">
          {names.map((n, i) => (
            <li key={`${value[i]}-${i}`}>
              <span title={value[i]}>{n}</span>
              <button
                type="button"
                className="btn secondary"
                style={{ padding: "2px 8px", fontSize: 12 }}
                onClick={() => removeAt(i)}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="hint">Файлы не выбраны.</p>
      )}

      {open ? (
        <div className="modal-backdrop" onClick={() => setOpen(false)}>
          <div
            className="modal-card stack source-browser"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="page-header">
              <h3 className="group-title">Исходники на сервере</h3>
              <button
                type="button"
                className="btn secondary"
                onClick={() => setOpen(false)}
              >
                Закрыть
              </button>
            </div>
            <p className="hint">
              {root}
              {cwd ? ` / ${cwd}` : ""}
            </p>
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              <button
                type="button"
                className="btn secondary"
                disabled={parent === null || busy}
                onClick={() => void loadDir(parent ?? "")}
              >
                ← Назад
              </button>
              <button
                type="button"
                className="btn secondary"
                disabled={busy}
                onClick={() => void loadDir(cwd)}
              >
                Обновить
              </button>
              <span className="hint">Выбрано: {selected.size}</span>
            </div>
            {error ? <div className="error-banner">{error}</div> : null}
            <div className="source-browser-list">
              {busy && !entries.length ? (
                <div className="hint">Загрузка…</div>
              ) : null}
              {!busy && !entries.length ? (
                <div className="hint">Папка пуста.</div>
              ) : null}
              {entries.map((e) =>
                e.is_dir ? (
                  <button
                    key={`d-${e.path}`}
                    type="button"
                    className="source-browser-row dir"
                    onClick={() => void loadDir(e.path)}
                  >
                    <span>📁 {e.name}</span>
                    <span className="hint">открыть</span>
                  </button>
                ) : (
                  <label
                    key={`f-${e.path}`}
                    className={`source-browser-row file ${
                      e.abs_path && selected.has(e.abs_path) ? "on" : ""
                    }`}
                  >
                    <input
                      type="checkbox"
                      checked={!!(e.abs_path && selected.has(e.abs_path))}
                      disabled={!e.abs_path}
                      onChange={() => e.abs_path && toggleFile(e.abs_path)}
                    />
                    <span>🎬 {e.name}</span>
                    <span className="hint">{formatSize(e.size)}</span>
                  </label>
                ),
              )}
            </div>
            <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
              <button
                type="button"
                className="btn secondary"
                onClick={() => setOpen(false)}
              >
                Отмена
              </button>
              <button type="button" className="btn" onClick={confirmServer}>
                Выбрать
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
