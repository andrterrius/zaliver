import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Platform, type Profile } from "../api/client";
import { useJobPoll } from "../hooks/useJobPoll";
import { usePersistedJobId } from "../hooks/usePersistedJobId";
import { ProgressBar } from "../components/ProgressBar";
import { JobLogBox } from "../components/JobLogBox";
import { ToggleSwitch } from "../components/ToggleSwitch";

type Props = { platform: Platform };

type JobModal =
  | null
  | "warmup"
  | "promote"
  | "cookie-farm";

const JOB_BUTTONS: { path: string; label: string; igOnly?: boolean; modal?: JobModal }[] =
  [
    { path: "availability", label: "Проверить доступность" },
    { path: "instagram-register", label: "Зарегать акки", igOnly: true },
    { path: "instagram-2fa", label: "Подключить 2FA", igOnly: true },
    { path: "warmup", label: "Прогрев", modal: "warmup" },
    { path: "promote", label: "Продвижение", modal: "promote" },
    { path: "cookie-farm", label: "Фарм Cookie", modal: "cookie-farm" },
  ];

function tagList(tags: unknown[]): string[] {
  return (tags || [])
    .map((t) => {
      if (typeof t === "string") return t;
      if (t && typeof t === "object" && "name" in t)
        return String((t as { name: unknown }).name);
      return String(t);
    })
    .filter(Boolean);
}

