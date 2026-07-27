import { useState } from "react";
import { api } from "../api/client";
import { useJobPoll } from "../hooks/useJobPoll";

export function ChannelEditPage() {
  const [profileIds, setProfileIds] = useState("");
  const [description, setDescription] = useState("");
  const [channelName, setChannelName] = useState("");
  const [linkTitle, setLinkTitle] = useState("");
  const [linkUrl, setLinkUrl] = useState("");
  const [error, setError] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const { job } = useJobPoll(jobId);

  const start = async () => {
    setError("");
    const ids = profileIds
      .split(/[,\n]/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (!ids.length) {
      setError("Укажите ID профилей.");
      return;
    }
    try {
      const assignments = ids.map((id) => ({
        profile_id: id,
        channel_name: channelName,
        channel_description: description,
      }));
      const res = await api.startProfileJob("channel-setup", {
        profile_ids: ids,
        description,
        link_title: linkTitle,
        link_url: linkUrl,
        assignments,
        headless: false,
      });
      setJobId(res.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="stack">
      <div className="page-header">
        <h1 className="title">Редактирование каналов</h1>
        <button type="button" className="btn" onClick={start}>
          Запустить
        </button>
      </div>
      {error ? <div className="error-banner">{error}</div> : null}
      <section className="group stack">
        <label className="hint">ID профилей (через запятую или с новой строки)</label>
        <textarea
          className="field"
          value={profileIds}
          onChange={(e) => setProfileIds(e.target.value)}
        />
        <label className="hint">Название / username</label>
        <input
          className="field"
          value={channelName}
          onChange={(e) => setChannelName(e.target.value)}
        />
        <label className="hint">Описание</label>
        <textarea
          className="field"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <label className="hint">Ссылка — заголовок</label>
        <input
          className="field"
          value={linkTitle}
          onChange={(e) => setLinkTitle(e.target.value)}
        />
        <label className="hint">Ссылка — URL</label>
        <input
          className="field"
          value={linkUrl}
          onChange={(e) => setLinkUrl(e.target.value)}
        />
      </section>
      {job ? (
        <div className="log-box">
          {job.status}: {job.message}
          {"\n"}
          {(job.logs || []).slice(-30).join("\n")}
        </div>
      ) : null}
    </div>
  );
}
