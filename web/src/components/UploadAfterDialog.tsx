import { useEffect, useMemo, useState } from "react";
import { api, type Platform, type Profile } from "../api/client";
import { TitleVariablesHint } from "./TitleVariablesHint";

export type UploadAfterChoice = {
  profileIds: string[];
  title: string;
  description: string;
  headless: boolean;
  maxBrowsers: number;
};

type Mode = "uniquify" | "slicing" | "stitching";

type Props = {
  open: boolean;
  mode: Mode;
  onCancel: () => void;
  onConfirm: (choice: UploadAfterChoice) => void;
};

function dialogTitle(mode: Mode, platform: Platform | null): string {
  const where =
    platform === "instagram"
      ? "Instagram"
      : platform === "yt_inst"
        ? "YouTube / Instagram"
        : "YouTube";
  if (mode === "slicing") return `Загрузка в ${where} после нарезки`;
  if (mode === "stitching") return `Загрузка в ${where} после склейки`;
  return `Загрузка в ${where} после уникализации`;
}

export function UploadAfterDialog({
  open,
  mode,
  onCancel,
  onConfirm,
}: Props) {
  const [platform, setPlatform] = useState<Platform | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [headless, setHeadless] = useState(true);
  const [maxBrowsers, setMaxBrowsers] = useState(3);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const isIg = platform === "instagram";

  useEffect(() => {
    if (!open) return;
    setError("");
    setSearch("");
    setLoading(true);
    void (async () => {
      try {
        const [plat, prof, settings] = await Promise.all([
          api.getPlatform(),
          api.listProfiles(),
          api.getSettings(),
        ]);
        setPlatform(plat.platform);
        setProfiles(prof.profiles || []);
        setHeadless(
          Boolean(settings.values["antydetect/dolphin_headless"] ?? true),
        );
        setMaxBrowsers(
          Number(settings.values["antydetect/max_concurrent_browsers"] ?? 3) ||
            3,
        );
        const savedTitle = String(settings.values["upload_title"] ?? "");
        const savedDesc = String(settings.values["upload_description"] ?? "");
        if (savedTitle) setTitle(savedTitle);
        if (savedDesc) setDescription(savedDesc);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    })();
  }, [open]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return profiles;
    return profiles.filter(
      (p) =>
        p.id.toLowerCase().includes(q) ||
        (p.name || "").toLowerCase().includes(q) ||
        JSON.stringify(p.tags || [])
          .toLowerCase()
          .includes(q),
    );
  }, [profiles, search]);

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const confirm = () => {
    setError("");
    const ids = [...selected];
    const t = title.trim();
    if (ids.length && !isIg && !t) {
      setError("Название видео обязательно для загрузки в YouTube.");
      return;
    }
    onConfirm({
      profileIds: ids,
      title: t,
      description: isIg ? "" : description.trim(),
      headless,
      maxBrowsers,
    });
  };

  if (!open) return null;

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div
        className="modal-card stack upload-after-dialog"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="page-header">
          <h3 className="group-title">{dialogTitle(mode, platform)}</h3>
          <button type="button" className="btn secondary" onClick={onCancel}>
            Отмена
          </button>
        </div>
        <p className="hint">
          Отметьте профили для залива. Если ничего не выбрать — только обработка
          без залива.
        </p>
        {error ? <div className="error-banner">{error}</div> : null}
        {loading ? <p className="hint">Загрузка профилей…</p> : null}

        <label className="hint">
          {isIg ? "Подпись" : "Название"}{" "}
          <TitleVariablesHint onInsert={(tok) => setTitle((v) => v + tok)} />
        </label>
        <input
          className="field"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder={
            isIg
              ? "Подпись к Reels (необязательно). {date}, {profile}…"
              : "Название ({date}, {profile}, {video}, {index}…)"
          }
          autoFocus
        />

        {!isIg ? (
          <>
            <label className="hint">Описание</label>
            <textarea
              className="field"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={2}
              placeholder="Описание (необязательно)…"
            />
          </>
        ) : null}

        <div className="row">
          <label className="check">
            <input
              type="checkbox"
              checked={headless}
              onChange={(e) => setHeadless(e.target.checked)}
            />
            Headless
          </label>
          <label className="hint" style={{ display: "flex", gap: 8, alignItems: "center" }}>
            Браузеров
            <input
              className="field"
              style={{ width: 72 }}
              type="number"
              min={1}
              max={10}
              value={maxBrowsers}
              onChange={(e) => setMaxBrowsers(Number(e.target.value) || 1)}
            />
          </label>
        </div>

        <label className="hint">Поиск профилей</label>
        <input
          className="field"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="имя / id / тег"
        />
        <div className="row">
          <button
            type="button"
            className="btn secondary"
            onClick={() => setSelected(new Set(filtered.map((p) => p.id)))}
          >
            Выделить видимые
          </button>
          <button
            type="button"
            className="btn secondary"
            onClick={() => setSelected(new Set())}
          >
            Снять
          </button>
          <span className="hint">
            Выбрано: {selected.size}
            {selected.size === 0 ? " — без залива" : ""}
          </span>
        </div>

        <div className="list-panel upload-after-profiles">
          {filtered.length === 0 ? (
            <div className="list-item hint">
              {loading ? "…" : "Нет профилей (проверьте антидетект в Настройках)."}
            </div>
          ) : (
            filtered.map((p) => (
              <label
                key={p.id}
                className={`list-item ${selected.has(p.id) ? "active" : ""}`}
                style={{ display: "flex", gap: 10, cursor: "pointer" }}
              >
                <input
                  type="checkbox"
                  checked={selected.has(p.id)}
                  onChange={() => toggle(p.id)}
                />
                <span>
                  <div style={{ fontWeight: 600, color: "var(--title)" }}>
                    {p.name || p.id}
                  </div>
                  <div className="hint">{p.id}</div>
                </span>
              </label>
            ))
          )}
        </div>

        <div className="row" style={{ justifyContent: "flex-end" }}>
          <button type="button" className="btn danger" onClick={onCancel}>
            Отмена
          </button>
          <button type="button" className="btn" onClick={confirm}>
            Старт
          </button>
        </div>
      </div>
    </div>
  );
}