export function ProfilesPage({ platform }: Props) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [kind, setKind] = useState("local");
  const [baseUrl, setBaseUrl] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [tagFilter, setTagFilter] = useState<Set<string>>(new Set());
  const [showTagModal, setShowTagModal] = useState(false);
  const [modal, setModal] = useState<JobModal>(null);
  const [error, setError] = useState("");
  const [jobId, setJobId] = usePersistedJobId("profiles");
  const { job } = useJobPoll(jobId);

  // Warmup
  const [shortsCount, setShortsCount] = useState(10);
  const [likePct, setLikePct] = useState(10);
  const [subPct, setSubPct] = useState(10);
  const [watchMin, setWatchMin] = useState(5);
  const [watchMax, setWatchMax] = useState(25);
  const [watchFull, setWatchFull] = useState(false);
  const [recs, setRecs] = useState(true);
  const [searchQ, setSearchQ] = useState("");

  // Promote
  const [promoSubscribe, setPromoSubscribe] = useState(false);
  const [promoCount, setPromoCount] = useState(10);
  const [promoLike, setPromoLike] = useState(10);
  const [promoComments, setPromoComments] = useState(false);
  const [promoCommentPct, setPromoCommentPct] = useState(50);
  const [promoCommentText, setPromoCommentText] = useState("");

  // Cookie farm
  const [usePreset, setUsePreset] = useState(true);
  const [sitesCount, setSitesCount] = useState(10);
  const [cookieWatchMin, setCookieWatchMin] = useState(15);
  const [cookieWatchMax, setCookieWatchMax] = useState(45);
  const [customDomains, setCustomDomains] = useState("");

  const refresh = useCallback(async () => {
    setError("");
    try {
      const res = await api.listProfiles();
      setProfiles(res.profiles);
      if (res.kind) setKind(String(res.kind));
      if (res.base_url) setBaseUrl(String(res.base_url));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

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

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const buildBaseBody = () => {
    const ids = [...selected];
    const profiles_custom_data: Record<string, Record<string, unknown>> = {};
    for (const p of profiles) {
      if (ids.includes(p.id) && p.custom_data && Object.keys(p.custom_data).length) {
        profiles_custom_data[p.id] = p.custom_data;
      }
    }
    return {
      profile_ids: ids,
      kind,
      headless: true,
      ...(Object.keys(profiles_custom_data).length
        ? { profiles_custom_data }
        : {}),
    };
  };

  const startJob = async (path: string, extra: Record<string, unknown> = {}) => {
    setError("");
    if (![...selected].length) {
      setError("Отметьте профили.");
      return;
    }
    try {
      const res = await api.startProfileJob(path, {
        ...buildBaseBody(),
        ...extra,
      });
      setJobId(res.id);
      setModal(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const onJobClick = (path: string, m?: JobModal) => {
    if (m) {
      setModal(m);
      return;
    }
    void startJob(path);
  };

  const confirmModal = () => {
    if (modal === "warmup") {
      const isIg = platform === "instagram";
      void startJob("warmup", {
        shorts: {
          shorts_count: shortsCount,
          like_probability_pct: likePct,
          subscribe_probability_pct: subPct,
          shorts_watch_min_s: watchMin,
          shorts_watch_max_s: watchMax,
          watch_full_video: watchFull,
          shorts_recommendations: recs,
          shorts_search_query: searchQ,
        },
        reels: {
          reels_count: shortsCount,
          like_probability_pct: likePct,
          follow_probability_pct: subPct,
          watch_min_s: watchMin,
          watch_max_s: watchMax,
          watch_full: watchFull,
          reels_recommendations: recs,
          reels_search_query: searchQ,
        },
        ...(isIg ? {} : {}),
      });
    } else if (modal === "promote") {
      void startJob("promote", {
        settings: {
          subscribe_to_channels: promoSubscribe,
          shorts_count: promoCount,
          like_probability_pct: promoLike,
          enable_comments: promoComments,
          comment_probability_pct: promoCommentPct,
          comments: promoCommentText
            .split("\n")
            .map((s) => s.trim())
            .filter(Boolean),
        },
      });
    } else if (modal === "cookie-farm") {
      void startJob("cookie-farm", {
        settings: {
          use_preset_domains: usePreset,
          domains: customDomains
            .split(/[\n,]/)
            .map((s) => s.trim())
            .filter(Boolean),
          sites_count: sitesCount,
          watch_min_s: cookieWatchMin,
          watch_max_s: cookieWatchMax,
        },
      });
    }
  };

  const title =
    platform === "instagram" ? "Профили Instagram" : "Профили антидетекта";

  return (
    <div className="stack">
      <div className="page-header">
        <h1 className="title">{title}</h1>
        <button type="button" className="btn secondary" onClick={refresh}>
          Обновить
        </button>
      </div>
      {baseUrl ? (
        <p className="hint">
          Режим: {kind}
          {baseUrl ? ` · ${baseUrl}` : ""}
        </p>
      ) : null}
      {error ? <div className="error-banner">{error}</div> : null}
      {job ? (
        <ProgressBar
          value={job.progress.current}
          max={Math.max(1, job.progress.total)}
          label={`${job.kind}: ${job.status} ${job.progress.current}/${job.progress.total}`}
        />
      ) : null}

      <div className="row" style={{ flexWrap: "wrap" }}>
        <input
          className="field"
          style={{ maxWidth: 220 }}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Поиск…"
        />
        <button
          type="button"
          className="btn secondary"
          onClick={() => setShowTagModal(true)}
        >
          По тэгам{tagFilter.size ? ` (${tagFilter.size})` : ""}
        </button>
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
          Снять выделение
        </button>
      </div>

      <div className="row" style={{ flexWrap: "wrap" }}>
        {JOB_BUTTONS.filter((j) => !j.igOnly || platform === "instagram").map(
          (j) => (
            <button
              key={j.path}
              type="button"
              className="btn secondary"
              onClick={() => onJobClick(j.path, j.modal)}
            >
              {j.label}
            </button>
          ),
        )}
      </div>

      <div className="list-panel">
        {filtered.length === 0 ? (
          <div className="list-item hint">
            Нет профилей. Убедитесь, что локальный антидетект запущен.
          </div>
        ) : (
          filtered.map((p) => {
            const tags = tagList(p.tags);
            return (
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
                <span style={{ flex: 1 }}>
                  <div style={{ fontWeight: 600, color: "var(--title)" }}>
                    {p.name || p.id}
                  </div>
                  <div className="hint">{p.id}</div>
                  {tags.length ? (
                    <div className="hint">{tags.join(", ")}</div>
                  ) : null}
                </span>
              </label>
            );
          })
        )}
      </div>
      {job?.logs?.length ? (
        <JobLogBox lines={job.logs.slice(-40)} emptyHint="" />
      ) : null}

      {showTagModal ? (
        <div className="modal-backdrop" onClick={() => setShowTagModal(false)}>
          <div className="modal-card stack" onClick={(e) => e.stopPropagation()}>
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
            {allTags.map((t) => (
              <label key={t} className="check">
                <input
                  type="checkbox"
                  checked={tagFilter.has(t)}
                  onChange={() => {
                    setTagFilter((prev) => {
                      const next = new Set(prev);
                      if (next.has(t)) next.delete(t);
                      else next.add(t);
                      return next;
                    });
                  }}
                />
                {t}
              </label>
            ))}
          </div>
        </div>
      ) : null}

      {modal ? (
        <div className="modal-backdrop" onClick={() => setModal(null)}>
          <div className="modal-card stack" onClick={(e) => e.stopPropagation()}>
            <div className="page-header">
              <h3 className="group-title">
                {modal === "warmup"
                  ? platform === "instagram"
                    ? "Прогрев Reels"
                    : "Прогрев Shorts"
                  : modal === "promote"
                    ? "Продвижение"
                    : "Фарм Cookie"}
              </h3>
              <button
                type="button"
                className="btn secondary"
                onClick={() => setModal(null)}
              >
                Отмена
              </button>
            </div>
            {modal === "warmup" ? (
              <>
                <label className="hint">Количество</label>
                <input
                  className="field"
                  type="number"
                  min={1}
                  value={shortsCount}
                  onChange={(e) => setShortsCount(Number(e.target.value) || 1)}
                />
                <label className="hint">Лайк %</label>
                <input
                  className="field"
                  type="number"
                  min={0}
                  max={100}
                  value={likePct}
                  onChange={(e) => setLikePct(Number(e.target.value) || 0)}
                />
                <label className="hint">
                  {platform === "instagram" ? "Подписка %" : "Подписка %"}
                </label>
                <input
                  className="field"
                  type="number"
                  min={0}
                  max={100}
                  value={subPct}
                  onChange={(e) => setSubPct(Number(e.target.value) || 0)}
                />
                <label className="hint">Просмотр мин/макс (сек)</label>
                <div className="row">
                  <input
                    className="field"
                    type="number"
                    value={watchMin}
                    onChange={(e) => setWatchMin(Number(e.target.value) || 1)}
                  />
                  <input
                    className="field"
                    type="number"
                    value={watchMax}
                    onChange={(e) => setWatchMax(Number(e.target.value) || 1)}
                  />
                </div>
                <ToggleSwitch
                  label="Смотреть до конца"
                  checked={watchFull}
                  onChange={setWatchFull}
                />
                <ToggleSwitch
                  label="Рекомендации"
                  checked={recs}
                  onChange={setRecs}
                />
                <label className="hint">Поисковый запрос</label>
                <input
                  className="field"
                  value={searchQ}
                  onChange={(e) => setSearchQ(e.target.value)}
                />
              </>
            ) : null}
            {modal === "promote" ? (
              <>
                <ToggleSwitch
                  label="Подписываться на каналы"
                  checked={promoSubscribe}
                  onChange={setPromoSubscribe}
                />
                <label className="hint">Количество Shorts</label>
                <input
                  className="field"
                  type="number"
                  value={promoCount}
                  onChange={(e) => setPromoCount(Number(e.target.value) || 1)}
                />
                <label className="hint">Лайк %</label>
                <input
                  className="field"
                  type="number"
                  value={promoLike}
                  onChange={(e) => setPromoLike(Number(e.target.value) || 0)}
                />
                <ToggleSwitch
                  label="Комментарии"
                  checked={promoComments}
                  onChange={setPromoComments}
                />
                {promoComments ? (
                  <>
                    <label className="hint">Вероятность комментария %</label>
                    <input
                      className="field"
                      type="number"
                      value={promoCommentPct}
                      onChange={(e) =>
                        setPromoCommentPct(Number(e.target.value) || 0)
                      }
                    />
                    <label className="hint">Комментарии (по строке)</label>
                    <textarea
                      className="field"
                      rows={4}
                      value={promoCommentText}
                      onChange={(e) => setPromoCommentText(e.target.value)}
                    />
                  </>
                ) : null}
              </>
            ) : null}
            {modal === "cookie-farm" ? (
              <>
                <ToggleSwitch
                  label="Пресет доменов"
                  checked={usePreset}
                  onChange={setUsePreset}
                />
                {!usePreset ? (
                  <>
                    <label className="hint">Домены</label>
                    <textarea
                      className="field"
                      rows={4}
                      value={customDomains}
                      onChange={(e) => setCustomDomains(e.target.value)}
                    />
                  </>
                ) : null}
                <label className="hint">Число сайтов</label>
                <input
                  className="field"
                  type="number"
                  value={sitesCount}
                  onChange={(e) => setSitesCount(Number(e.target.value) || 1)}
                />
                <label className="hint">Время на сайте мин/макс</label>
                <div className="row">
                  <input
                    className="field"
                    type="number"
                    value={cookieWatchMin}
                    onChange={(e) =>
                      setCookieWatchMin(Number(e.target.value) || 1)
                    }
                  />
                  <input
                    className="field"
                    type="number"
                    value={cookieWatchMax}
                    onChange={(e) =>
                      setCookieWatchMax(Number(e.target.value) || 1)
                    }
                  />
                </div>
              </>
            ) : null}
            <button type="button" className="btn" onClick={confirmModal}>
              Старт
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
