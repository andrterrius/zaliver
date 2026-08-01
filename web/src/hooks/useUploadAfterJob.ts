import { useEffect, useRef } from "react";
import { api, type Job, type Platform } from "../api/client";
import type { UploadAfterChoice } from "../components/UploadAfterDialog";

export type PendingUpload = UploadAfterChoice & {
  /** Bind pending upload to the processing job that was just started. */
  processingJobId?: string;
  platform?: Platform;
};

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

async function outputsForJob(job: Job): Promise<string[]> {
  let outputs = (job.outputs || []).filter(Boolean);
  if (outputs.length) return outputs;
  // Rare race: status already succeeded while outputs not yet in snapshot.
  try {
    const fresh = await api.getJob(job.id);
    outputs = (fresh.outputs || []).filter(Boolean);
  } catch {
    /* keep empty */
  }
  return outputs;
}

/** After processing job succeeds, start upload from pending choice + job.outputs. */
export function useUploadAfterJob(
  kind: string,
  job: Job | null,
  setUploadJobId: (id: string | null) => void,
  onError?: (msg: string) => void,
  platform?: Platform | null,
) {
  const startedFor = useRef<string | null>(null);
  const starting = useRef(false);

  useEffect(() => {
    if (!job || job.status !== "succeeded") return;
    if (startedFor.current === job.id || starting.current) return;
    const pending = loadPendingUpload(kind);
    if (!pending) return;
    if (pending.processingJobId && pending.processingJobId !== job.id) {
      return;
    }

    starting.current = true;
    void (async () => {
      try {
        const outputs = await outputsForJob(job);
        if (!outputs.length) {
          savePendingUpload(kind, null);
          onError?.(
            "Обработка завершена, но нет путей к видео для залива.",
          );
          return;
        }

        const plat = pending.platform || platform || undefined;
        if (plat) {
          try {
            await api.setPlatform(plat);
          } catch {
            /* continue — platform also sent on upload body */
          }
        }

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
        startedFor.current = job.id;
        savePendingUpload(kind, null);
        setUploadJobId(res.id);
      } catch (e) {
        // Keep pending so a remount / retry can pick it up.
        onError?.(e instanceof Error ? e.message : String(e));
      } finally {
        starting.current = false;
      }
    })();
  }, [job, kind, setUploadJobId, onError, platform]);
}
