import { useState } from "react";
import { api } from "../api/client";
import { useJobPoll } from "../hooks/useJobPoll";
import { useManagedOutputDir } from "../hooks/useManagedOutputDir";
import { usePersistedJobId } from "../hooks/usePersistedJobId";
import { useProcessingDefaults } from "../hooks/useProcessingDefaults";
import { ProgressBar } from "../components/ProgressBar";
import { SectionNav } from "../components/SectionNav";
import { SourcePicker } from "../components/SourcePicker";
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

export function StitchingPage() {
  const proc = useProcessingDefaults();
  const [section, setSection] = useState(0);
  const [part1, setPart1] = useState<string[]>([]);
  const [part2, setPart2] = useState<string[]>([]);
  const [music, setMusic] = useState<string[]>([]);
  const { path: outputDir } = useManagedOutputDir("gluing");
  const [copies, setCopies] = useState(1);
  const [transition, setTransition] = useState<(typeof TRANSITIONS)[number]["id"] | "">(
    "cut",
  );
  const [lastTransition, setLastTransition] =
    useState<(typeof TRANSITIONS)[number]["id"]>("cut");
  const [transitionRandom, setTransitionRandom] = useState(false);
  const [partDuration, setPartDuration] = useState<RangeValue>({
    lo: 2,
    hi: 6,
  });
  const [textOverlay, setTextOverlay] = useState<TextOverlayState>(
    defaultStitchTextOverlay,
  );
  const [jobId, setJobId] = usePersistedJobId("stitching");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const { job } = useJobPoll(jobId);
  const running =
    busy || (job != null && ["queued", "running"].includes(job.status));

  const onStart = async () => {
    setError("");
    setBusy(true);
    try {
      const part1_files = part1;
      const part2_files = part2;
      const music_files = music;
      if (!part1_files.length) throw new Error("Добавьте клипы для части 1.");
      if (!part2_files.length) throw new Error("Добавьте клипы для части 2.");
      if (!music_files.length) throw new Error("Добавьте аудиотреки.");
      const res = await api.startStitching({
        part1_files,
        part2_files,
        music_files,
        copies_per_track: copies,
        num_workers: proc.numWorkers,
        use_gpu: proc.useGpu,
        use_gpu_finalize: proc.useGpuFinalize,
        transition: transition || lastTransition,
        transition_duration: 0.4,
        transition_random: transitionRandom,
        min_part_duration: partDuration.lo,
        max_part_duration: partDuration.hi,
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
              <p className="hint">
                Результат сохраняется на сервере:
                <br />
                <code>{outputDir || "…"}</code>
              </p>
              <label>
                Количество роликов{" "}
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
        <section className="group">
          <h3 className="group-title">Лог</h3>
          <div className="log-box">
            {job?.logs?.length ? job.logs.join("\n") : "Лог склейки…"}
          </div>
        </section>
      </div>
    </div>
  );
}
