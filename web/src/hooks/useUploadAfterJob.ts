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
  try {
    const fresh = await api.getJob(job.id);
    outputs = (fresh.outputs || []).filter(Boolean);
  } catch {
    /* keep empty */
  }
  return outputs;
}

function minReadyFor(pending: PendingUpload, plannedHint: number): number {
  const nProf = Math.max(1, pending.profileIds.length);
  const target = nProf * 2;
  if (plannedHint > 0) return Math.max(1, Math.min(target, plannedHint));
  return Math.max(1, target);
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

function uploadBody(
  pending: PendingUpload,
  outputs: string[],
  plat: Platform | undefined,
  extra: Record<string, unknown> = {},
) {
  return {
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
    ...extra,
  };
}

/** After processing job succeeds (or streams), start upload from pending + outputs. */
export function useUploadAfterJob(
  kind: string,
  job: Job | null,
  setUploadJobId: (id: string | null) => void,
  onError?: (msg: string) => void,
  platform?: Platform | null,
) {
  const boundJobId = useRef<string | null>(null);
  const starting = useRef(false);
  const uploadJobIdRef = useRef<string | null>(null);
  const enqueuedRef = useRef<Set<string>>(new Set());
  const producerDoneRef = useRef(false);
  const batchDoneRef = useRef(false);

  useEffect(() => {
    if (!job) return;
    const pending = loadPendingUpload(kind);
    if (!pending) return;
    if (pending.processingJobId && pending.processingJobId !== job.id) {
      return;
    }

    if (boundJobId.current !== job.id) {
      boundJobId.current = job.id;
      uploadJobIdRef.current = null;
      enqueuedRef.current = new Set();
      producerDoneRef.current = false;
      batchDoneRef.current = false;
      starting.current = false;
    }

    const plat = pending.platform || platform || undefined;
    const streaming = Boolean(pending.uploadAsReady);
    const finished =
      job.status === "succeeded" ||
      job.status === "failed" ||
      job.status === "cancelled";

    void (async () => {
      try {
        if (plat) {
          try {
            await api.setPlatform(plat);
          } catch {
            /* continue */
          }
        }

        if (!streaming) {
          if (job.status !== "succeeded" || batchDoneRef.current || starting.current) {
            return;
          }
          starting.current = true;
          try {
            const all = await outputsForJob(job);
            if (!all.length) {
              savePendingUpload(kind, null);
              batchDoneRef.current = true;
              onError?.(
                "Обработка завершена, но нет путей к видео для залива.",
              );
              return;
            }
            await persistUploadSettings(kind, pending);
            const res = await api.startUpload(uploadBody(pending, all, plat));
            batchDoneRef.current = true;
            savePendingUpload(kind, null);
            setUploadJobId(res.id);
          } finally {
            starting.current = false;
          }
          return;
        }

        // --- upload-as-ready (streaming) ---
        const outputs = (job.outputs || []).filter(Boolean);
        const minReady = minReadyFor(pending, outputs.length);

        if (
          !uploadJobIdRef.current &&
          !starting.current &&
          outputs.length >= minReady &&
          (job.status === "running" || job.status === "succeeded")
        ) {
          starting.current = true;
          try {
            await persistUploadSettings(kind, pending);
            const res = await api.startUpload(
              uploadBody(pending, outputs, plat, {
                await_more_videos: true,
                planned_videos: Math.max(outputs.length, minReady),
              }),
            );
            uploadJobIdRef.current = res.id;
            for (const p of outputs) enqueuedRef.current.add(p);
            setUploadJobId(res.id);
          } finally {
            starting.current = false;
          }
        }

        const liveUploadId = uploadJobIdRef.current;
        if (liveUploadId && outputs.length) {
          const fresh = outputs.filter((p) => !enqueuedRef.current.has(p));
          if (fresh.length) {
            try {
              await api.enqueueUpload(liveUploadId, {
                video_paths: fresh,
                title: pending.title,
                description: pending.description,
              });
              for (const p of fresh) enqueuedRef.current.add(p);
            } catch (e) {
              onError?.(e instanceof Error ? e.message : String(e));
            }
          }
        }

        if (!finished || producerDoneRef.current || batchDoneRef.current) {
          return;
        }

        // Processing ended before buffer filled — one-shot upload.
        if (!uploadJobIdRef.current) {
          if (job.status !== "succeeded" || starting.current) {
            if (job.status !== "succeeded") {
              savePendingUpload(kind, null);
              batchDoneRef.current = true;
            }
            return;
          }
          starting.current = true;
          try {
            const all = await outputsForJob(job);
            if (!all.length) {
              savePendingUpload(kind, null);
              batchDoneRef.current = true;
              onError?.(
                "Обработка завершена, но нет путей к видео для залива.",
              );
              return;
            }
            await persistUploadSettings(kind, pending);
            const res = await api.startUpload(uploadBody(pending, all, plat));
            batchDoneRef.current = true;
            savePendingUpload(kind, null);
            setUploadJobId(res.id);
          } finally {
            starting.current = false;
          }
          return;
        }

        producerDoneRef.current = true;
        try {
          await api.uploadProducerDone(uploadJobIdRef.current);
        } catch {
          /* already draining */
        }
        savePendingUpload(kind, null);
        batchDoneRef.current = true;
      } catch (e) {
        onError?.(e instanceof Error ? e.message : String(e));
        starting.current = false;
      }
    })();
  }, [job, kind, setUploadJobId, onError, platform]);
}
