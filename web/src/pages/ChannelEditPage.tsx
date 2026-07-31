import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api, type Platform, type Profile } from "../api/client";
import { useJobPoll } from "../hooks/useJobPoll";
import { usePersistedJobId } from "../hooks/usePersistedJobId";
import { ProgressBar } from "../components/ProgressBar";
import { JobLogBox } from "../components/JobLogBox";
import { TitleVariablesHint } from "../components/TitleVariablesHint";
import { ToggleSwitch } from "../components/ToggleSwitch";

type Props = { platform: Platform };

function cycleLines(text: string): string[] {
  return (text || "")
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function pickCycled(items: string[], index: number): string {
  if (!items.length) return "";
  return items[index % items.length];
}

function insertAtCursor(
  value: string,
  token: string,
  el: HTMLTextAreaElement | null,
): string {
  if (!el) return value + token;
  const start = el.selectionStart ?? value.length;
  const end = el.selectionEnd ?? value.length;
  return value.slice(0, start) + token + value.slice(end);
}

function SectionHead({
  label,
  checked,
  onChange,
  actions,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  actions?: ReactNode;
}) {
  return (
    <div className="channel-section-head">
      <ToggleSwitch label={label} checked={checked} onChange={onChange} />
      {actions ? (
        <div className="channel-section-actions">{actions}</div>
      ) : null}
    </div>
  );
}

export function ChannelEditPage({ platform }: Props) {
  const isIg = platform === "instagram";
  const namesTitle = isIg ? "Юзернейм" : "Название канала";

  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerSelected, setPickerSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");

  const [changeLanguage, setChangeLanguage] = useState(false);
  const [enableAvatar, setEnableAvatar] = useState(true);
  const [noCrop, setNoCrop] = useState(false);
  const [avatarPaths, setAvatarPaths] = useState("");

  const [enableNames, setEnableNames] = useState(true);
  const [namesText, setNamesText] = useState("");

  const [enableDesc, setEnableDesc] = useState(true);
  const [descText, setDescText] = useState("");

  const [enableLink, setEnableLink] = useState(!isIg);
  const [linkTitles, setLinkTitles] = useState("");
  const [linkUrls, setLinkUrls] = useState("");

  const [enableVideoTitle, setEnableVideoTitle] = useState(!isIg);
  const [videoTitles, setVideoTitles] = useState("");

  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [jobId, setJobId] = usePersistedJobId("channel_setup");
  const { job } = useJobPoll(jobId);

  const [aiOpen, setAiOpen] = useState(false);
  const [aiTarget, setAiTarget] = useState<
    "names" | "desc" | "linkTitle" | "video" | null
  >(null);
  const [aiPromptId, setAiPromptId] = useState("");
  const [aiLines, setAiLines] = useState(1);
  const [aiBusy, setAiBusy] = useState(false);
  const [prompts, setPrompts] = useState<
    { id: string; title: string; text: string }[]
  >([]);

  const namesRef = useRef<HTMLTextAreaElement>(null);
  const descRef = useRef<HTMLTextAreaElement>(null);
  const linkTitleRef = useRef<HTMLTextAreaElement>(null);
  const videoRef = useRef<HTMLTextAreaElement>(null);
  const importRef = useRef<HTMLInputElement>(null);
  const importTarget = useRef<"names" | "desc" | "video" | null>(null);

  const running =
    job != null && ["queued", "running"].includes(job.status);

  useEffect(() => {
    if (isIg) {
      setEnableLink(false);
      setEnableVideoTitle(false);
    }
  }, [isIg]);

  useEffect(() => {
    if (job?.status === "succeeded" || job?.status === "failed") {
      setStatus(job.message || job.status);
    } else if (job && running) {
      setStatus(
        `${job.progress.message || job.status}: ${job.progress.current}/${Math.max(1, job.progress.total)}`,
      );
    }
  }, [job, running]);

  const refreshProfiles = useCallback(async () => {
    setError("");
    try {
      const res = await api.listProfiles();
      setProfiles(res.profiles || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void refreshProfiles();
  }, [refreshProfiles]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return profiles;
    return profiles.filter(
      (p) =>
        p.id.toLowerCase().includes(q) ||
        (p.name || "").toLowerCase().includes(q),
    );
  }, [profiles, search]);

  const validateForm = (): string | null => {
    const hasDesc = enableDesc && cycleLines(descText).length > 0;
    const hasNames = enableNames && cycleLines(namesText).length > 0;
    const hasAvatar = enableAvatar && cycleLines(avatarPaths).length > 0;
    const hasVideo = !isIg && enableVideoTitle && cycleLines(videoTitles).length > 0;
    const titles = enableLink && !isIg ? cycleLines(linkTitles) : [];
    const urls = enableLink && !isIg ? cycleLines(linkUrls) : [];
    const hasLinks = titles.length > 0 && urls.length > 0;

    if (
      !hasDesc &&
      !hasNames &&
      !hasAvatar &&
      !hasVideo &&
      !hasLinks &&
      !changeLanguage
    ) {
      return "Включите и заполните хотя бы один раздел или отметьте «Поменять язык».";
    }
    if (enableLink && !isIg) {
      if ((titles.length > 0 && !urls.length) || (urls.length > 0 && !titles.length)) {
        return "Нужны и названия ссылок, и URL (строка = профиль; короткий список зацикливается).";
      }
    }
    return null;
  };

  const openPicker = async () => {
    setError("");
    const err = validateForm();
    if (err) {
      setError(err);
      return;
    }
    await refreshProfiles();
    if (!profiles.length) {
      // refreshProfiles updates state async — re-fetch for guard
      try {
        const res = await api.listProfiles();
        if (!(res.profiles || []).length) {
          setError(
            "Сначала загрузите список профилей (вкладка «Профили» → «Обновить»).",
          );
          return;
        }
        setProfiles(res.profiles);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        return;
      }
    }
    setPickerOpen(true);
  };

  const buildPayload = (ids: string[]) => {
    const nameLines = enableNames ? cycleLines(namesText) : [];
    const descLines = enableDesc ? cycleLines(descText) : [];
    const videoLines =
      !isIg && enableVideoTitle ? cycleLines(videoTitles) : [];
    const avatarLines = enableAvatar ? cycleLines(avatarPaths) : [];
    const titleLines = !isIg && enableLink ? cycleLines(linkTitles) : [];
    const urlLines = !isIg && enableLink ? cycleLines(linkUrls) : [];

    const channel_links: string[][] = [];
    if (titleLines.length && urlLines.length) {
      const n = Math.max(titleLines.length, urlLines.length);
      for (let i = 0; i < n; i++) {
        channel_links.push([
          pickCycled(titleLines, i),
          pickCycled(urlLines, i),
        ]);
      }
    }

    const assignments = ids.map((id, index) => {
      const p = profiles.find((x) => x.id === id);
      const channel_name = pickCycled(nameLines, index);
      return {
        profile_id: id,
        profile_name: p?.name || "",
        channel_name,
        channel_description: pickCycled(descLines, index),
        skip_name_change: !enableNames || !channel_name,
        video_default_title: pickCycled(videoLines, index),
        avatar_path: pickCycled(avatarLines, index),
      };
    });

    const profiles_custom_data: Record<string, Record<string, unknown>> = {};
    for (const p of profiles) {
      if (
        ids.includes(p.id) &&
        p.custom_data &&
        Object.keys(p.custom_data).length
      ) {
        profiles_custom_data[p.id] = p.custom_data;
      }
    }

    return {
      profile_ids: ids,
      description: descLines[0] || "",
      description_lines: descLines,
      link_title: channel_links[0]?.[0] || "",
      link_url: channel_links[0]?.[1] || "",
      channel_links,
      assignments,
      change_language: changeLanguage,
      headless: false,
      ...(Object.keys(profiles_custom_data).length
        ? { profiles_custom_data }
        : {}),
    };
  };

  const applyToProfiles = async () => {
    const ids = [...pickerSelected];
    if (!ids.length) {
      setError("Выберите профили.");
      return;
    }
    const msg = isIg
      ? `Применить настройки профиля в Instagram для ${ids.length} профилей?`
      : `Применить настройки канала в YouTube Studio для ${ids.length} профилей?`;
    if (!confirm(msg)) return;

    setPickerOpen(false);
    setError("");
    setStatus(`Редактирование: 0 / ${ids.length}…`);
    try {
      const res = await api.startProfileJob(
        "channel-setup",
        buildPayload(ids),
      );
      setJobId(res.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStatus("");
    }
  };

  const openAi = async (
    target: "names" | "desc" | "linkTitle" | "video",
    defaultPromptId: string,
  ) => {
    setAiTarget(target);
    setAiLines(Math.max(1, pickerSelected.size || 1));
    try {
      const r = await api.getAiPrompts();
      setPrompts(r.prompts);
      const found = r.prompts.find((p) => p.id === defaultPromptId);
      setAiPromptId(found?.id || r.prompts[0]?.id || "");
      setAiOpen(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const runAi = async () => {
    if (!aiTarget || !aiPromptId) return;
    setAiBusy(true);
    try {
      const r = await api.aiGenerate({
        prompt_id: aiPromptId,
        reply_lines: aiLines,
      });
      const text = r.text || "";
      if (aiTarget === "names") setNamesText(text);
      if (aiTarget === "desc") setDescText(text);
      if (aiTarget === "linkTitle") setLinkTitles(text);
      if (aiTarget === "video") setVideoTitles(text);
      setAiOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAiBusy(false);
    }
  };

  const triggerImport = (target: "names" | "desc" | "video") => {
    importTarget.current = target;
    importRef.current?.click();
  };

  const onImportFile = async (file: File) => {
    const target = importTarget.current;
    if (!target) return;
    const text = await file.text();
    if (target === "names") setNamesText(text);
    if (target === "desc") setDescText(text);
    if (target === "video") setVideoTitles(text);
  };

  const togglePicker = (id: string) => {
    setPickerSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="stack">
      <div className="page-header">
        <h1 className="title">Редактирование канала</h1>
        <button
          type="button"
          className="btn"
          onClick={openPicker}
          disabled={running}
        >
          Выбрать профили
        </button>
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

      <input
        ref={importRef}
        type="file"
        accept=".txt,.csv,text/plain"
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void onImportFile(f);
          e.target.value = "";
        }}
      />

      <label className="check" title={
        isIg
          ? "Перед редактированием: Language preferences → «Русский»."
          : "Перед настройкой канала: язык интерфейса YouTube «Русский»."
      }>
        <input
          type="checkbox"
          checked={changeLanguage}
          onChange={(e) => setChangeLanguage(e.target.checked)}
          disabled={running}
        />
        Поменять язык
      </label>

      <section className="group stack">
        <SectionHead
          label="Фото профиля"
          checked={enableAvatar}
          onChange={setEnableAvatar}
          actions={
            <label className="check">
              <input
                type="checkbox"
                checked={noCrop}
                onChange={(e) => setNoCrop(e.target.checked)}
                disabled={!enableAvatar || running}
              />
              Не обрезать
            </label>
          }
        />
        {enableAvatar ? (
          <>
            <label className="hint">
              Пути к файлам на сервере (по одному на строку; список зацикливается
              по профилям)
            </label>
            <textarea
              className="field"
              rows={3}
              disabled={running}
              value={avatarPaths}
              onChange={(e) => setAvatarPaths(e.target.value)}
              placeholder="C:\avatars\1.png"
            />
            {noCrop ? (
              <p className="hint">1 файл = 1 профиль, без вырезки иконок.</p>
            ) : (
              <p className="hint">
                Обрезка как на десктопе недоступна в вебе — указывайте уже готовые
                файлы.
              </p>
            )}
          </>
        ) : null}
      </section>

      <section className="group stack">
        <SectionHead
          label={namesTitle}
          checked={enableNames}
          onChange={setEnableNames}
          actions={
            <>
              <button
                type="button"
                className="btn secondary"
                disabled={!enableNames || running}
                onClick={() => triggerImport("names")}
              >
                Импорт…
              </button>
              <TitleVariablesHint
                onInsert={(t) =>
                  setNamesText((v) => insertAtCursor(v, t, namesRef.current))
                }
              />
              <button
                type="button"
                className="btn secondary"
                disabled={!enableNames || running}
                title={`Сгенерировать через ИИ — «${namesTitle}»`}
                onClick={() => openAi("names", "builtin_channel_name")}
              >
                ✦
              </button>
            </>
          }
        />
        {enableNames ? (
          <textarea
            ref={namesRef}
            className="field"
            rows={4}
            disabled={running}
            value={namesText}
            onChange={(e) => setNamesText(e.target.value)}
            placeholder={
              isIg
                ? "Юзернейм — по одному на строку…"
                : "Название канала — по одному на строку…"
            }
          />
        ) : null}
      </section>

      {!isIg ? (
        <section className="group stack">
          <SectionHead
            label="Название видео"
            checked={enableVideoTitle}
            onChange={setEnableVideoTitle}
            actions={
              <>
                <button
                  type="button"
                  className="btn secondary"
                  disabled={!enableVideoTitle || running}
                  onClick={() => triggerImport("video")}
                >
                  Импорт…
                </button>
                <TitleVariablesHint
                  onInsert={(t) =>
                    setVideoTitles((v) =>
                      insertAtCursor(v, t, videoRef.current),
                    )
                  }
                />
                <button
                  type="button"
                  className="btn secondary"
                  disabled={!enableVideoTitle || running}
                  title="Сгенерировать через ИИ — «Название видео»"
                  onClick={() => openAi("video", "builtin_video_title")}
                >
                  ✦
                </button>
              </>
            }
          />
          {enableVideoTitle ? (
            <textarea
              ref={videoRef}
              className="field"
              rows={4}
              disabled={running}
              value={videoTitles}
              onChange={(e) => setVideoTitles(e.target.value)}
              placeholder="Название видео — по одному на строку. Переменные: {date}, {profile}, {video}, {index}…"
            />
          ) : null}
        </section>
      ) : null}

      <section className="group stack">
        <SectionHead
          label="Описание канала"
          checked={enableDesc}
          onChange={setEnableDesc}
          actions={
            <>
              <button
                type="button"
                className="btn secondary"
                disabled={!enableDesc || running}
                onClick={() => triggerImport("desc")}
              >
                Импорт…
              </button>
              <TitleVariablesHint
                onInsert={(t) =>
                  setDescText((v) => insertAtCursor(v, t, descRef.current))
                }
              />
              <button
                type="button"
                className="btn secondary"
                disabled={!enableDesc || running}
                title="Сгенерировать через ИИ — «Описание канала»"
                onClick={() => openAi("desc", "builtin_channel_description")}
              >
                ✦
              </button>
            </>
          }
        />
        {enableDesc ? (
          <textarea
            ref={descRef}
            className="field"
            rows={4}
            disabled={running}
            value={descText}
            onChange={(e) => setDescText(e.target.value)}
            placeholder="Описание — по одному на строку (строка = профиль)…"
          />
        ) : null}
      </section>

      {!isIg ? (
        <section className="group stack">
          <SectionHead
            label="Ссылка"
            checked={enableLink}
            onChange={setEnableLink}
            actions={
              <button
                type="button"
                className="btn secondary"
                disabled={!enableLink || running}
                title="Сгенерировать названия ссылок через ИИ"
                onClick={() => openAi("linkTitle", "builtin_link_title")}
              >
                ✦
              </button>
            }
          />
          {enableLink ? (
            <div className="grid-2">
              <div className="stack">
                <label className="hint">Название ссылки</label>
                <textarea
                  ref={linkTitleRef}
                  className="field"
                  rows={4}
                  disabled={running}
                  value={linkTitles}
                  onChange={(e) => setLinkTitles(e.target.value)}
                  placeholder="Название ссылки — по одному на строку (строка = профиль)…"
                />
              </div>
              <div className="stack">
                <label className="hint">URL</label>
                <textarea
                  className="field"
                  rows={4}
                  disabled={running}
                  value={linkUrls}
                  onChange={(e) => setLinkUrls(e.target.value)}
                  placeholder="https://… — по одному URL на строку (строка = профиль)"
                />
              </div>
            </div>
          ) : null}
        </section>
      ) : null}

      {job?.logs?.length ? (
        <JobLogBox lines={job.logs.slice(-40)} emptyHint="" />
      ) : null}

      {pickerOpen ? (
        <div className="modal-backdrop" onClick={() => setPickerOpen(false)}>
          <div
            className="modal-card stack"
            onClick={(e) => e.stopPropagation()}
            style={{ width: "min(640px, 100%)" }}
          >
            <div className="page-header">
              <h3 className="group-title">Выбрать профили</h3>
              <button
                type="button"
                className="btn secondary"
                onClick={() => setPickerOpen(false)}
              >
                Отмена
              </button>
            </div>
            <div className="row">
              <input
                className="field"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Поиск…"
              />
              <button
                type="button"
                className="btn secondary"
                onClick={() =>
                  setPickerSelected(new Set(filtered.map((p) => p.id)))
                }
              >
                Все видимые
              </button>
              <button
                type="button"
                className="btn secondary"
                onClick={() => setPickerSelected(new Set())}
              >
                Снять
              </button>
            </div>
            <p className="hint">
              Выбрано профилей для редактирования: {pickerSelected.size}
            </p>
            <div
              className="list-panel"
              style={{ maxHeight: 320, overflow: "auto" }}
            >
              {filtered.map((p) => (
                <label
                  key={p.id}
                  className={`list-item ${pickerSelected.has(p.id) ? "active" : ""}`}
                  style={{ display: "flex", gap: 10, cursor: "pointer" }}
                >
                  <input
                    type="checkbox"
                    checked={pickerSelected.has(p.id)}
                    onChange={() => togglePicker(p.id)}
                  />
                  <span>
                    <div style={{ fontWeight: 700, color: "var(--title)" }}>
                      {p.name || p.id}
                    </div>
                    <div className="hint">{p.id}</div>
                  </span>
                </label>
              ))}
            </div>
            <button type="button" className="btn" onClick={applyToProfiles}>
              {isIg ? "Применить" : "Применить в Studio"}
            </button>
          </div>
        </div>
      ) : null}

      {aiOpen ? (
        <div className="modal-backdrop" onClick={() => setAiOpen(false)}>
          <div
            className="modal-card stack"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="page-header">
              <h3 className="group-title">Генерация ИИ</h3>
              <button
                type="button"
                className="btn secondary"
                onClick={() => setAiOpen(false)}
              >
                Отмена
              </button>
            </div>
            <label className="hint">Промпт</label>
            <select
              className="field"
              value={aiPromptId}
              onChange={(e) => setAiPromptId(e.target.value)}
            >
              {prompts.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.title || p.id}
                </option>
              ))}
            </select>
            <label className="hint">Количество строк</label>
            <input
              className="field"
              style={{ maxWidth: 120 }}
              type="number"
              min={1}
              max={500}
              value={aiLines}
              onChange={(e) => setAiLines(Number(e.target.value) || 1)}
            />
            <button
              type="button"
              className="btn"
              disabled={aiBusy}
              onClick={runAi}
            >
              {aiBusy ? "Генерация…" : "Сгенерировать"}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
