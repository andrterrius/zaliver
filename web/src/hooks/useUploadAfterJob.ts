import { useEffect, useRef } from "react";
import { api, type Job, type Platform } from "../api/client";
import type { UploadAfterChoice } from "../components/UploadAfterDialog";

export type PendingUpload = UploadAfterChoice & {
  /** Bind pending upload to the processing job that was just started. */
  processingJobId?: string;
  platform?: Platform;
  /** Total videos planned for this processing run (inputs × copies). */
  plannedVideos?: number;
};

/** Match server: processing is always 1 thread per user. */
export const STREAMING_UPLOAD_WORKERS = 1;

const pendingKey = (kind: string) => `zaliver:pendingUpload:${kind}`;

export function savePendingUpload(kind: string, choice: PendingUpload | null) {
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

export function loadPendingUpload(kind: string): PendingUpload | null {
  try {
    const raw = sessionStorage.getItem(pendingKey(kind));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PendingUpload;
    if (!parsed?.profileIds?.length) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function workersForUploadChoice(
  _choice: UploadAfterChoice,
  _fallback: number,
): number {
  return 1;
}

/** Payload for server-driven upload-after / upload-as-ready. */
export function uploadAfterPayload(
  choice: UploadAfterChoice,
  platform: Platform,
  plannedVideos: number,
) {
  if (!choice.profileIds.length) return undefined;
  return {
    profile_ids: choice.profileIds,
    title: choice.title,
    description: choice.description,
    platform,
    kind: "local",
    headless: choice.headless,
    max_concurrent_browsers: choice.maxBrowsers,
    publish_before_checks: choice.publishBeforeChecks,
    keep_studio_title: choice.keepStudioTitle,
    schedule_publish: choice.schedulePublish,
    schedule_times_iso: choice.scheduleTimesIso || [],
    schedule_warmup_shorts: choice.scheduleWarmupShorts,
    schedule_warmup_shorts_recommendations:
      choice.scheduleWarmupRecommendations,
    schedule_warmup_search_query: choice.scheduleWarmupSearchQuery || "",
    delete_after_upload: choice.deleteAfterUpload,
    upload_as_ready: choice.uploadAsReady,
    planned_videos: Math.max(0, plannedVideos),
  };
}

async function persistUploadSettings(
  kind: string,
  pending: PendingUpload,
): Promise<void> {
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
}

/**
 * Track server-driven upload followup via processing job.linked_upload_job_id.
 * Fallback for old API: start upload once after processing succeeds.
 */
export function useUploadAfterJob(
  kind: string,
  job: Job | null,
  setUploadJobId: (id: string | null) => void,
  onError?: (msg: string) => void,
  platform?: Platform | null,
) {
  const boundJobId = useRef<string | null>(null);
  const linkedSet = useRef(false);
  const fallbackDone = useRef(false);
  const settingsSaved = useRef(false);

  useEffect(() => {
    if (!job) return;
    const pending = loadPendingUpload(kind);
    if (!pending) return;
    if (pending.processingJobId && pending.processingJobId !== job.id) {
      return;
    }

    if (boundJobId.current !== job.id) {
      boundJobId.current = job.id;
      linkedSet.current = false;
      fallbackDone.current = false;
      settingsSaved.current = false;
    }

    const finished =
      job.status === "succeeded" ||
      job.status === "failed" ||
      job.status === "cancelled";

    void (async () => {
      try {
        if (!settingsSaved.current) {
          settingsSaved.current = true;
          await persistUploadSettings(kind, pending);
        }

        const linked = String(job.linked_upload_job_id || "").trim();
        if (linked && !linkedSet.current) {
          linkedSet.current = true;
          setUploadJobId(linked);
        }

        // Server owns upload-as-ready / upload-after when followup is attached.
        if (job.upload_followup_active || linkedSet.current) {
          if (finished) savePendingUpload(kind, null);
          return;
        }

        if (!finished) return;
        if (job.status !== "succeeded" || fallbackDone.current) {
          if (job.status !== "succeeded") {
            savePendingUpload(kind, null);
            fallbackDone.current = true;
          }
          return;
        }

        fallbackDone.current = true;
        const plat = pending.platform || platform || undefined;
        if (plat) {
          try {
            await api.setPlatform(plat);
          } catch {
            /* continue */
          }
        }
        let outputs = (job.outputs || []).filter(Boolean);
        if (!outputs.length) {
          try {
            const fresh = await api.getJob(job.id);
            outputs = (fresh.outputs || []).filter(Boolean);
          } catch {
            /* keep empty */
          }
        }
        if (!outputs.length) {
          savePendingUpload(kind, null);
          onError?.("Обработка завершена, но нет путей к видео для залива.");
          return;
        }
        const res = await api.startUpload({
          profile_ids: pending.profileIds,
          video_paths: outputs,
          title: pending.title,
          description: pending.description,
          ...(plat ? { platform: plat } : {}),
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
        savePendingUpload(kind, null);
        setUploadJobId(res.id);
      } catch (e) {
        onError?.(e instanceof Error ? e.message : String(e));
        fallbackDone.current = false;
      }
    })();
  }, [job, kind, setUploadJobId, onError, platform]);
}
