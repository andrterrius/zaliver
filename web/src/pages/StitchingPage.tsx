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
import {
  UploadAfterDialog,
  type UploadAfterChoice,
} from "../components/UploadAfterDialog";
import { JobLogBox, mergeJobLogLines } from "../components/JobLogBox";
import { OutputFolderPicker } from "../components/OutputFolderPicker";
import {
  TextOverlayFields,
  defaultStitchTextOverlay,
  textOverlayToApi,
  type TextOverlayState,
} from "../components/TextOverlayFields";
import { RangeSlider, type RangeValue } from "../components/RangeSlider";

const SECTIONS = ["Исходники", "Текст", "Музыка", "Переходы"];

const TRANSITIONS = [
  {
    id: "cut",
    label: "Простая склейка",
    hint: "Жёсткий стык двух клипов без эффекта.",
  },
  {
    id: "fade",
    label: "Растворение",
    hint: "Плавное растворение одного кадра в другой (~0.4с).",
  },
  {
    id: "circleopen",
    label: "Круговое раскрытие",
    hint: "Вторая часть открывается кругом из центра (~0.4с).",
  },
  {
    id: "zoomin",
    label: "Зум-удар",
    hint: "Punch-zoom как в эдитах: наезд в стык (~0.4с).",
  },
  {
    id: "fadewhite",
    label: "Вспышка",
    hint: "Белая вспышка на бите — классика эдитов (~0.4с).",
  },
  {
    id: "hblur",
    label: "Whip-смаз",
    hint: "Горизонтальный смаз, как whip-pan между кадрами (~0.4с).",
  },
] as const;

type TransitionId = (typeof TRANSITIONS)[number]["id"];

type Props = { platform: Platform };

