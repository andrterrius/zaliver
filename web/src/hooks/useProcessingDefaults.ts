import { useEffect, useState } from "react";
import { api } from "../api/client";

export type ProcessingDefaults = {
  numWorkers: number;
  useGpu: boolean;
  useGpuFinalize: boolean;
  sliceFpsMode: string;
  loaded: boolean;
};

const DEFAULTS: ProcessingDefaults = {
  numWorkers: 1,
  useGpu: false,
  useGpuFinalize: false,
  sliceFpsMode: "30",
  loaded: false,
};

export function useProcessingDefaults() {
  const [state, setState] = useState<ProcessingDefaults>(DEFAULTS);

  useEffect(() => {
    void (async () => {
      try {
        const s = await api.getSettings();
        const v = s.values;
        setState({
          numWorkers: 1,
          useGpu: Boolean(v["use_gpu_enabled"] ?? false),
          useGpuFinalize: Boolean(v["use_gpu_finalize_enabled"] ?? false),
          sliceFpsMode: String(v["slice/fps_mode"] ?? "30") || "30",
          loaded: true,
        });
      } catch {
        setState((prev) => ({ ...prev, loaded: true }));
      }
    })();
  }, []);

  return state;
}
