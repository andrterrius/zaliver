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
  const [part1, setPart1] = useState("");
  const [part2, setPart2] = useState("");
  const [music, setMusic] = useState("");
  const [outputDir, setOutputDir] = useState("");
  const [copies, setCopies] = useState(1);
  const [workers, setWorkers] = useState(2);
  const [transition, setTransition] = useState<(typeof TRANSITIONS)[number]["id"] | "">(
    "cut"
  );
  const [lastTransition, setLastTransition] =
    useState<(typeof TRANSITIONS)[number]["id"]>("cut");
  const [transitionRandom, setTransitionRandom] = useState(false);
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
      const part1_files = linesToList(part1);
      const part2_files = linesToList(part2);
      const music_files = linesToList(music);
      if (!outputDir.trim()) throw new Error("Укажите выходную папку.");
      if (!part1_files.length) throw new Error("Добавьте клипы для части 1.");
      if (!part2_files.length) throw new Error("Добавьте клипы для части 2.");
      if (!music_files.length) throw new Error("Добавьте аудиотреки.");
      const res = await api.startStitching({
        output_dir: outputDir.trim(),
        part1_files,
        part2_files,
        music_files,
        copies_per_track: copies,
        num_workers: workers,
        transition: transition || lastTransition,
        transition_duration: 0.4,
        transition_random: transitionRandom,
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
      {error ? <div className="error-banner">{error}</div> : null}
      <div className="grid-2">
        <section className="group stack">
          <h3 className="group-title">Части и музыка</h3>
          <label className="hint">Часть 1 — видео (пути)</label>
          <textarea
            className="field"
            value={part1}
            onChange={(e) => setPart1(e.target.value)}
          />
          <label className="hint">Часть 2 — видео (пути)</label>
          <textarea
            className="field"
            value={part2}
            onChange={(e) => setPart2(e.target.value)}
          />
          <label className="hint">Аудиотреки</label>
          <textarea
            className="field"
            value={music}
            onChange={(e) => setMusic(e.target.value)}
          />
          <label className="hint">Выходная папка</label>
          <input
            className="field"
            value={outputDir}
            onChange={(e) => setOutputDir(e.target.value)}
          />
          <div className="row">
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
            <label>
              Потоки{" "}
              <input
                className="field"
                style={{ width: 90 }}
                type="number"
                min={1}
                value={workers}
                onChange={(e) => setWorkers(Number(e.target.value) || 1)}
              />
            </label>
          </div>
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
                Перед каждым роликом — случайный переход, включая простую склейку.
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
            Длительность ролика = сумма двух полных исходников. Переход на бит;
            если фрагмент не найден — старт ~10% трека, при нехватке хвост
            зацикливается.
          </p>
        </section>
        <section className="group stack">
          <h3 className="group-title">Лог</h3>
          <pre className="log">{(job?.log_tail || []).join("\n")}</pre>
        </section>
      </div>
    </div>
  );
}
