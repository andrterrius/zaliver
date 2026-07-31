import { useCallback, useEffect, useRef, useState, type PointerEvent } from "react";
import { api } from "../api/client";

type Entry = {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
  abs_path: string | null;
};

type Area = "sources" | "output";

type Props = {
  open: boolean;
  onClose: () => void;
};

function formatSize(n: number | null): string {
  if (n == null || Number.isNaN(n)) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function SourcesManagerModal({ open, onClose }: Props) {
  const fileRef = useRef<HTMLInputElement>(null);
  const dragRef = useRef<{
    active: boolean;
    paintSelect: boolean;
  } | null>(null);
  const [area, setArea] = useState<Area>("sources");
  const [cwd, setCwd] = useState("");
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [root, setRoot] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  const loadDir = useCallback(
    async (path: string, which: Area = area) => {
      setBusy(true);
      setError("");
      try {
        const res =
          which === "output"
            ? await api.listOutput(path, "all")
            : await api.listSources(path, "all");
        setRoot(res.root);
        setCwd(res.path);
        setParent(res.parent);
        setEntries(res.entries);
        setSelected(new Set());
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [area],
  );

  useEffect(() => {
    if (!open) return;
    setStatus("");
    setArea("sources");
    void loadDir("", "sources");
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const endDrag = () => {
      if (dragRef.current) dragRef.current.active = false;
    };
    window.addEventListener("pointerup", endDrag);
    window.addEventListener("pointercancel", endDrag);
    return () => {
      window.removeEventListener("pointerup", endDrag);
      window.removeEventListener("pointercancel", endDrag);
    };
  }, []);

  if (!open) return null;

  const fileEntries = entries.filter((e) => !e.is_dir);
  const selectedFileCount = fileEntries.filter((e) =>
    selected.has(e.path),
  ).length;

  const switchArea = (next: Area) => {
    if (next === area) return;
    setArea(next);
    setStatus("");
    setError("");
    void loadDir("", next);
  };

  const applyPaint = (rel: string, paintSelect: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (paintSelect) next.add(rel);
      else next.delete(rel);
      return next;
    });
  };

  const onRowPointerDown = (rel: string, e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    // Don't start drag-select from the open-folder button.
    if ((e.target as HTMLElement).closest(".source-browser-open")) return;
    const paintSelect = !selected.has(rel);
    dragRef.current = { active: true, paintSelect };
    applyPaint(rel, paintSelect);
    e.preventDefault();
  };

  const onRowPointerEnter = (rel: string) => {
    const drag = dragRef.current;
    if (!drag?.active) return;
    applyPaint(rel, drag.paintSelect);
  };

  const selectAllFiles = () => {
    setSelected(new Set(fileEntries.map((e) => e.path)));
  };

  const clearSelection = () => {
    setSelected(new Set());
  };

  const onUpload = async (files: FileList | null) => {
    if (!files?.length || area !== "sources") return;
    setBusy(true);
    setError("");
    setStatus("");
    try {
      const subdir = cwd ? `${cwd}/uploads` : "uploads";
      const res = await api.uploadSources([...files], subdir);
      setStatus(`Загружено: ${res.paths.length}`);
      await loadDir(cwd, "sources");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const onDelete = async () => {
    const paths = [...selected];
    if (!paths.length) {
      setError("Выберите файлы или папки для удаления.");
      return;
    }
    if (
      !confirm(
        `Удалить выбранное (${paths.length}) с сервера? Это необратимо.`,
      )
    ) {
      return;
    }
    setBusy(true);
    setError("");
    setStatus("");
    try {
      const res =
        area === "output"
          ? await api.deleteOutput(paths)
          : await api.deleteSources(paths);
      setStatus(`Удалено: ${res.deleted}`);
      await loadDir(cwd, area);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onDownload = async () => {
    const paths = [...selected].filter((p) =>
      fileEntries.some((e) => e.path === p),
    );
    if (!paths.length) {
      setError("Выберите файлы для скачивания.");
      return;
    }
    setBusy(true);
    setError("");
    setStatus("");
    try {
      await api.downloadLibrary(area, paths);
      setStatus(
        paths.length === 1
          ? "Скачивание начато."
          : `Скачивание архива (${paths.length} файлов)…`,
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal-card stack source-browser"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="page-header">
          <h3 className="group-title">Файлы на сервере</h3>
          <button type="button" className="btn secondary" onClick={onClose}>
            Закрыть
          </button>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button
            type="button"
            className={`btn ${area === "sources" ? "" : "secondary"}`}
            onClick={() => switchArea("sources")}
          >
            Исходники
          </button>
          <button
            type="button"
            className={`btn ${area === "output" ? "" : "secondary"}`}
            onClick={() => switchArea("output")}
          >
            Результаты
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
            onClick={() => void loadDir(parent ?? "", area)}
          >
            ← Назад
          </button>
          <button
            type="button"
            className="btn secondary"
            disabled={busy}
            onClick={() => void loadDir(cwd, area)}
          >
            Обновить
          </button>
          {area === "sources" ? (
            <button
              type="button"
              className="btn secondary"
              disabled={busy}
              onClick={() => fileRef.current?.click()}
            >
              Загрузить…
            </button>
          ) : null}
          <button
            type="button"
            className="btn secondary"
            disabled={busy || fileEntries.length === 0}
            onClick={selectAllFiles}
          >
            Выделить все
          </button>
          <button
            type="button"
            className="btn secondary"
            disabled={busy || selected.size === 0}
            onClick={clearSelection}
          >
            Снять
          </button>
          <button
            type="button"
            className="btn secondary"
            disabled={busy || selectedFileCount === 0}
            onClick={() => void onDownload()}
          >
            Скачать ({selectedFileCount})
          </button>
          <button
            type="button"
            className="btn secondary"
            disabled={busy || selected.size === 0}
            onClick={() => void onDelete()}
          >
            Удалить ({selected.size})
          </button>
        </div>
        <input
          ref={fileRef}
          type="file"
          accept="video/*,audio/*"
          multiple
          hidden
          onChange={(e) => void onUpload(e.target.files)}
        />
        {error ? <div className="error-banner">{error}</div> : null}
        {status ? <p className="hint">{status}</p> : null}
        <div className="source-browser-list source-browser-list--select">
          {busy && !entries.length ? (
            <div className="hint">Загрузка…</div>
          ) : null}
          {!busy && !entries.length ? (
            <div className="hint">Папка пуста.</div>
          ) : null}
          {entries.map((e) =>
            e.is_dir ? (
              <div
                key={`d-${e.path}`}
                className={`source-browser-row dir manage ${
                  selected.has(e.path) ? "on" : ""
                }`}
                onPointerDown={(ev) => onRowPointerDown(e.path, ev)}
                onPointerEnter={() => onRowPointerEnter(e.path)}
              >
                <input
                  type="checkbox"
                  checked={selected.has(e.path)}
                  readOnly
                  tabIndex={-1}
                />
                <button
                  type="button"
                  className="source-browser-open"
                  onClick={() => void loadDir(e.path, area)}
                >
                  📁 {e.name}
                </button>
                <span className="hint">папка</span>
              </div>
            ) : (
              <div
                key={`f-${e.path}`}
                className={`source-browser-row file ${
                  selected.has(e.path) ? "on" : ""
                }`}
                onPointerDown={(ev) => onRowPointerDown(e.path, ev)}
                onPointerEnter={() => onRowPointerEnter(e.path)}
              >
                <input
                  type="checkbox"
                  checked={selected.has(e.path)}
                  readOnly
                  tabIndex={-1}
                />
                <span>🎬 {e.name}</span>
                <span className="hint">{formatSize(e.size)}</span>
              </div>
            ),
          )}
        </div>
      </div>
    </div>
  );
}
