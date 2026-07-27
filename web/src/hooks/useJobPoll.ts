import { useEffect, useRef, useState } from "react";
import { api, type Job } from "../api/client";

export function useJobPoll(jobId: string | null, intervalMs = 1000) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string>("");
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    if (!jobId) {
      setJob(null);
      return;
    }
    let timer: number | undefined;

    const tick = async () => {
      try {
        const j = await api.getJob(jobId);
        if (!alive.current) return;
        setJob(j);
        setError("");
        const done = ["succeeded", "failed", "cancelled"].includes(j.status);
        if (!done) {
          timer = window.setTimeout(tick, intervalMs);
        }
      } catch (e) {
        if (!alive.current) return;
        setError(e instanceof Error ? e.message : String(e));
        timer = window.setTimeout(tick, intervalMs * 2);
      }
    };

    void tick();
    return () => {
      alive.current = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [jobId, intervalMs]);

  return { job, error };
}
