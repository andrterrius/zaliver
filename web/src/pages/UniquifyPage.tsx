import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
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
} from "../hooks/useUploadAfterJob";
import { ProgressBar } from "../components/ProgressBar";
import { SectionNav } from "../components/SectionNav";
import { SourcePicker } from "../components/SourcePicker";
import { ToggleSwitch } from "../components/ToggleSwitch";
import { RangeSlider, type RangeValue } from "../components/RangeSlider";
import {
  UploadAfterDialog,
  type UploadAfterChoice,
} from "../components/UploadAfterDialog";
import { JobLogBox, mergeJobLogLines } from "../components/JobLogBox";
import { OutputFolderPicker } from "../components/OutputFolderPicker";
import {
  TextOverlayFields,
  defaultUniquifyTextOverlay,
  textOverlayToApi,
  type TextOverlayState,
} from "../components/TextOverlayFields";

const SECTIONS = ["Исходники", "Фильтры", "Текст", "Музыка"];

type FxKey =
  | "brightness"
  | "contrast"
  | "saturation"
  | "scale"
  | "noise"
  | "speed";

type FxRow = {
  key: FxKey;
  label: string;
  enabled: boolean;
  range: RangeValue;
  min: number;
  max: number;
  step: number;
  decimals: number;
  suffix?: string;
};

const DEFAULT_FX: FxRow[] = [
  {
    key: "brightness",
    label: "Яркость",
    enabled: true,
    range: { lo: -22, hi: 22 },
    min: -40,
    max: 40,
    step: 0.5,
    decimals: 1,
  },
  {
    key: "contrast",
    label: "Контраст",
    enabled: true,
    range: { lo: 0.88, hi: 1.14 },
    min: 0.7,
    max: 1.4,
    step: 0.01,
    decimals: 2,
  },
  {
    key: "saturation",
    label: "Насыщенность",
    enabled: true,
    range: { lo: 0.88, hi: 1.12 },
    min: 0.7,
    max: 1.4,
    step: 0.01,
    decimals: 2,
  },
  {
    key: "scale",
    label: "Масштаб",
    enabled: true,
    range: { lo: 95, hi: 100.6 },
    min: 90,
    max: 110,
    step: 0.1,
    decimals: 1,
  },
  {
    key: "noise",
    label: "Шум",
    enabled: true,
    range: { lo: 0.5, hi: 4 },
    min: 0,
    max: 10,
    step: 0.05,
    decimals: 2,
  },
  {
    key: "speed",
    label: "Скорость видео+аудио",
    enabled: true,
    range: { lo: 1, hi: 1.1 },
    min: 0.85,
    max: 1.25,
    step: 0.01,
    decimals: 2,
  },
];

function fxByKey(rows: FxRow[], key: FxKey): FxRow {
  return rows.find((r) => r.key === key)!;
}

function loadFx(v: Record<string, unknown>): FxRow[] {
  return DEFAULT_FX.map((row) => {
    const enKey =
      row.key === "speed" ? "playback_speed_enabled" : `fx_${row.key}_enabled`;
    const minKey = row.key === "speed" ? "fx_speed_min" : `fx_${row.key}_min`;
    const maxKey = row.key === "speed" ? "fx_speed_max" : `fx_${row.key}_max`;
    return {
      ...row,
      enabled: asBool(v[enKey], row.enabled),
      range: asRange(v[minKey], v[maxKey], row.range),
    };
  });
}

