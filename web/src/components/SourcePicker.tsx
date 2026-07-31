import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { usePaintSelectList } from "../hooks/usePaintSelectList";
import { formatDiskUsage } from "../lib/diskUsage";

export type SourceKind = "media" | "video" | "audio" | "all";

type Entry = {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
  abs_path: string | null;
  created_at: string | null;
};

type Props = {
  label: string;
  value: string[];
  onChange: (paths: string[]) => void;
  kind?: SourceKind;
  accept?: string;
  multiple?: boolean;
};

const VIDEO_EXTS = new Set([
  ".mp4",
  ".mov",
  ".mkv",
  ".webm",
  ".avi",
  ".m4v",
]);
const AUDIO_EXTS = new Set([
  ".mp3",
  ".wav",
  ".m4a",
  ".aac",
  ".flac",
  ".ogg",
  ".wma",
]);

function fileExt(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}

function uploadSubdirForKind(kind: SourceKind): "video" | "audio" | "uploads" {
  if (kind === "video") return "video";
  if (kind === "audio") return "audio";
  return "uploads";
}

function classifyUploadSubdir(file: File): "video" | "audio" | "uploads" {
  const ext = fileExt(file.name);
  if (VIDEO_EXTS.has(ext) || file.type.startsWith("video/")) return "video";
  if (AUDIO_EXTS.has(ext) || file.type.startsWith("audio/")) return "audio";
  return "uploads";
}

function formatSize(n: number | null): string {
  if (n == null || Number.isNaN(n)) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatMeta(e: Entry): string {
  const parts: string[] = [];
  if (e.is_dir) parts.push("открыть");
  else {
    const sz = formatSize(e.size);
    if (sz) parts.push(sz);
  }
  if (e.created_at) parts.push(e.created_at);
  return parts.join(" · ");
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
  const [diskHint, setDiskHint] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const loadDir = useCallback(
    async (path: string) => {
      setBusy(true);
      setError("");
      try {
        const res = await api.listSources(path, kind);
        setCwd(res.path);
        setParent(res.parent);
        setEntries(res.entries);
        setDiskHint(formatDiskUsage(res));
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

  const paint = useCallback(
    (abs: string, paintSelect: boolean) => {
      setSelected((prev) => {
        if (!multiple) {
          return paintSelect ? new Set([abs]) : new Set();
        }
        const has = prev.has(abs);
        if (paintSelect && has) return prev;
        if (!paintSelect && !has) return prev;
        const next = new Set(prev);
        if (paintSelect) next.add(abs);
        else next.delete(abs);
        return next;
      });
    },
    [multiple],
  );

  const { listRef, onPointerDown } = usePaintSelectList({
    isSelected: (key) => selected.has(key),
    paint,
  });

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
      const batches = new Map<"video" | "audio" | "uploads", File[]>();
      if (kind === "video" || kind === "audio") {
        batches.set(uploadSubdirForKind(kind), list);
      } else {
        for (const f of list) {
          const sub = classifyUploadSubdir(f);
          const bucket = batches.get(sub) || [];
          bucket.push(f);
          batches.set(sub, bucket);
        }
      }
      const paths: string[] = [];
      for (const [subdir, batch] of batches) {
        const res = await api.uploadSources(batch, subdir);
        paths.push(...res.paths);
      }
      onChange(
        multiple
          ? [...new Set([...value, ...paths])]
          : paths.slice(0, 1),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
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
      <p className="hint">
        {value.length
          ? `Выбрано файлов: ${value.length}`
          : "Файлы не выбраны."}
      </p>

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
            <p className="hint">{cwd ? cwd : "корень исходников"}</p>
            {diskHint ? <p className="hint">{diskHint}</p> : null}
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
            <div
              ref={listRef}
              className="source-browser-list source-browser-list--select"
              onPointerDown={onPointerDown}
            >
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
                    <span className="hint">{formatMeta(e)}</span>
                  </button>
                ) : e.abs_path ? (
                  <div
                    key={`f-${e.path}`}
                    data-entry-path={e.abs_path}
                    className={`source-browser-row file ${
                      selected.has(e.abs_path) ? "on" : ""
                    }`}
                  >
                    <input
                      type="checkbox"
                      className="source-browser-check"
                      checked={selected.has(e.abs_path)}
                      readOnly
                      tabIndex={-1}
                    />
                    <span>🎬 {e.name}</span>
                    <span className="hint">{formatMeta(e)}</span>
                  </div>
                ) : null,
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
