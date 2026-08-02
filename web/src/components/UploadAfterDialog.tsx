import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Platform, type Profile } from "../api/client";
import { usePaintSelectList } from "../hooks/usePaintSelectList";
import {
  profileHasAccountData,
  profileHasOldestChannel,
  profileHasTagError,
  tagFilterClass,
  tagList,
  tagPillClass,
} from "../lib/profileTags";
import { TitleVariablesHint } from "./TitleVariablesHint";

export type UploadAfterChoice = {
  profileIds: string[];
  title: string;
  description: string;
  headless: boolean;
  maxBrowsers: number;
  publishBeforeChecks: boolean;
  keepStudioTitle: boolean;
  uploadAsReady: boolean;
  schedulePublish: boolean;
  scheduleTimesIso: string[];
  scheduleWarmupShorts: boolean;
  scheduleWarmupRecommendations: boolean;
  scheduleWarmupSearchQuery: string;
  deleteAfterUpload: boolean;
};

type Mode = "uniquify" | "slicing" | "stitching";

type Props = {
  open: boolean;
  mode: Mode;
  /** UI platform (source of truth — do not re-fetch from API). */
  platform: Platform;
  onCancel: () => void;
  onConfirm: (choice: UploadAfterChoice) => void;
};

type SelectFilter =
  | "all"
  | "no_errors"
  | "with_errors"
  | "no_account_data"
  | "no_oldest_channel";