export function UniquifyPage() {
  const proc = useProcessingDefaults();
  const [section, setSection] = useState(0);
  const [hydrated, setHydrated] = useState(false);
  const [inputFiles, setInputFiles] = useState<string[]>([]);
  const [outputDir, setOutputDir] = useState("");
  const [copies, setCopies] = useState(1);
  const [oneCopyNoFx, setOneCopyNoFx] = useState(false);
  const [deleteAfter, setDeleteAfter] = useState(true);
  const [fx, setFx] = useState<FxRow[]>(DEFAULT_FX);
  const [textOverlay, setTextOverlay] = useState<TextOverlayState>(
    defaultUniquifyTextOverlay,
  );
  const [musicEnabled, setMusicEnabled] = useState(false);
  const [musicFiles, setMusicFiles] = useState<string[]>([]);
  const [musicMix, setMusicMix] = useState(false);
  const [musicVol, setMusicVol] = useState<RangeValue>({ lo: 35, hi: 35 });
  const [jobId, setJobId] = usePersistedJobId("uniquify");
  const [uploadJobId, setUploadJobId] = usePersistedJobId("upload-after-uniquify");
  const [uploadDialogOpen, setUploadDialogOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const { job } = useJobPoll(jobId);
  const { job: uploadJob } = useJobPoll(uploadJobId);
  const onUploadErr = useCallback((msg: string) => setError(msg), []);
  useUploadAfterJob("uniquify", job, setUploadJobId, onUploadErr);

  const running =
    busy ||
    (job != null && ["queued", "running"].includes(job.status)) ||
    (uploadJob != null && ["queued", "running"].includes(uploadJob.status));

  useEffect(() => {
    void (async () => {
      try {
        const s = await api.getSettings();
        const v = s.values;
        setInputFiles(asStringList(v.input_files));
        setOutputDir(String(v.output_folder || "").trim());
        setCopies(Math.max(1, Math.round(asNumber(v.copies_per_file, 1))));
        setOneCopyNoFx(asBool(v.one_copy_no_effects, false));
        setDeleteAfter(asBool(v.delete_after_upload, true));
        setFx(loadFx(v));
        setTextOverlay(textOverlayFromSettings(v, "", defaultUniquifyTextOverlay()));
        setMusicEnabled(asBool(v.background_music_enabled, false));
        setMusicFiles(asStringList(v.background_music_files));
        setMusicMix(asBool(v.background_music_mix_with_source, false));
        setMusicVol(
          asRange(
            v.background_music_volume_pct_min ?? v.background_music_volume_pct,
            v.background_music_volume_pct_max ?? v.background_music_volume_pct,
            { lo: 35, hi: 35 },
          ),
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
    const br = fxByKey(fx, "brightness");
    const ct = fxByKey(fx, "contrast");
    const sat = fxByKey(fx, "saturation");
    const sc = fxByKey(fx, "scale");
    const nz = fxByKey(fx, "noise");
    const sp = fxByKey(fx, "speed");
    return {
      input_files: inputFiles,
      output_folder: outputDir,
      copies_per_file: copies,
      one_copy_no_effects: oneCopyNoFx,
      delete_after_upload: deleteAfter,
      fx_brightness_enabled: br.enabled,
      fx_brightness_min: br.range.lo,
      fx_brightness_max: br.range.hi,
      fx_contrast_enabled: ct.enabled,
      fx_contrast_min: ct.range.lo,
      fx_contrast_max: ct.range.hi,
      fx_saturation_enabled: sat.enabled,
      fx_saturation_min: sat.range.lo,
      fx_saturation_max: sat.range.hi,
      fx_scale_enabled: sc.enabled,
      fx_scale_min: sc.range.lo,
      fx_scale_max: sc.range.hi,
      fx_noise_enabled: nz.enabled,
      fx_noise_min: nz.range.lo,
      fx_noise_max: nz.range.hi,
      playback_speed_enabled: sp.enabled,
      fx_speed_min: sp.range.lo,
      fx_speed_max: sp.range.hi,
      background_music_enabled: musicEnabled,
      background_music_files: musicFiles,
      background_music_mix_with_source: musicMix,
      background_music_volume_pct: Math.round(musicVol.lo),
      background_music_volume_pct_min: Math.round(musicVol.lo),
      background_music_volume_pct_max: Math.round(musicVol.hi),
      ...textOverlayToSettings(textOverlay, ""),
    };
  }, [
    hydrated,
    inputFiles,
    outputDir,
    copies,
    oneCopyNoFx,
    deleteAfter,
    fx,
    musicEnabled,
    musicFiles,
    musicMix,
    musicVol,
    textOverlay,
  ]);

  useDebouncedSettingsPatch(persistValues);

  useEffect(() => {
    if (job && ["failed", "cancelled"].includes(job.status)) {
      savePendingUpload("uniquify", null);
    }
  }, [job]);

  const updateFx = (key: FxKey, partial: Partial<FxRow>) => {
    setFx((rows) => rows.map((r) => (r.key === key ? { ...r, ...partial } : r)));
  };

  const onStart = () => {
    setError("");
    if (!inputFiles.length) {
      setError("Укажите хотя бы один входной файл.");
      return;
    }
    setUploadDialogOpen(true);
  };

  const onUploadDialogConfirm = async (choice: UploadAfterChoice) => {
    setUploadDialogOpen(false);
    setBusy(true);
    setError("");
    try {
      const files = inputFiles;
      if (!files.length) throw new Error("Укажите хотя бы один входной файл.");

      const br = fxByKey(fx, "brightness");
      const ct = fxByKey(fx, "contrast");
      const sat = fxByKey(fx, "saturation");
      const sc = fxByKey(fx, "scale");
      const nz = fxByKey(fx, "noise");
      const sp = fxByKey(fx, "speed");

      const random_bounds = {
        brightness_min: br.range.lo,
        brightness_max: br.range.hi,
        contrast_min: ct.range.lo,
        contrast_max: ct.range.hi,
        saturation_min: sat.range.lo,
        saturation_max: sat.range.hi,
        crop_jitter_min: 0,
        crop_jitter_max: 0,
        scale_pct_min: sc.range.lo,
        scale_pct_max: sc.range.hi,
        noise_sigma_min: nz.range.lo,
        noise_sigma_max: nz.range.hi,
        seed_min: 0,
        seed_max: 0,
        playback_speed_min: sp.range.lo,
        playback_speed_max: sp.range.hi,
        audio_chorus_prob: 0,
        audio_chorus_prob_min: 0,
        audio_chorus_prob_max: 0,
      };

      const settings = {
        brightness_delta: br.range.lo,
        contrast: ct.range.lo,
        saturation_scale: sat.range.lo,
        crop_jitter_px: 0,
        scale_pct: sc.range.lo,
        noise_sigma: nz.range.lo,
        seed_base: 0,
        playback_speed_factor: sp.range.lo,
        audio_chorus: false,
      };

      if (persistValues) {
        await api.patchSettings(persistValues);
      }

      const willUpload = choice.profileIds.length > 0;
      savePendingUpload("uniquify", willUpload ? choice : null);

      const res = await api.startUniquify({
        output_dir: outputDir,
        input_files: files,
        copies_per_file: copies,
        num_workers: proc.numWorkers,
        use_gpu: proc.useGpu,
        use_gpu_finalize: proc.useGpuFinalize,
        randomize_uniquify: true,
        one_copy_no_effects: oneCopyNoFx,
        brightness_enabled: br.enabled,
        contrast_enabled: ct.enabled,
        saturation_enabled: sat.enabled,
        scale_enabled: sc.enabled,
        noise_enabled: nz.enabled,
        playback_speed_enabled: sp.enabled,
        background_music_enabled: musicEnabled,
        background_music_mix_with_source: musicMix,
        background_music_volume_pct: Math.round(musicVol.lo),
        background_music_volume_pct_min: Math.round(musicVol.lo),
        background_music_volume_pct_max: Math.round(musicVol.hi),
        background_music_files: musicFiles,
        settings,
        random_bounds,
        text_overlay: textOverlayToApi(textOverlay),
        youtube_upload_after_processing: willUpload,
      });
      setJobId(res.id);
      setUploadJobId(null);
    } catch (e) {
      savePendingUpload("uniquify", null);
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
      <SectionNav sections={SECTIONS} active={section} onChange={setSection} />
      <p className="hint">
        Выбор видео → папка результатов · случайная уникализация · GPU/потоки в
        Настройках
      </p>
      {error ? <div className="error-banner">{error}</div> : null}

      <UploadAfterDialog
        open={uploadDialogOpen}
        mode="uniquify"
        onCancel={() => setUploadDialogOpen(false)}
        onConfirm={(c) => void onUploadDialogConfirm(c)}
      />

      <div className="grid-2">
        <div className="stack">
          {section === 0 ? (
            <section className="group stack">
              <h3 className="group-title">Файлы</h3>
              <SourcePicker
                label="Исходные видео"
                value={inputFiles}
                onChange={setInputFiles}
                kind="video"
                accept="video/*"
              />
              <OutputFolderPicker
                kind="uniquify"
                value={outputDir}
                onChange={setOutputDir}
                disabled={running}
              />
              <p className="hint">
                Результат сохраняется в выбранную папку внутри результатов
                (уникализация).
              </p>
              <div className="row">
                <label>
                  Копий на исходник{" "}
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
                <label className="check">
                  <input
                    type="checkbox"
                    checked={oneCopyNoFx}
                    onChange={(e) => setOneCopyNoFx(e.target.checked)}
                  />
                  1 копия без эффектов
                </label>
              </div>
              <p className="hint">
                Каждая копия — отдельный прогон со своими случайными параметрами.
                Имена: имя_u_&lt;hex&gt;.mp4.
              </p>
              <label className="check">
                <input
                  type="checkbox"
                  checked={deleteAfter}
                  onChange={(e) => setDeleteAfter(e.target.checked)}
                />
                Удалять после залива
              </label>
            </section>
          ) : null}

          {section === 1 ? (
            <section className="group stack">
              <h3 className="group-title">Фильтры</h3>
              <p className="hint">
                Включите эффект и задайте диапазон — на каждый ролик значения
                выбираются случайно.
              </p>
              {fx.map((row) => (
                <div key={row.key} className="fx-row">
                  <label className="check" title={row.label}>
                    <input
                      type="checkbox"
                      checked={row.enabled}
                      onChange={(e) => updateFx(row.key, { enabled: e.target.checked })}
                    />
                  </label>
                  <span className="fx-label">{row.label}</span>
                  <RangeSlider
                    min={row.min}
                    max={row.max}
                    step={row.step}
                    decimals={row.decimals}
                    suffix={row.suffix}
                    value={row.range}
                    disabled={!row.enabled}
                    onChange={(range) => updateFx(row.key, { range })}
                  />
                </div>
              ))}
            </section>
          ) : null}

          {section === 2 ? (
            <TextOverlayFields value={textOverlay} onChange={setTextOverlay} />
          ) : null}

          {section === 3 ? (
            <section className="group stack">
              <h3 className="group-title">Фоновые треки</h3>
              <ToggleSwitch
                label="Добавить музыку"
                checked={musicEnabled}
                onChange={setMusicEnabled}
              />
              {musicEnabled ? (
                <>
                  <SourcePicker
                    label="Треки"
                    value={musicFiles}
                    onChange={setMusicFiles}
                    kind="audio"
                    accept="audio/*"
                  />
                  <ToggleSwitch
                    label="Смешивать с аудио исходника (иначе — полная замена дорожки)"
                    checked={musicMix}
                    onChange={setMusicMix}
                  />
                  <div className="form-grid">
                    <label className="hint">Громкость музыки</label>
                    <RangeSlider
                      min={0}
                      max={100}
                      step={1}
                      decimals={0}
                      suffix=" %"
                      value={musicVol}
                      disabled={!musicMix}
                      onChange={setMusicVol}
                    />
                  </div>
                  <p className="hint">
                    Разведите точки — случайная громкость в диапазоне на каждый
                    ролик. Громкость активна при смешивании.
                  </p>
                </>
              ) : null}
            </section>
          ) : null}
        </div>

        <JobLogBox
          lines={mergeJobLogLines(job?.logs, uploadJob?.logs)}
          emptyHint="Лог появится после старта задачи…"
        >
          {job ? (
            <p className="hint" style={{ marginTop: 8 }}>
              Задача {job.id.slice(0, 8)}… · {job.status}
              {job.message ? ` · ${job.message}` : ""}
            </p>
          ) : null}
          {uploadJob ? (
            <p className="hint" style={{ marginTop: 4 }}>
              Залив {uploadJob.id.slice(0, 8)}… · {uploadJob.status}
              {uploadJob.progress.total
                ? ` · ${uploadJob.progress.current}/${uploadJob.progress.total}`
                : ""}
              {uploadJob.message ? ` · ${uploadJob.message}` : ""}
            </p>
          ) : null}
        </JobLogBox>
      </div>
    </div>
  );
}
