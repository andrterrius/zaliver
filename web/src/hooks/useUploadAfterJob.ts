import { useEffect, useRef } from "react";
import { api, type Job } from "../api/client";
import type { UploadAfterChoice } from "../components/UploadAfterDialog";

const pendingKey = (kind: string) => `zaliver:pendingUpload:${kind}`;

export function savePendingUpload(kind: string, choice: UploadAfterChoice | null) {
  try {
    const key = pendingKey(kind);
    if (!choice || !choice.profileIds.length) {
      sessionStorage.removeItem(key);
      return;
    }
    sessionStorage.setItem(key, JSON.stringify(choice));
  } catch {
    /* ignore */
  }
}

export function loadPendingUpload(kind: string): UploadAfterChoice | null {
  try {
    const raw = sessionStorage.getItem(pendingKey(kind));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as UploadAfterChoice;
    if (!parsed?.profileIds?.length) return null;
    return parsed;
  } catch {
    return null;
  }
}

/** After processing job succeeds, start upload from pending choice + job.outputs. */
export function useUploadAfterJob(
  kind: string,
  job: Job | null,
  setUploadJobId: (id: string | null) => void,
  onError?: (msg: string) => void,
) {
  const startedFor = useRef<string | null>(null);

  useEffect(() => {
    if (!job || job.status !== "succeeded") return;
    if (startedFor.current === job.id) return;
    const pending = loadPendingUpload(kind);
    if (!pending) return;

    const outputs = (job.outputs || []).filter(Boolean);
    if (!outputs.length) {
      savePendingUpload(kind, null);
      onError?.(
        "Обработка завершена, но нет путей к видео для залива.",
      );
      return;
    }

    startedFor.current = job.id;
    savePendingUpload(kind, null);
    void (async () => {
      const delKey =
        kind === "slicing"
          ? "slice/delete_after_upload"
          : kind === "stitching"
            ? "stitch/delete_after_upload"
            : "delete_after_upload";
      try {
        await api.patchSettings({
          "antydetect/dolphin_headless": pending.headless,
          "antydetect/max_concurrent_browsers": pending.maxBrowsers,
          upload_title: pending.title,
          upload_description: pending.description,
          upload_as_ready: pending.uploadAsReady,
          [delKey]: pending.deleteAfterUpload,
        });
      } catch {
        /* optional persist */
      }
      try {
        const res = await api.startUpload({
          profile_ids: pending.profileIds,
          video_paths: outputs,
          title: pending.title,
          description: pending.description,
          headless: pending.headless,
          max_concurrent_browsers: pending.maxBrowsers,
          kind: "local",
          publish_before_checks: pending.publishBeforeChecks,
          keep_studio_title: pending.keepStudioTitle,
          schedule_publish: pending.schedulePublish,
          schedule_times_iso: pending.scheduleTimesIso || [],
          schedule_warmup_shorts: pending.scheduleWarmupShorts,
          schedule_warmup_shorts_recommendations:
            pending.scheduleWarmupRecommendations,
          schedule_warmup_search_query: pending.scheduleWarmupSearchQuery || "",
          delete_after_upload: pending.deleteAfterUpload,
        });
        setUploadJobId(res.id);
      } catch (e) {
        onError?.(e instanceof Error ? e.message : String(e));
      }
    })();
  }, [job, kind, setUploadJobId, onError]);
}