function dialogTitle(mode: Mode, platform: Platform): string {
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

function defaultScheduleLocal(): string {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(10, 0, 0, 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function addHoursLocal(isoLocal: string, hours: number): string {
  const d = new Date(isoLocal);
  if (Number.isNaN(d.getTime())) return defaultScheduleLocal();
  d.setHours(d.getHours() + hours);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** datetime-local → ISO naive (сервер трактует как МСК). */
function localToIsoNaive(local: string): string {
  const s = (local || "").trim();
  if (!s) return "";
  return s.length === 16 ? `${s}:00` : s;
}

export function UploadAfterDialog({
  open,
  mode,
  platform,
  onCancel,
  onConfirm,
}: Props) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [headless, setHeadless] = useState(true);
  const [maxBrowsers, setMaxBrowsers] = useState(3);
  const [publishBeforeChecks, setPublishBeforeChecks] = useState(true);
  const [keepStudioTitle, setKeepStudioTitle] = useState(false);
  const [uploadAsReady, setUploadAsReady] = useState(false);
  const [schedulePublish, setSchedulePublish] = useState(false);
  const [scheduleTimes, setScheduleTimes] = useState<string[]>([
    defaultScheduleLocal(),
  ]);
  const [scheduleWarmupShorts, setScheduleWarmupShorts] = useState(true);
  const [scheduleWarmupRecommendations, setScheduleWarmupRecommendations] =
    useState(true);
  const [scheduleWarmupSearchQuery, setScheduleWarmupSearchQuery] = useState("");
  const [deleteAfterUpload, setDeleteAfterUpload] = useState(false);
  const [search, setSearch] = useState("");
  const [tagFilter, setTagFilter] = useState<Set<string>>(new Set());
  const [showTagModal, setShowTagModal] = useState(false);
  const [showSelectMenu, setShowSelectMenu] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const isIg = platform === "instagram";
  const showYtOptions = !isIg;

  useEffect(() => {
    if (!open) return;
    setError("");
    setSearch("");
    setTagFilter(new Set());
    setShowTagModal(false);
    setShowSelectMenu(false);
    setSelected(new Set());
    setSchedulePublish(false);
    setScheduleTimes([defaultScheduleLocal()]);
    setLoading(true);
    void (async () => {
      try {
        // Keep API platform in sync with UI (survives API restart / multi-worker).
        try {
          await api.setPlatform(platform);
        } catch {
          /* optional */
        }
        const [prof, settings] = await Promise.all([
          api.listProfiles(),
          api.getSettings(),
        ]);
        setProfiles(prof.profiles || []);
        const v = settings.values;
        setHeadless(Boolean(v["antydetect/dolphin_headless"] ?? true));
        setMaxBrowsers(
          Number(v["antydetect/max_concurrent_browsers"] ?? 3) || 3,
        );
        setUploadAsReady(Boolean(v["upload_as_ready"] ?? false));
        const delKey =
          mode === "slicing"
            ? "slice/delete_after_upload"
            : mode === "stitching"
              ? "stitch/delete_after_upload"
              : "delete_after_upload";
        setDeleteAfterUpload(Boolean(v[delKey] ?? false));
        setPublishBeforeChecks(true);
        setKeepStudioTitle(false);
        setScheduleWarmupShorts(true);
        setScheduleWarmupRecommendations(true);
        setScheduleWarmupSearchQuery("");
        const savedTitle = String(v["upload_title"] ?? "");
        const savedDesc = String(v["upload_description"] ?? "");
        if (savedTitle) setTitle(savedTitle);
        if (savedDesc) setDescription(savedDesc);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    })();
  }, [open, mode, platform]);

  const allTags = useMemo(() => {
    const s = new Set<string>();
    for (const p of profiles) for (const t of tagList(p.tags)) s.add(t);
    return [...s].sort();
  }, [profiles]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return profiles.filter((p) => {
      const tags = tagList(p.tags);
      if (tagFilter.size && ![...tagFilter].every((t) => tags.includes(t))) {
        return false;
      }
      if (!q) return true;
      return (
        p.id.toLowerCase().includes(q) ||
        (p.name || "").toLowerCase().includes(q) ||
        tags.some((t) => t.toLowerCase().includes(q))
      );
    });
  }, [profiles, search, tagFilter]);

  const paint = useCallback((id: string, paintSelect: boolean) => {
    setSelected((prev) => {
      const has = prev.has(id);
      if (paintSelect && has) return prev;
      if (!paintSelect && !has) return prev;
      const next = new Set(prev);
      if (paintSelect) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  const { listRef, onPointerDown } = usePaintSelectList({
    isSelected: (key) => selected.has(key),
    paint,
  });

  const applySelectFilter = (modeFilter: SelectFilter) => {
    const ids = filtered.map((p) => p.id);
    let next: Set<string>;
    if (modeFilter === "all") {
      next = new Set(ids);
    } else if (modeFilter === "no_errors") {
      next = new Set(
        filtered.filter((p) => !profileHasTagError(p.tags)).map((p) => p.id),
      );
    } else if (modeFilter === "with_errors") {
      next = new Set(
        filtered.filter((p) => profileHasTagError(p.tags)).map((p) => p.id),
      );
    } else if (modeFilter === "no_account_data") {
      next = new Set(
        filtered
          .filter((p) => !profileHasAccountData(p.custom_data, platform))
          .map((p) => p.id),
      );
    } else {
      next = new Set(
        filtered
          .filter((p) => !profileHasOldestChannel(p.custom_data))
          .map((p) => p.id),
      );
    }
    setSelected(next);
    setShowSelectMenu(false);
  };

  const confirm = () => {
    setError("");
    const ids = [...selected];
    const t = title.trim();
    if (ids.length && showYtOptions && !keepStudioTitle && !t) {
      setError("Название видео обязательно для загрузки в YouTube.");
      return;
    }
    if (
      showYtOptions &&
      schedulePublish &&
      scheduleWarmupShorts &&
      !scheduleWarmupRecommendations &&
      !scheduleWarmupSearchQuery.trim()
    ) {
      setError(
        "Укажите поисковый запрос для прогрева Shorts или включите «Рекомендации Shorts».",
      );
      return;
    }
    const timesIso =
      showYtOptions && schedulePublish
        ? scheduleTimes.map(localToIsoNaive).filter(Boolean)
        : [];
    if (showYtOptions && schedulePublish && !timesIso.length) {
      setError("Укажите хотя бы одно время отложки (МСК).");
      return;
    }
    onConfirm({
      profileIds: ids,
      title: keepStudioTitle ? "" : t,
      description: isIg ? "" : description.trim(),
      headless,
      maxBrowsers,
      publishBeforeChecks: isIg ? true : publishBeforeChecks,
      keepStudioTitle: isIg ? false : keepStudioTitle,
      uploadAsReady,
      schedulePublish: isIg ? false : schedulePublish,
      scheduleTimesIso: timesIso,
      scheduleWarmupShorts:
        !isIg && schedulePublish ? scheduleWarmupShorts : false,
      scheduleWarmupRecommendations: scheduleWarmupRecommendations,
      scheduleWarmupSearchQuery: scheduleWarmupSearchQuery.trim(),
      deleteAfterUpload,
    });
  };

  if (!open) return null;

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div
        className="modal-card upload-after-dialog"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="upload-after-top stack">
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
            {!keepStudioTitle ? (
              <TitleVariablesHint onInsert={(tok) => setTitle((v) => v + tok)} />
            ) : null}
          </label>
          <input
            className="field"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            disabled={keepStudioTitle}
            placeholder={
              keepStudioTitle
                ? "Название не вводится — из Studio (настройки канала или имя файла)…"
                : isIg
                  ? "Подпись к Reels (необязательно). {date}, {profile}…"
                  : "Название ({date}, {profile}, {video}, {index}…)"
            }
            autoFocus={!keepStudioTitle}
          />

          {showYtOptions ? (
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

          <div className="stack" style={{ gap: 8 }}>
            {showYtOptions ? (
              <>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={publishBeforeChecks}
                    onChange={(e) => setPublishBeforeChecks(e.target.checked)}
                  />
                  Опубликовать до проверок
                </label>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={keepStudioTitle}
                    onChange={(e) => setKeepStudioTitle(e.target.checked)}
                  />
                  Название из настроек/названия файлов
                </label>
              </>
            ) : null}
            <label
              className="check"
              title={
                "Если включено: обработка в 2 потока, залив стартует после запаса " +
                "готовых видео (число выбранных профилей × 2). Например, 5 профилей — " +
                "после 10 роликов. Если всего видео меньше этого запаса — ждём, пока " +
                "обработаются все. Дальше новые ролики сразу идут в очередь.\n" +
                "Если выключено: сначала обрабатываются все видео, затем начинается залив."
              }
            >
              <input
                type="checkbox"
                checked={uploadAsReady}
                onChange={(e) => setUploadAsReady(e.target.checked)}
              />
              Заливать по мере готовности
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={deleteAfterUpload}
                onChange={(e) => setDeleteAfterUpload(e.target.checked)}
              />
              Удалять после залива
            </label>
            {showYtOptions ? (
              <label className="check">
                <input
                  type="checkbox"
                  checked={schedulePublish}
                  onChange={(e) => setSchedulePublish(e.target.checked)}
                />
                Опубликовать в отложку
              </label>
            ) : null}
          </div>

          {showYtOptions && schedulePublish ? (
            <div className="stack" style={{ gap: 8, marginLeft: 8 }}>
              <p className="hint">
                Время по Москве. На каждый профиль — по одному видео на каждое
                время.
              </p>
              {scheduleTimes.map((t, i) => (
                <label
                  key={i}
                  className="hint"
                  style={{ display: "flex", gap: 8, alignItems: "center" }}
                >
                  Время {i + 1} (МСК)
                  <input
                    className="field"
                    type="datetime-local"
                    value={t}
                    onChange={(e) => {
                      const next = [...scheduleTimes];
                      next[i] = e.target.value;
                      setScheduleTimes(next);
                    }}
                    style={{ flex: 1 }}
                  />
                </label>
              ))}
              <div className="row" style={{ gap: 8 }}>
                <button
                  type="button"
                  className="btn secondary"
                  onClick={() =>
                    setScheduleTimes((prev) => [
                      ...prev,
                      addHoursLocal(
                        prev[prev.length - 1] || defaultScheduleLocal(),
                        5,
                      ),
                    ])
                  }
                >
                  Добавить время
                </button>
                <button
                  type="button"
                  className="btn secondary"
                  disabled={scheduleTimes.length <= 1}
                  onClick={() =>
                    setScheduleTimes((prev) =>
                      prev.length <= 1 ? prev : prev.slice(0, -1),
                    )
                  }
                >
                  Убрать время
                </button>
              </div>
              <label className="check">
                <input
                  type="checkbox"
                  checked={scheduleWarmupShorts}
                  onChange={(e) => setScheduleWarmupShorts(e.target.checked)}
                />
                Прогрев Shorts во второй вкладке
              </label>
              {scheduleWarmupShorts ? (
                <>
                  <label className="check" style={{ marginLeft: 16 }}>
                    <input
                      type="checkbox"
                      checked={scheduleWarmupRecommendations}
                      onChange={(e) =>
                        setScheduleWarmupRecommendations(e.target.checked)
                      }
                    />
                    Рекомендации Shorts
                  </label>
                  {!scheduleWarmupRecommendations ? (
                    <label
                      className="hint"
                      style={{
                        display: "flex",
                        gap: 8,
                        alignItems: "center",
                        marginLeft: 16,
                      }}
                    >
                      Поисковый запрос
                      <input
                        className="field"
                        style={{ flex: 1 }}
                        value={scheduleWarmupSearchQuery}
                        onChange={(e) =>
                          setScheduleWarmupSearchQuery(e.target.value)
                        }
                        placeholder="Текст для поиска Shorts"
                      />
                    </label>
                  ) : null}
                </>
              ) : null}
            </div>
          ) : null}

          <div className="row">
            <label className="check">
              <input
                type="checkbox"
                checked={headless}
                onChange={(e) => setHeadless(e.target.checked)}
              />
              Headless (без окна браузера)
            </label>
            <label
              className="hint"
              style={{ display: "flex", gap: 8, alignItems: "center" }}
            >
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
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            <button
              type="button"
              className="btn secondary"
              onClick={() => setShowTagModal(true)}
            >
              По тэгам{tagFilter.size ? ` (${tagFilter.size})` : ""}
            </button>
            <div style={{ position: "relative" }}>
              <button
                type="button"
                className="btn secondary"
                onClick={() => setShowSelectMenu((v) => !v)}
              >
                Выделить…
              </button>
              {showSelectMenu ? (
                <div className="upload-after-select-menu">
                  <button type="button" onClick={() => applySelectFilter("all")}>
                    Все видимые
                  </button>
                  <button
                    type="button"
                    onClick={() => applySelectFilter("no_errors")}
                  >
                    Без ошибок в статусах
                  </button>
                  <button
                    type="button"
                    onClick={() => applySelectFilter("with_errors")}
                  >
                    С ошибками в статусах
                  </button>
                  <button
                    type="button"
                    onClick={() => applySelectFilter("no_account_data")}
                  >
                    Без данных в учётке
                  </button>
                  {!isIg ? (
                    <button
                      type="button"
                      onClick={() => applySelectFilter("no_oldest_channel")}
                    >
                      Без старейшего канала
                    </button>
                  ) : null}
                </div>
              ) : null}
            </div>
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
              {search.trim() || tagFilter.size
                ? ` · показано ${filtered.length} из ${profiles.length}`
                : ""}
            </span>
          </div>
        </div>

        <div
          ref={listRef}
          className="list-panel upload-after-profiles source-browser-list--select"
          onPointerDown={onPointerDown}
        >
          {filtered.length === 0 ? (
            <div className="list-item hint">
              {loading
                ? "…"
                : "Нет профилей (проверьте антидетект в Настройках)."}
            </div>
          ) : (
            filtered.map((p) => {
              const tags = tagList(p.tags);
              return (
                <div
                  key={p.id}
                  data-entry-path={p.id}
                  className={`list-item ${selected.has(p.id) ? "active" : ""}`}
                  style={{ display: "flex", gap: 10, cursor: "pointer" }}
                >
                  <input
                    type="checkbox"
                    className="source-browser-check"
                    checked={selected.has(p.id)}
                    readOnly
                    tabIndex={-1}
                  />
                  <span style={{ flex: 1 }}>
                    <div style={{ fontWeight: 600, color: "var(--title)" }}>
                      {p.name || p.id}
                    </div>
                    <div className="hint">{p.id}</div>
                    {tags.length ? (
                      <div className="row-tags">
                        {tags.map((t) => (
                          <span className={tagPillClass(t)} key={t}>
                            {t}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </span>
                </div>
              );
            })
          )}
        </div>

        <div
          className="row upload-after-footer"
          style={{ justifyContent: "flex-end" }}
        >
          <button type="button" className="btn danger" onClick={onCancel}>
            Отмена
          </button>
          <button type="button" className="btn" onClick={confirm}>
            Старт
          </button>
        </div>
      </div>

      {showTagModal ? (
        <div
          className="modal-backdrop"
          style={{ zIndex: 60 }}
          onClick={() => setShowTagModal(false)}
        >
          <div
            className="modal-card stack"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="page-header">
              <h3 className="group-title">Фильтр по тэгам</h3>
              <button
                type="button"
                className="btn secondary"
                onClick={() => setShowTagModal(false)}
              >
                OK
              </button>
            </div>
            <div className="row">
              <button
                type="button"
                className="btn secondary"
                onClick={() => setTagFilter(new Set(allTags))}
              >
                Все
              </button>
              <button
                type="button"
                className="btn secondary"
                onClick={() => setTagFilter(new Set())}
              >
                Сбросить фильтр
              </button>
            </div>
            <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
              {allTags.map((t) => (
                <button
                  key={t}
                  type="button"
                  className={tagFilterClass(t, tagFilter.has(t))}
                  onClick={() => {
                    setTagFilter((prev) => {
                      const next = new Set(prev);
                      if (next.has(t)) next.delete(t);
                      else next.add(t);
                      return next;
                    });
                  }}
                >
                  {t}
                </button>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
