import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { usePaintSelectList } from "../hooks/usePaintSelectList";
import { formatDiskUsage } from "../lib/diskUsage";

type Entry = {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
  abs_path: string | null;
  created_at: string | null;
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

function formatMeta(e: Entry): string {
  const parts: string[] = [];
  if (e.is_dir) parts.push("папка");
  else {
    const sz = formatSize(e.size);
    if (sz) parts.push(sz);
  }
  if (e.created_at) parts.push(e.created_at);
  return parts.join(" · ");
}

export function SourcesManagerModal({ open, onClose }: Props) {
  const fileRef = useRef<HTMLInputElement>(null);
  const mkdirInputRef = useRef<HTMLInputElement>(null);
  const selectNInputRef = useRef<HTMLInputElement>(null);
  const [area, setArea] = useState<Area>("sources");
  const [cwd, setCwd] = useState("");
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [diskHint, setDiskHint] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [mkdirOpen, setMkdirOpen] = useState(false);
  const [mkdirName, setMkdirName] = useState("");
  const [mkdirError, setMkdirError] = useState("");
  const [selectNOpen, setSelectNOpen] = useState(false);
  const [selectNValue, setSelectNValue] = useState("1");
  const [selectNError, setSelectNError] = useState("");

  const paint = useCallback((rel: string, paintSelect: boolean) => {
    setSelected((prev) => {
      const has = prev.has(rel);
      if (paintSelect && has) return prev;
      if (!paintSelect && !has) return prev;
      const next = new Set(prev);
      if (paintSelect) next.add(rel);
      else next.delete(rel);
      return next;
    });
  }, []);

  const { listRef, onPointerDown } = usePaintSelectList({
    isSelected: (key) => selected.has(key),
    paint,
  });

  const loadDir = useCallback(
    async (path: string, which: Area = area) => {
      setBusy(true);
      setError("");
      try {
        const res =
          which === "output"
            ? await api.listOutput(path, "all")
            : await api.listSources(path, "all");
        setCwd(res.path);
        setParent(res.parent);
        setEntries(res.entries);
        setDiskHint(formatDiskUsage(res));
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
    setMkdirOpen(false);
    setMkdirName("");
    setMkdirError("");
    setSelectNOpen(false);
    setSelectNError("");
    setArea("sources");
    void loadDir("", "sources");
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!mkdirOpen) return;
    const t = window.setTimeout(() => {
      mkdirInputRef.current?.focus();
      mkdirInputRef.current?.select();
    }, 0);
    return () => window.clearTimeout(t);
  }, [mkdirOpen]);

  useEffect(() => {
    if (!selectNOpen) return;
    const t = window.setTimeout(() => selectNInputRef.current?.focus(), 0);
    return () => window.clearTimeout(t);
  }, [selectNOpen]);

  const fileEntries = entries.filter((e) => !e.is_dir);
  const selectedFileCount = fileEntries.filter((e) =>
    selected.has(e.path),
  ).length;

  if (!open) return null;

  const switchArea = (next: Area) => {
    if (next === area) return;
    setArea(next);
    setStatus("");
    setError("");
    setMkdirOpen(false);
    setSelectNOpen(false);
    void loadDir("", next);
  };

  const selectAllFiles = () => {
    setSelected(new Set(fileEntries.map((e) => e.path)));
  };

  const clearSelection = () => {
    setSelected(new Set());
  };

  const openSelectN = () => {
    setSelectNValue("1");
    setSelectNError("");
    setSelectNOpen(true);
  };

  const closeSelectN = () => {
    setSelectNOpen(false);
    setSelectNError("");
  };

  const submitSelectN = () => {
    const raw = selectNValue.trim();
    const n = Number.parseInt(raw, 10);
    if (!raw || !Number.isFinite(n) || n < 1) {
      setSelectNError("Введите целое число не меньше 1.");
      return;
    }
    const take = Math.min(n, fileEntries.length);
    setSelected(new Set(fileEntries.slice(0, take).map((e) => e.path)));
    setSelectNOpen(false);
    setSelectNError("");
  };

  const onUpload = async (files: FileList | null) => {
    if (!files?.length || area !== "sources") return;
    setBusy(true);
    setError("");
    setStatus("");
    try {
      // Upload into the current folder (root → uploads for convenience).
      const subdir = cwd || "uploads";
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

  const openMkdir = () => {
    if (area !== "sources") return;
    setMkdirName("");
    setMkdirError("");
    setMkdirOpen(true);
  };

  const closeMkdir = () => {
    if (busy) return;
    setMkdirOpen(false);
    setMkdirName("");
    setMkdirError("");
  };

  const submitMkdir = async () => {
    if (area !== "sources") return;
    const name = mkdirName.trim();
    if (!name) {
      setMkdirError("Укажите имя папки.");
      return;
    }
    setBusy(true);
    setMkdirError("");
    setError("");
    setStatus("");
    try {
      const res = await api.mkdirSources(cwd, name);
      setStatus(`Создана папка: ${res.path}`);
      setMkdirOpen(false);
      setMkdirName("");
      await loadDir(cwd, "sources");
    } catch (e) {
      setMkdirError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async () => {
    const paths = [...selected];
    if (!paths.length) {
      setError("Выберите файлы или папки для удаления.");
      return;
    }
    const dirCount = entries.filter(
      (e) => e.is_dir && selected.has(e.path),
    ).length;
    const msg =
      dirCount > 0
        ? `Удалить выбранное (${paths.length}), включая папки с содержимым? Это необратимо.`
        : `Удалить выбранное (${paths.length}) с сервера? Это необратимо.`;
    if (!confirm(msg)) {
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
      const errMsg = e instanceof Error ? e.message : String(e);
      setError(errMsg);
      const match = /Too many paths \(max (\d+)\)/i.exec(errMsg);
      if (match) {
        const max = Number.parseInt(match[1], 10);
        if (Number.isFinite(max) && max > 0) {
          setSelected((prev) => {
            if (prev.size <= max) return prev;
            const ordered = [
              ...entries
                .filter((entry) => prev.has(entry.path))
                .map((entry) => entry.path),
              ...[...prev].filter(
                (p) => !entries.some((entry) => entry.path === p),
              ),
            ];
            return new Set(ordered.slice(0, max));
          });
        }
      }
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
          {cwd
            ? cwd
            : area === "output"
              ? "корень результатов"
              : "корень исходников"}
        </p>
        {diskHint ? <p className="disk-usage">{diskHint}</p> : null}
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
          {area === "sources" ? (
            <button
              type="button"
              className="btn secondary"
              disabled={busy}
              onClick={openMkdir}
            >
              Создать папку…
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
            disabled={busy || fileEntries.length === 0}
            onClick={openSelectN}
          >
            Выбрать N…
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
              <div
                key={`d-${e.path}`}
                data-entry-path={e.path}
                className={`source-browser-row dir manage ${
                  selected.has(e.path) ? "on" : ""
                }`}
              >
                <input
                  type="checkbox"
                  className="source-browser-check"
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
                <span className="hint">{formatMeta(e)}</span>
              </div>
            ) : (
              <div
                key={`f-${e.path}`}
                data-entry-path={e.path}
                className={`source-browser-row file ${
                  selected.has(e.path) ? "on" : ""
                }`}
              >
                <input
                  type="checkbox"
                  className="source-browser-check"
                  checked={selected.has(e.path)}
                  readOnly
                  tabIndex={-1}
                />
                <span>🎬 {e.name}</span>
                <span className="hint">{formatMeta(e)}</span>
              </div>
            ),
          )}
        </div>
      </div>

      {mkdirOpen ? (
        <div
          className="modal-backdrop modal-backdrop--nested"
          onClick={closeMkdir}
        >
          <div
            className="modal-card stack mkdir-dialog"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-labelledby="mkdir-title"
          >
            <h3 id="mkdir-title" className="group-title">
              Новая папка
            </h3>
            <p className="hint">
              {cwd ? `Внутри: ${cwd}` : "В корне исходников"}
            </p>
            <label className="hint" htmlFor="mkdir-name-input">
              Имя папки
            </label>
            <input
              id="mkdir-name-input"
              ref={mkdirInputRef}
              className="field"
              value={mkdirName}
              disabled={busy}
              placeholder="например, clips"
              autoComplete="off"
              onChange={(e) => {
                setMkdirName(e.target.value);
                if (mkdirError) setMkdirError("");
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void submitMkdir();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  closeMkdir();
                }
              }}
            />
            {mkdirError ? <div className="error-banner">{mkdirError}</div> : null}
            <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
              <button
                type="button"
                className="btn secondary"
                disabled={busy}
                onClick={closeMkdir}
              >
                Отмена
              </button>
              <button
                type="button"
                className="btn"
                disabled={busy || !mkdirName.trim()}
                onClick={() => void submitMkdir()}
              >
                Создать
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {selectNOpen ? (
        <div
          className="modal-backdrop modal-backdrop--nested"
          onClick={closeSelectN}
        >
          <div
            className="modal-card stack mkdir-dialog"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-labelledby="mgr-select-n-title"
          >
            <h3 id="mgr-select-n-title" className="group-title">
              Выбрать N файлов
            </h3>
            <p className="hint">
              В этой папке файлов: {fileEntries.length}. Если N больше — будут
              выбраны все.
            </p>
            <label className="hint" htmlFor="mgr-select-n-input">
              Количество
            </label>
            <input
              id="mgr-select-n-input"
              ref={selectNInputRef}
              className="field"
              type="number"
              min={1}
              step={1}
              value={selectNValue}
              placeholder="например, 10"
              autoComplete="off"
              onChange={(e) => {
                setSelectNValue(e.target.value);
                if (selectNError) setSelectNError("");
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  submitSelectN();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  closeSelectN();
                }
              }}
            />
            {selectNError ? (
              <div className="error-banner">{selectNError}</div>
            ) : null}
            <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
              <button
                type="button"
                className="btn secondary"
                onClick={closeSelectN}
              >
                Отмена
              </button>
              <button type="button" className="btn" onClick={submitSelectN}>
                Выбрать
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
