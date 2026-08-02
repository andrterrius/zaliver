import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Platform } from "../api/client";
import { useJobPoll } from "../hooks/useJobPoll";
import { usePersistedJobId } from "../hooks/usePersistedJobId";
import { useProcessingDefaults } from "../hooks/useProcessingDefaults";
import {
  asBool,
  asNumber,
  asRange,
  asStringList,
  textOverlayFromSettings,
  textOverlayToSettings,
  useDebouncedSettingsPatch,
} from "../hooks/useServerSettingsPersist";
import {
  savePendingUpload,
  useUploadAfterJob,
  workersForUploadChoice,
} from "../hooks/useUploadAfterJob";
import { ProgressBar } from "../components/ProgressBar";
import { SectionNav } from "../components/SectionNav";
import { SourcePicker } from "../components/SourcePicker";
import { RangeSlider, type RangeValue } from "../components/RangeSlider";
import {
  UploadAfterDialog,
  type UploadAfterChoice,
} from "../components/UploadAfterDialog";
import { JobLogBox, mergeJobLogLines } from "../components/JobLogBox";
import { OutputFolderPicker } from "../components/OutputFolderPicker";
import {
  TextOverlayFields,
  defaultSliceTextOverlay,
  textOverlayToApi,
  type TextOverlayState,
} from "../components/TextOverlayFields";

const SECTIONS = ["Исходники", "Сцены", "Текст", "Музыка"];

type Props = { platform: Platform };

