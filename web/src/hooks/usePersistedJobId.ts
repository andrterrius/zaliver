import { useCallback, useState } from "react";

function storageKey(kind: string): string {
  return `zaliver:lastJob:${kind}`;
}

export function usePersistedJobId(kind: string) {
  const [jobId, setJobIdState] = useState<string | null>(() => {
    try {
      return sessionStorage.getItem(storageKey(kind));
    } catch {
      return null;
    }
  });

  const setJobId = useCallback(
    (id: string | null) => {
      setJobIdState(id);
      try {
        const key = storageKey(kind);
        if (id) sessionStorage.setItem(key, id);
        else sessionStorage.removeItem(key);
      } catch {
        /* ignore quota / private mode */
      }
    },
    [kind],
  );

  return [jobId, setJobId] as const;
}
