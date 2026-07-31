import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  type Platform,
  type Profile,
  type UploadedItem,
  type UploadSession,
} from "../api/client";
import { useJobPoll } from "../hooks/useJobPoll";
import { usePersistedJobId } from "../hooks/usePersistedJobId";
import { ProgressBar } from "../components/ProgressBar";

type Props = { platform: Platform };
type SortMode = "views" | "likes" | "time";

function formatInt(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return Math.trunc(v)
    .toString()
    .replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}

function sumOptional(values: Array<number | null | undefined>): number | null {
  let total = 0;
  let any = false;
  for (const v of values) {
    if (v == null || Number.isNaN(v)) continue;
    total += Math.trunc(v);
    any = true;
  }
  return any ? total : null;
}

function profileLabel(p: Profile): string {
  const name = (p.name || "").trim();
  return name ? `${name}  (${p.id})` : p.id;
}

export function UploadedPage({ platform }: Props) {
  const [items, setItems] = useState<UploadedItem[]>([]);
  const [sessions, setSessions] = useState<UploadSession[]>([]);
  const [sessionId, setSessionId] = useState<number | "">("");
  const [sort, setSort] = useState<SortMode>("views");
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [checkerProfileId, setCheckerProfileId] = useState("");
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const [jobId, setJobId] = usePersistedJobId("stats_refresh");
  const { job } = useJobPoll(jobId);
  const isIg = platform === "instagram";

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [list, sess, settings, profRes] = await Promise.all([
        api.listUploaded(sessionId === "" ? null : sessionId),
        api.listSessions(),
        api.getSettings(),
        isIg
          ? api.listProfiles()
          : Promise.resolve({ profiles: [] as Profile[] }),
      ]);
      setItems(list);
      setSessions(sess);
      if (isIg) {
        setProfiles(profRes.profiles || []);
        setCheckerProfileId(
          String(settings.values["instagram/stats_checker_profile_id"] ?? ""),
        );
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [sessionId, isIg]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (job?.status === "succeeded" || job?.status === "failed") {
      void refresh();
      setStatus(job.message || job.status);
    }
  }, [job?.status, job?.message, refresh]);

  const sorted = useMemo(() => {
    const copy = [...items];
    const num = (v: number | null | undefined) =>
      v == null || Number.isNaN(v) ? -1 : v;
    if (sort === "views") {
      copy.sort((a, b) => num(b.view_count) - num(a.view_count));
    } else if (sort === "likes") {
      copy.sort((a, b) => num(b.like_count) - num(a.like_count));
    } else {
      copy.sort((a, b) => (b.uploaded_at || "").localeCompare(a.uploaded_at || ""));
    }
    return copy;
  }, [items, sort]);

  const tiles = useMemo(() => {
    const videos = items.length;
    const counted = items.filter((v) => !v.stats_unavailable);
    const views = sumOptional(counted.map((v) => v.view_count));
    const likes = sumOptional(counted.map((v) => v.like_count));
    const comments = sumOptional(counted.map((v) => v.comment_count));
    let zero = 0;
    let over300 = 0;
    for (const v of counted) {
      const vc = v.view_count;
      if (vc === 0) zero += 1;
      if (vc != null && vc >= 300) over300 += 1;
    }
    const plus18 = items.filter((v) => v.age_restricted === true).length;
    const banned = items.filter((v) => v.stats_unavailable).length;
    return { videos, views, likes, comments, zero, over300, plus18, banned };
  }, [items]);

  const checkStats = async () => {
    setError("");
    setStatus("");
    if (sessionId === "" || sessionId <= 0) {
      setError("Выберите сессию — проверка идёт только по видео выбранной сессии.");
      return;
    }
    const vids = [
      ...new Set(
        items
          .map((v) => (v.video_id || "").trim())
          .filter(Boolean),
      ),
    ];
    if (!vids.length) {
      setError("В выбранной сессии нет видео для проверки.");
      return;
    }
    try {
      if (isIg) {
        if (!checkerProfileId.trim()) {
          setError("Выберите профиль для чека статистики.");
          return;
        }
        await api.patchSettings({
          "instagram/stats_checker_profile_id": checkerProfileId.trim(),
        });
      }
      const body: Record<string, unknown> = {
        session_id: sessionId,
        video_ids: vids,
      };
      if (isIg) {
        body.checker_profile_id = checkerProfileId.trim();
      }
      const res = await api.refreshUploadedStats(body);
      setJobId(res.id);
      setStatus(`Проверка статистики: 0 / ${vids.length}…`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const purge = async (filter: "unavailable" | "age_restricted") => {
    const label =
      filter === "unavailable" ? "недоступные" : "18+";
    if (!confirm(`Удалить из базы записи: ${label}? Ролики на площадке не удаляются.`)) {
      return;
    }
    try {
      const r = await api.deleteUploaded({ filter });
      setStatus(`Удалено: ${r.deleted}`);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="stack">
      <div className="page-header">
        <h1 className="title">Залитые видео</h1>
        <div className="row">
          <button
            type="button"
            className="btn secondary"
            onClick={refresh}
            disabled={loading}
          >
            Список
          </button>
          <button
            type="button"
            className="btn"
            onClick={checkStats}
            disabled={sessionId === "" || sessionId <= 0}
            title={
              sessionId === "" || sessionId <= 0
                ? "Сначала выберите сессию"
                : "Проверить статистику видео выбранной сессии"
            }
          >
            ▶ Прочекать
          </button>
        </div>
      </div>
      {error ? <div className="error-banner">{error}</div> : null}
      {status ? <p className="hint">{status}</p> : null}
      {job ? (
        <ProgressBar
          value={job.progress.current}
          max={Math.max(1, job.progress.total)}
          label={`${job.status} ${job.progress.current}/${job.progress.total}`}
        />
      ) : null}

      <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
        <label className="hint">
          Сессия{" "}
          <select
            className="field"
            style={{ maxWidth: 260 }}
            value={sessionId === "" ? "" : String(sessionId)}
            onChange={(e) =>
              setSessionId(e.target.value ? Number(e.target.value) : "")
            }
          >
            <option value="">Все</option>
            {sessions.map((s) => (
              <option key={s.id} value={s.id}>
                #{s.id} · {s.started_at} · ok {s.uploaded_ok}
              </option>
            ))}
          </select>
        </label>
        <label className="hint">
          Сортировка{" "}
          <select
            className="field"
            style={{ maxWidth: 140 }}
            value={sort}
            onChange={(e) => setSort(e.target.value as SortMode)}
          >
            <option value="views">Просмотры</option>
            <option value="likes">Лайки</option>
            <option value="time">Время</option>
          </select>
        </label>
        {isIg ? (
          <label className="hint" style={{ flex: 1, minWidth: 240 }}>
            Аккаунт для чека
            <select
              className="field"
              value={checkerProfileId}
              onChange={(e) => {
                const id = e.target.value;
                setCheckerProfileId(id);
                void api
                  .patchSettings({
                    "instagram/stats_checker_profile_id": id,
                  })
                  .catch(() => {
                    /* keep local selection; save again on check */
                  });
              }}
            >
              <option value="">— не выбран —</option>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {profileLabel(p)}
                </option>
              ))}
              {checkerProfileId &&
              !profiles.some((p) => p.id === checkerProfileId) ? (
                <option value={checkerProfileId}>
                  {checkerProfileId} (нет в списке)
                </option>
              ) : null}
            </select>
          </label>
        ) : null}
      </div>

      <div className="hint" style={{ fontWeight: 600, color: "var(--title)" }}>
        Итого
      </div>
      <div className="stats-tiles">
        <div className="stat-tile">
          <div className="hint">Видео</div>
          <div className="stat-n">{tiles.videos}</div>
        </div>
        <div className="stat-tile">
          <div className="hint">Просмотры</div>
          <div className="stat-n">{formatInt(tiles.views)}</div>
        </div>
        <div className="stat-tile">
          <div className="hint">Лайки</div>
          <div className="stat-n">{formatInt(tiles.likes)}</div>
        </div>
        <div className="stat-tile">
          <div className="hint">Комментарии</div>
          <div className="stat-n">{formatInt(tiles.comments)}</div>
        </div>
      </div>
      <div className="stats-tiles">
        <div className="stat-tile">
          <div className="stat-n">{tiles.zero}</div>
          <div className="hint">С 0 просмотров</div>
        </div>
        <div className="stat-tile">
          <div className="stat-n">{tiles.over300}</div>
          <div className="hint">300+ просмотров</div>
        </div>
        <div className="stat-tile">
          <div className="stat-n">{tiles.plus18}</div>
          <div className="hint">С меткой 18+</div>
        </div>
        <div className="stat-tile">
          <div className="stat-n">{tiles.banned}</div>
          <div className="hint">Забанено / недоступно</div>
        </div>
      </div>

      <div className="row">
        <button
          type="button"
          className="btn secondary"
          onClick={() => purge("unavailable")}
        >
          Удалить из базы: недоступные
        </button>
        <button
          type="button"
          className="btn secondary"
          onClick={() => purge("age_restricted")}
        >
          Удалить из базы: 18+
        </button>
      </div>

      <div className="list-panel">
        {sorted.length === 0 ? (
          <div className="list-item hint">Нет записей о заливах.</div>
        ) : (
          sorted.map((v) => (
            <div key={v.id} className="list-item">
              <div style={{ fontWeight: 600, color: "var(--title)" }}>
                {v.title || v.video_id || "Без названия"}
                {v.age_restricted ? " · 18+" : ""}
                {v.stats_unavailable ? " · недоступно" : ""}
              </div>
              <div className="hint">
                {v.uploaded_at} · профиль {v.profile_id || "—"} · 👁{" "}
                {v.view_count ?? "—"} · ❤ {v.like_count ?? "—"} · 💬{" "}
                {v.comment_count ?? "—"}
              </div>
              {v.url ? (
                <a href={v.url} target="_blank" rel="noreferrer">
                  ↗ Открыть
                </a>
              ) : null}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
