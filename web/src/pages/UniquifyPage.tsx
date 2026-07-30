import { useState } from "react";
import { api } from "../api/client";
import { useJobPoll } from "../hooks/useJobPoll";
import { ProgressBar } from "../components/ProgressBar";

function linesToList(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function UniquifyPage() {
  const [inputFiles, setInputFiles] = useState("");
  const [outputDir, setOutputDir] = useState("");
  const [copies, setCopies] = useState(1);
  const [workers, setWorkers] = useState(2);
  const [useGpu, setUseGpu] = useState(false);
  const [randomize, setRandomize] = useState(true);
  const [deleteAfter, setDeleteAfter] = useState(false);
  const [musicFiles, setMusicFiles] = useState("");
  const [musicEnabled, setMusicEnabled] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const { job } = useJobPoll(jobId);

  const running =
    busy || (job != null && ["queued", "running"].includes(job.status));

  const onStart = async () => {
    setError("");
    setBusy(true);
    try {
      const files = linesToList(inputFiles);
      if (!outputDir.trim()) throw new Error("Укажите выходную папку.");
      if (!files.length) throw new Error("Укажите хотя бы один входной файл.");
      const res = await api.startUniquify({
        output_dir: outputDir.trim(),
        input_files: files,
        copies_per_file: copies,
        num_workers: workers,
        use_gpu: useGpu,
        use_gpu_finalize: useGpu,
        randomize_uniquify: randomize,
        background_music_enabled: musicEnabled,
        background_music_files: linesToList(musicFiles),
      });
      setJobId(res.id);
      if (deleteAfter) {
        await api.patchSettings({ delete_after_upload: true });
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onCancel = async () => {
    if (!jobId) return;
    try {
      await api.cancelJob(jobId);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const pctLabel =
    job && job.progress.total > 0
      ? `${job.progress.current}/${job.progress.total}`
      : job?.progress.message || "";

  return (
    <div className="stack">
      <div className="page-header">
        <h1 className="title">Zaliver</h1>
        <ProgressBar
          value={job?.progress.current ?? 0}
          max={Math.max(1, job?.progress.total ?? 1)}
          label={pctLabel}
        />
        <button type="button" className="btn" disabled={running} onClick={onStart}>
          Старт
        </button>
        <button
          type="button"
          className="btn danger"
          disabled={!jobId || !running}
          onClick={onCancel}
        >
          Отмена
        </button>
      </div>
      <p className="hint">
        Выбор видео → папка результатов · случайная уникализация
      </p>
      {error ? <div className="error-banner">{error}</div> : null}

      <div className="grid-2">
        <div className="stack">
          <section className="group">
            <h3 className="group-title">Файлы и папка результата</h3>
            <div className="stack">
              <label className="hint">Исходные видео (по одному пути на строку)</label>
              <textarea
                className="field"
                value={inputFiles}
                onChange={(e) => setInputFiles(e.target.value)}
                placeholder={"C:\\Videos\\a.mp4\nC:\\Videos\\b.mp4"}
              />
              <label className="hint">Выходная папка</label>
              <input
                className="field"
                value={outputDir}
                onChange={(e) => setOutputDir(e.target.value)}
                placeholder="Папка для уникализированных файлов…"
              />
              <div className="row">
                <label>
                  Копий на исходник{" "}
                  <input
                    className="field"
                    style={{ width: 90 }}
                    type="number"
                    min={1}
                    value={copies}
                    onChange={(e) => setCopies(Number(e.target.value) || 1)}
                  />
                </label>
                <label>
                  Потоки{" "}
                  <input
                    className="field"
                    style={{ width: 90 }}
                    type="number"
                    min={1}
                    max={32}
                    value={workers}
                    onChange={(e) => setWorkers(Number(e.target.value) || 1)}
                  />
                </label>
              </div>
              <label className="check">
                <input
                  type="checkbox"
                  checked={randomize}
                  onChange={(e) => setRandomize(e.target.checked)}
                />
                Случайная уникализация
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={useGpu}
                  onChange={(e) => setUseGpu(e.target.checked)}
                />
                GPU
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={deleteAfter}
                  onChange={(e) => setDeleteAfter(e.target.checked)}
                />
                Удалять после залива
              </label>
            </div>
          </section>

          <section className="group">
            <h3 className="group-title">Фоновые треки</h3>
            <label className="check">
              <input
                type="checkbox"
                checked={musicEnabled}
                onChange={(e) => setMusicEnabled(e.target.checked)}
              />
              Добавить музыку
            </label>
            {musicEnabled ? (
              <textarea
                className="field"
                value={musicFiles}
                onChange={(e) => setMusicFiles(e.target.value)}
                placeholder="Пути к аудиофайлам…"
              />
            ) : null}
          </section>
        </div>

        <section className="group">
          <h3 className="group-title">Лог</h3>
          <div className="log-box">
            {job?.logs?.length
              ? job.logs.join("\n")
              : "Лог появится после старта задачи…"}
          </div>
          {job ? (
            <p className="hint" style={{ marginTop: 8 }}>
              Задача {job.id.slice(0, 8)}… · {job.status}
              {job.message ? ` · ${job.message}` : ""}
            </p>
          ) : null}
        </section>
      </div>
    </div>
  );
}