export function SlicingPage({ platform }: Props) {
  const proc = useProcessingDefaults();
  const [section, setSection] = useState(0);
  const [hydrated, setHydrated] = useState(false);
  const [clips, setClips] = useState<string[]>([]);
  const [outputDir, setOutputDir] = useState("");
  const [music, setMusic] = useState<string[]>([]);
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
  const [jobId, setJobId] = usePersistedJobId("slicing");
  const [uploadJobId, setUploadJobId] = usePersistedJobId("upload-after-slicing");
  const [uploadDialogOpen, setUploadDialogOpen] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const { job } = useJobPoll(jobId);
  const { job: uploadJob } = useJobPoll(uploadJobId);
  const onUploadErr = useCallback((msg: string) => setError(msg), []);
  useUploadAfterJob("slicing", job, setUploadJobId, onUploadErr, platform);
  const running =
    busy ||
    (job != null && ["queued", "running"].includes(job.status)) ||
    (uploadJob != null && ["queued", "running"].includes(uploadJob.status));

  useEffect(() => {
    if (job && ["failed", "cancelled"].includes(job.status)) {
      savePendingUpload("slicing", null);
    }
  }, [job]);

  useEffect(() => {
    void (async () => {
      try {
        const s = await api.getSettings();
        const v = s.values;
        setClips(asStringList(v["slice/clip_files"]));
        setOutputDir(String(v["slice/output_folder"] || "").trim());
        setMusic(asStringList(v["slice/music_files"]));
        setCopies(
          Math.max(1, Math.round(asNumber(v["slice/copies_per_track"], 1))),
        );
        setAutoDurations(asBool(v["slice/auto_scene_durations"], false));
        setSceneDuration(
          asRange(v["slice/min_scene_duration"], v["slice/max_scene_duration"], {
            lo: 1.0,
            hi: 1.3,
          }),
        );
        setScenesCount(
          asRange(v["slice/min_scenes"], v["slice/max_scenes"], {
            lo: 12,
            hi: 23,
          }),
        );
        setTextOverlay(
          textOverlayFromSettings(v, "slice/", defaultSliceTextOverlay()),
        );
      } catch {
        /* keep defaults */
      } finally {
        setHydrated(true);
      }
    })();
  }, []);

  const persistValues = useMemo(() => {
    if (!hydrated) return null;
    return {
      "slice/clip_files": clips,
      "slice/output_folder": outputDir,
      "slice/music_files": music,
      "slice/copies_per_track": copies,
      "slice/auto_scene_durations": autoDurations,
      "slice/min_scene_duration": sceneDuration.lo,
      "slice/max_scene_duration": sceneDuration.hi,
      "slice/min_scenes": Math.round(scenesCount.lo),
      "slice/max_scenes": Math.round(scenesCount.hi),
      ...textOverlayToSettings(textOverlay, "slice/"),
    };
  }, [
    hydrated,
    clips,
    outputDir,
    music,
    copies,
    autoDurations,
    sceneDuration,
    scenesCount,
    textOverlay,
  ]);

  useDebouncedSettingsPatch(persistValues);

  const onStart = () => {
    setError("");
    if (!clips.length) {
      setError("Добавьте клипы.");
      return;
    }
    if (!music.length) {
      setError("Добавьте аудиотреки.");
      return;
    }
    if (scenesCount.lo > scenesCount.hi) {
      setError("Мин. количество сцен не может быть больше максимального.");
      return;
    }
    if (!autoDurations && sceneDuration.lo > sceneDuration.hi) {
      setError("Мин. длительность сцены не может быть больше максимальной.");
      return;
    }
    setUploadDialogOpen(true);
  };

  const onUploadDialogConfirm = async (choice: UploadAfterChoice) => {
    setUploadDialogOpen(false);
    setBusy(true);
    setError("");
    try {
      if (persistValues) {
        await api.patchSettings(persistValues);
      }
      const willUpload = choice.profileIds.length > 0;
      try {
        await api.setPlatform(platform);
      } catch {
        /* continue */
      }
      const plannedVideos = music.length * copies;
      const res = await api.startSlicing({
        output_dir: outputDir,
        platform,
        clip_files: clips,
        music_files: music,
        copies_per_track: copies,
        num_workers: workersForUploadChoice(choice, proc.numWorkers),
        use_gpu: proc.useGpu,
        use_gpu_finalize: proc.useGpuFinalize,
        use_suggested_durations: autoDurations,
        min_scene_duration: sceneDuration.lo,
        max_scene_duration: sceneDuration.hi,
        min_scenes: Math.round(scenesCount.lo),
        max_scenes: Math.round(scenesCount.hi),
        slice_fps_mode: proc.sliceFpsMode,
        text_overlay: textOverlayToApi(textOverlay),
        youtube_upload_after_processing: willUpload,
      });
      savePendingUpload(
        "slicing",
        willUpload
          ? { ...choice, processingJobId: res.id, platform, plannedVideos }
          : null,
      );
      setJobId(res.id);
      setUploadJobId(null);
    } catch (e) {
      savePendingUpload("slicing", null);
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
      <UploadAfterDialog
        open={uploadDialogOpen}
        mode="slicing"
        platform={platform}
        onCancel={() => setUploadDialogOpen(false)}
        onConfirm={(c) => void onUploadDialogConfirm(c)}
      />
      <div className="grid-2">
        <div className="stack">
          {section === 0 ? (
            <section className="group stack">
              <h3 className="group-title">Клипы</h3>
              <SourcePicker
                label="Видеоклипы"
                value={clips}
                onChange={setClips}
                kind="video"
                accept="video/*"
              />
              <OutputFolderPicker
                kind="slicing"
                platform={platform}
                value={outputDir}
                onChange={setOutputDir}
                disabled={running}
              />
              <label>
                Копий на трек{" "}
                <input
                  className="field"
                  style={{ width: 90 }}
                  type="number"
                  min={1}
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
              <SourcePicker
                label="Аудиотреки"
                value={music}
                onChange={setMusic}
                kind="audio"
                accept="audio/*"
              />
            </section>
          ) : null}
        </div>
        <JobLogBox
          lines={mergeJobLogLines(job?.logs, uploadJob?.logs)}
          emptyHint="Лог нарезки…"
        >
          {uploadJob ? (
            <p className="hint" style={{ marginTop: 8 }}>
              Залив {uploadJob.status}
              {uploadJob.progress.total
                ? ` · ${uploadJob.progress.current}/${uploadJob.progress.total}`
                : ""}
            </p>
          ) : null}
        </JobLogBox>
      </div>
    </div>
  );
}