export function StitchingPage({ platform }: Props) {
  const proc = useProcessingDefaults();
  const [section, setSection] = useState(0);
  const [hydrated, setHydrated] = useState(false);
  const [part1, setPart1] = useState<string[]>([]);
  const [part2, setPart2] = useState<string[]>([]);
  const [outputDir, setOutputDir] = useState("");
  const [music, setMusic] = useState<string[]>([]);
  const [copies, setCopies] = useState(1);
  const [transition, setTransition] = useState<TransitionId | "">("cut");
  const [lastTransition, setLastTransition] = useState<TransitionId>("cut");
  const [transitionRandom, setTransitionRandom] = useState(false);
  const [partDuration, setPartDuration] = useState<RangeValue>({
    lo: 2,
    hi: 6,
  });
  const [textOverlay, setTextOverlay] = useState<TextOverlayState>(
    defaultStitchTextOverlay,
  );
  const [jobId, setJobId] = usePersistedJobId("stitching");
  const [uploadJobId, setUploadJobId] = usePersistedJobId("upload-after-stitching");
  const [uploadDialogOpen, setUploadDialogOpen] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const { job } = useJobPoll(jobId);
  const { job: uploadJob } = useJobPoll(uploadJobId);
  const onUploadErr = useCallback((msg: string) => setError(msg), []);
  useUploadAfterJob("stitching", job, setUploadJobId, onUploadErr, platform);
  const running =
    busy ||
    (job != null && ["queued", "running"].includes(job.status)) ||
    (uploadJob != null && ["queued", "running"].includes(uploadJob.status));

  useEffect(() => {
    if (job && ["failed", "cancelled"].includes(job.status)) {
      savePendingUpload("stitching", null);
    }
  }, [job]);

  useEffect(() => {
    void (async () => {
      try {
        const s = await api.getSettings();
        const v = s.values;
        setPart1(asStringList(v["stitch/part1_files"]));
        setPart2(asStringList(v["stitch/part2_files"]));
        setOutputDir(String(v["stitch/output_folder"] || "").trim());
        setMusic(asStringList(v["stitch/music_files"]));
        setCopies(
          Math.max(1, Math.round(asNumber(v["stitch/copies_per_track"], 1))),
        );
        const random = asBool(v["stitch/transition_random"], false);
        setTransitionRandom(random);
        const saved = String(v["stitch/transition"] ?? "cut") as TransitionId;
        const known = TRANSITIONS.some((t) => t.id === saved) ? saved : "cut";
        setLastTransition(known);
        setTransition(random ? "" : known);
        setPartDuration(
          asRange(v["stitch/min_part_duration"], v["stitch/max_part_duration"], {
            lo: 2,
            hi: 6,
          }),
        );
        setTextOverlay(
          textOverlayFromSettings(v, "stitch/", defaultStitchTextOverlay()),
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
      "stitch/part1_files": part1,
      "stitch/part2_files": part2,
      "stitch/output_folder": outputDir,
      "stitch/music_files": music,
      "stitch/copies_per_track": copies,
      "stitch/transition": transition || lastTransition || "cut",
      "stitch/transition_random": transitionRandom,
      "stitch/min_part_duration": partDuration.lo,
      "stitch/max_part_duration": partDuration.hi,
      ...textOverlayToSettings(textOverlay, "stitch/"),
    };
  }, [
    hydrated,
    part1,
    part2,
    outputDir,
    music,
    copies,
    transition,
    lastTransition,
    transitionRandom,
    partDuration,
    textOverlay,
  ]);

  useDebouncedSettingsPatch(persistValues);

  const onStart = () => {
    setError("");
    if (!part1.length) {
      setError("Добавьте клипы для части 1.");
      return;
    }
    if (!part2.length) {
      setError("Добавьте клипы для части 2.");
      return;
    }
    if (!music.length) {
      setError("Добавьте аудиотреки.");
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
      const res = await api.startStitching({
        output_dir: outputDir,
        platform,
        part1_files: part1,
        part2_files: part2,
        music_files: music,
        copies_per_track: copies,
        num_workers: workersForUploadChoice(choice, proc.numWorkers),
        use_gpu: proc.useGpu,
        use_gpu_finalize: proc.useGpuFinalize,
        transition: transition || lastTransition,
        transition_duration: 0.4,
        transition_random: transitionRandom,
        min_part_duration: partDuration.lo,
        max_part_duration: partDuration.hi,
        text_overlay: textOverlayToApi(textOverlay),
        youtube_upload_after_processing: willUpload,
      });
      savePendingUpload(
        "stitching",
        willUpload
          ? { ...choice, processingJobId: res.id, platform, plannedVideos }
          : null,
      );
      setJobId(res.id);
      setUploadJobId(null);
    } catch (e) {
      savePendingUpload("stitching", null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack">
      <div className="page-header">
        <h1 className="title">Склейка</h1>
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
        mode="stitching"
        platform={platform}
        onCancel={() => setUploadDialogOpen(false)}
        onConfirm={(c) => void onUploadDialogConfirm(c)}
      />
      <div className="grid-2">
        <div className="stack">
          {section === 0 ? (
            <section className="group stack">
              <h3 className="group-title">Части</h3>
              <SourcePicker
                label="Часть 1 — видео"
                value={part1}
                onChange={setPart1}
                kind="video"
                accept="video/*"
              />
              <SourcePicker
                label="Часть 2 — видео"
                value={part2}
                onChange={setPart2}
                kind="video"
                accept="video/*"
              />
              <OutputFolderPicker
                kind="gluing"
                platform={platform}
                value={outputDir}
                onChange={setOutputDir}
                disabled={running}
              />
              <label>
                Количество роликов{" "}
                <input
                  className="field"
                  style={{ width: 90 }}
                  type="number"
                  min={1}
                  value={copies}
                  onChange={(e) => setCopies(Number(e.target.value) || 1)}
                />
              </label>
              <label className="hint">Длительность частей (сек)</label>
              <RangeSlider
                min={0.3}
                max={30}
                step={0.1}
                decimals={1}
                value={partDuration}
                onChange={setPartDuration}
                suffix="с"
              />
              <p className="hint">GPU / потоки — в Настройках.</p>
            </section>
          ) : null}

          {section === 1 ? (
            <TextOverlayFields
              value={textOverlay}
              onChange={setTextOverlay}
              showAfterFrameChange
            />
          ) : null}

          {section === 2 ? (
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

          {section === 3 ? (
            <section className="group stack">
              <h3 className="group-title">Переходы</h3>
              <label className="row" style={{ gap: 8 }}>
                <input
                  type="checkbox"
                  checked={transitionRandom}
                  onChange={(e) => {
                    const on = e.target.checked;
                    setTransitionRandom(on);
                    if (on) {
                      if (transition) setLastTransition(transition);
                      setTransition("");
                    } else {
                      setTransition(lastTransition || "cut");
                    }
                  }}
                />
                <span>
                  <strong>Выбирать рандомно</strong>
                  <div className="hint">
                    Перед каждым роликом — случайный переход, включая простую
                    склейку.
                  </div>
                </span>
              </label>
              {TRANSITIONS.map((t) => (
                <label
                  key={t.id}
                  className="row"
                  style={{
                    alignItems: "flex-start",
                    gap: 8,
                    opacity: transitionRandom ? 0.5 : 1,
                  }}
                >
                  <input
                    type="radio"
                    name="stitch-transition"
                    checked={transition === t.id}
                    disabled={transitionRandom}
                    onChange={() => {
                      setTransition(t.id);
                      setLastTransition(t.id);
                    }}
                    style={{ marginTop: 4 }}
                  />
                  <span>
                    <strong>{t.label}</strong>
                    <div className="hint">{t.hint}</div>
                  </span>
                </label>
              ))}
              <p className="hint">
                Длительность ролика = сумма двух полных исходников. Переход на
                бит; если фрагмент не найден — старт ~10% трека, при нехватке
                хвост зацикливается.
              </p>
            </section>
          ) : null}
        </div>
        <JobLogBox
          lines={mergeJobLogLines(job?.logs, uploadJob?.logs)}
          emptyHint="Лог склейки…"
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
