import { useState } from "react";
import { api } from "../api/client";
import { useJobPoll } from "../hooks/useJobPoll";
import { useProcessingDefaults } from "../hooks/useProcessingDefaults";
import { ProgressBar } from "../components/ProgressBar";
import { SectionNav } from "../components/SectionNav";
import { RangeSlider, type RangeValue } from "../components/RangeSlider";
import {
  TextOverlayFields,
  defaultSliceTextOverlay,
  textOverlayToApi,
  type TextOverlayState,
} from "../components/TextOverlayFields";
import { linesToList } from "../lib/paths";

const SECTIONS = ["Исходники", "Сцены", "Текст", "Музыка"];

export function SlicingPage() {
  const proc = useProcessingDefaults();
  const [section, setSection] = useState(0);
  const [clips, setClips] = useState("");
  const [music, setMusic] = useState("");
  const [outputDir, setOutputDir] = useState("");
  const [copies, setCopies] = useState(1);
  const [autoDurations, setAutoDurations] = useState(false);
  const [sceneDuration, setSceneDuration] = useState<RangeValue>({
    lo: 1.0,
    hi: 1.3,
  });
  const [scenesCount, setScenesCount] = useState<RangeValue>({ lo: 12, hi: 23 });
  const [textOverlay, setTextOverlay] = useState<TextOverlayState>(
    defaultSliceTextOverlay,
  );
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const { job } = useJobPoll(jobId);
  const running =
    busy || (job != null && ["queued", "running"].includes(job.status));

  const onStart = async () => {
    setError("");
    setBusy(true);
    try {
      const clip_files = linesToList(clips);
      const music_files = linesToList(music);
      if (!outputDir.trim()) throw new Error("Укажите выходную папку.");
      if (!clip_files.length) throw new Error("Добавьте клипы.");
      if (!music_files.length) throw new Error("Добавьте аудиотреки.");
      if (scenesCount.lo > scenesCount.hi) {
        throw new Error("Мин. количество сцен не может быть больше максимального.");
      }
      if (!autoDurations && sceneDuration.lo > sceneDuration.hi) {
        throw new Error(
          "Мин. длительность сцены не может быть больше максимальной.",
        );
      }
      const res = await api.startSlicing({
        output_dir: outputDir.trim(),
        clip_files,
        music_files,
        copies_per_track: copies,
        num_workers: proc.numWorkers,
        use_gpu: proc.useGpu,
        use_gpu_finalize: proc.useGpuFinalize,
        use_suggested_durations: autoDurations,
        min_scene_duration: sceneDuration.lo,
        max_scene_duration: sceneDuration.hi,
        min_scenes: Math.round(scenesCount.lo),
        max_scenes: Math.round(scenesCount.hi),
        slice_fps_mode: proc.sliceFpsMode,
        text_overlay: textOverlayToApi(textOverlay),
      });
      setJobId(res.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <div className="page-header">
        <h1 className="title">Нарезка</h1>
        <ProgressBar
          value={job?.progress.current ?? 0}
          max={Math.max(1, job?.progress.total ?? 1)}
          label={
            job && job.progress.total
              ? `${job.progress.current}/${job.progress.total}`
              : ""
          }
        />
        <button type="button" className="btn" disabled={running} onClick={onStart}>
          Старт
        </button>
        <button
          type="button"
          className="btn danger"
          disabled={!jobId || !running}
          onClick={() => jobId && api.cancelJob(jobId)}
        >
          Отмена
        </button>
      </div>
      <SectionNav sections={SECTIONS} active={section} onChange={setSection} />
      {error ? <div className="error-banner">{error}</div> : null}
      <div className="grid-2">
        <div className="stack">
          {section === 0 ? (
            <section className="group stack">
              <h3 className="group-title">Клипы и папка</h3>
              <label className="hint">Видеоклипы (пути)</label>
              <textarea
                className="field"
                value={clips}
                onChange={(e) => setClips(e.target.value)}
              />
              <label className="hint">Выходная папка</label>
              <input
                className="field"
                value={outputDir}
                onChange={(e) => setOutputDir(e.target.value)}
              />
              <label>
                Копий на трек{" "}
                <input
                  className="field"
                  style={{ width: 90 }}
                  type="number"
                  min={1}
                  max={100}
                  value={copies}
                  onChange={(e) => setCopies(Number(e.target.value) || 1)}
                />
              </label>
              <p className="hint">GPU / потоки / fps — в Настройках.</p>
            </section>
          ) : null}

          {section === 1 ? (
            <section className="group stack">
              <h3 className="group-title">Сцены</h3>
              <label className="check">
                <input
                  type="checkbox"
                  checked={autoDurations}
                  onChange={(e) => setAutoDurations(e.target.checked)}
                />
                Автоматически подобрать оптимальную длительность
              </label>
              <div className="form-grid">
                <label className="hint">Длительность</label>
                <RangeSlider
                  min={0.1}
                  max={60}
                  step={0.05}
                  decimals={2}
                  suffix=" с"
                  value={sceneDuration}
                  disabled={autoDurations}
                  onChange={setSceneDuration}
                />
              </div>
              <p className="hint">
                Интервал между сменами кадра на пиках аудио. Разведите точки —
                случайный диапазон.
              </p>
              <div className="form-grid">
                <label className="hint">Сцены</label>
                <RangeSlider
                  min={1}
                  max={999}
                  step={1}
                  decimals={0}
                  value={scenesCount}
                  onChange={setScenesCount}
                />
              </div>
              <p className="hint">
                Число сцен выбирается случайно в заданном диапазоне.
              </p>
            </section>
          ) : null}

          {section === 2 ? (
            <TextOverlayFields value={textOverlay} onChange={setTextOverlay} />
          ) : null}

          {section === 3 ? (
            <section className="group stack">
              <h3 className="group-title">Музыка</h3>
              <label className="hint">Аудиотреки (пути)</label>
              <textarea
                className="field"
                value={music}
                onChange={(e) => setMusic(e.target.value)}
              />
            </section>
          ) : null}
        </div>
        <section className="group">
          <h3 className="group-title">Лог</h3>
          <div className="log-box">
            {job?.logs?.length ? job.logs.join("\n") : "Лог нарезки…"}
          </div>
        </section>
      </div>
    </div>
  );
}
