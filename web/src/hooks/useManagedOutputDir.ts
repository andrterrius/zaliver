import { useEffect, useState } from "react";
import { api } from "../api/client";

export type OutputKind = "uniquify" | "slicing" | "gluing";

export function useManagedOutputDir(kind: OutputKind) {
  const [path, setPath] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await api.getOutputDirs();
        if (!alive) return;
        setPath(res.dirs[kind] || "");
        setError("");
      } catch (e) {
        if (!alive) return;
        setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [kind]);

  return { path, error };
}
