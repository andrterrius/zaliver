import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type Platform } from "../api/client";
import type { OutputKind } from "../hooks/useManagedOutputDir";
import { formatDiskUsage } from "../lib/diskUsage";

type Entry = {
  name: string;
  path: string;
  is_dir: boolean;
  size: number | null;
  abs_path: string | null;
  created_at: string | null;
};

type Props = {
  kind: OutputKind;
  platform: Platform;
  value: string;
  onChange: (absPath: string) => void;
  disabled?: boolean;
};

function normRel(p: string): string {
  return (p || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
}

function isUnderOrEqual(path: string, base: string): boolean {
  const p = normRel(path);
  const b = normRel(base);
  if (!b) return true;
  if (!p) return false;
  return p === b || p.startsWith(`${b}/`);
}

function displayLabel(absPath: string, root: string, fallbackRel: string): string {
  const abs = (absPath || "").replace(/\\/g, "/");
  const r = (root || "").replace(/\\/g, "/").replace(/\/+$/, "");
  if (abs && r && (abs === r || abs.startsWith(`${r}/`))) {
    return abs.slice(r.length).replace(/^\//, "") || fallbackRel;
  }
  if (abs) {
    const parts = abs.split("/");
    return parts.slice(-3).join("/") || abs;
  }
  return fallbackRel;
}

const KIND_LABEL: Record<OutputKind, string> = {
  uniquify: "уникализация",
  slicing: "нарезка",
  gluing: "склейка",
};

/** Pick an output folder under results/<platform>/<kind>/ (or a subfolder). */
export function OutputFolderPicker({
  kind,
  platform: uiPlatform,
  value,
  onChange,
  disabled = false,
}: Props) {
  const [root, setRoot] = useState("");
  const [platform, setPlatform] = useState(uiPlatform);
  const [defaultAbs, setDefaultAbs] = useState("");
  const [open, setOpen] = useState(false);
  const [cwd, setCwd] = useState("");
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [cwdAbs, setCwdAbs] = useState("");
  const [diskHint, setDiskHint] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [mkdirOpen, setMkdirOpen] = useState(false);
  const [mkdirName, setMkdirName] = useState("");
  const [mkdirError, setMkdirError] = useState("");
  const mkdirRef = useRef<HTMLInputElement>(null);

  const baseRel = useMemo(
    () => (platform ? `${platform}/${kind}` : kind),
    [platform, kind],
  );

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await api.getOutputDirs(uiPlatform);
        if (!alive) return;
        setRoot(res.root || "");
        setPlatform((res.platform as Platform) || uiPlatform);
        const abs = res.dirs[kind] || "";
        setDefaultAbs(abs);
        const cur = (value || "").replace(/\\/g, "/");
        const want = abs.replace(/\\/g, "/");
        const underUi =
          Boolean(uiPlatform) &&
          (cur === uiPlatform ||
            cur.startsWith(`${uiPlatform}/`) ||
            cur.includes(`/${uiPlatform}/`) ||
            cur.endsWith(`/${uiPlatform}`));
        // Switch folder when platform changes or value empty / from another platform.
        if (!cur || (want && !underUi)) {
          if (abs) onChange(abs);
        }
      } catch (e) {
        if (!alive) return;
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, uiPlatform]);

  const loadDir = useCallback(
    async (path: string) => {
      const target = normRel(path) || baseRel;
      if (!isUnderOrEqual(target, baseRel)) {
        setError("Можно выбирать только внутри папки результатов этой обработки.");
        return;
      }
      setBusy(true);
      setError("");
      try {
        const res = await api.listOutput(target, "all");
        const cur = normRel(res.path) || baseRel;
        setCwd(cur);
        const rawParent = res.parent;
        if (
          rawParent != null &&
          isUnderOrEqual(rawParent, baseRel) &&
          normRel(rawParent) !== cur
        ) {
          setParent(normRel(rawParent));
        } else {
          setParent(null);
        }
        setEntries(res.entries.filter((e) => e.is_dir));
        setDiskHint(formatDiskUsage(res));
        const listRoot = (res.root || root).replace(/\\/g, "/").replace(/\/+$/, "");
        setCwdAbs(cur ? `${listRoot}/${cur}` : listRoot);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [baseRel, defaultAbs, root],
  );

  useEffect(() => {
    if (!open || !baseRel) return;
    const start =
      value && defaultAbs && value.replace(/\\/g, "/").startsWith(defaultAbs.replace(/\\/g, "/"))
        ? (() => {
            const r = root.replace(/\\/g, "/").replace(/\/+$/, "");
            const v = value.replace(/\\/g, "/");
            if (r && v.startsWith(`${r}/`)) return v.slice(r.length + 1);
            return baseRel;
          })()
        : baseRel;
    void loadDir(start);
    setMkdirOpen(false);
    setMkdirName("");
    setMkdirError("");
  }, [open, baseRel]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!mkdirOpen) return;
    const t = window.setTimeout(() => {
      mkdirRef.current?.focus();
      mkdirRef.current?.select();
    }, 0);
    return () => window.clearTimeout(t);
  }, [mkdirOpen]);

  const label = displayLabel(value || defaultAbs, root, baseRel);

  const confirmCwd = () => {
    const abs = cwdAbs || defaultAbs;
    if (abs) onChange(abs);
    setOpen(false);
  };

  const submitMkdir = async () => {
    const name = mkdirName.trim();
    if (!name) {
      setMkdirError("Укажите имя папки.");
      return;
    }
    setBusy(true);
    setMkdirError("");
    try {
      await api.mkdirOutput(cwd || baseRel, name);
      setMkdirOpen(false);
      setMkdirName("");
      await loadDir(cwd || baseRel);
    } catch (e) {
      setMkdirError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack" style={{ gap: 8 }}>
      <label className="hint">Папка результатов</label>
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <input
          className="field"
          readOnly
          disabled={disabled}
          value={label}
          title={value || defaultAbs}
          style={{ flex: 1, minWidth: 160 }}
        />
        <button
          type="button"
          className="btn secondary"
          disabled={disabled || !baseRel}
          onClick={() => setOpen(true)}
        >
          Выбрать…
        </button>
        {value && defaultAbs && value.replace(/\\/g, "/") !== defaultAbs.replace(/\\/g, "/") ? (
          <button
            type="button"
            className="btn secondary"
            disabled={disabled || !defaultAbs}
            onClick={() => onChange(defaultAbs)}
          >
            По умолчанию
          </button>
        ) : null}
      </div>
      <p className="hint">
        Только внутри результатов · {KIND_LABEL[kind]} ({baseRel || "…"}).
      </p>
      {error && !open ? <div className="error-banner">{error}</div> : null}

      {open ? (
        <div className="modal-backdrop" onClick={() => setOpen(false)}>
          <div
            className="modal-card stack source-browser"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="page-header">
              <h3 className="group-title">Папка результатов</h3>
              <button
                type="button"
                className="btn secondary"
                onClick={() => setOpen(false)}
              >
                Закрыть
              </button>
            </div>
            <p className="hint">{cwd || baseRel}</p>
            {diskHint ? <p className="disk-usage">{diskHint}</p> : null}
            {error ? <div className="error-banner">{error}</div> : null}
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              <button
                type="button"
                className="btn secondary"
                disabled={parent === null || busy}
                onClick={() => void loadDir(parent ?? baseRel)}
              >
                ← Назад
              </button>
              <button
                type="button"
                className="btn secondary"
                disabled={busy}
                onClick={() => {
                  setMkdirName("");
                  setMkdirError("");
                  setMkdirOpen(true);
                }}
              >
                Новая папка…
              </button>
              <button
                type="button"
                className="btn"
                disabled={busy || !(cwdAbs || defaultAbs)}
                onClick={confirmCwd}
              >
                Выбрать эту папку
              </button>
            </div>
            <div className="source-browser-list">
              {entries.length === 0 ? (
                <div className="list-item hint">
                  {busy ? "Загрузка…" : "Нет подпапок — можно выбрать текущую."}
                </div>
              ) : (
                entries.map((e) => (
                  <button
                    key={e.path}
                    type="button"
                    className="source-browser-row dir"
                    disabled={busy}
                    onClick={() => void loadDir(e.path)}
                    style={{ width: "100%", textAlign: "left" }}
                  >
                    <span className="source-browser-name">📁 {e.name}</span>
                    <span className="hint">открыть</span>
                  </button>
                ))
              )}
            </div>
          </div>
        </div>
      ) : null}

      {mkdirOpen ? (
        <div
          className="modal-backdrop modal-backdrop--nested"
          onClick={() => !busy && setMkdirOpen(false)}
        >
          <div
            className="modal-card stack mkdir-dialog"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
          >
            <h3 className="group-title">Новая папка</h3>
            <p className="hint">Внутри: {cwd || baseRel}</p>
            <input
              ref={mkdirRef}
              className="field"
              value={mkdirName}
              onChange={(e) => {
                setMkdirName(e.target.value);
                if (mkdirError) setMkdirError("");
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void submitMkdir();
                }
              }}
              placeholder="Имя папки"
              disabled={busy}
            />
            {mkdirError ? <div className="error-banner">{mkdirError}</div> : null}
            <div className="row" style={{ gap: 8 }}>
              <button
                type="button"
                className="btn secondary"
                disabled={busy}
                onClick={() => setMkdirOpen(false)}
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
    </div>
  );
}
