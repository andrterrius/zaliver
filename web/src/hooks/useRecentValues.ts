import { useCallback, useEffect, useState } from "react";
import { api, type Platform, type RecentValues } from "../api/client";

const EMPTY: RecentValues = {
  platform: "youtube",
  upload_titles: [],
  channel_name_fields: [],
  channel_descriptions: [],
  channel_link_titles: [],
  channel_link_urls: [],
  video_default_title_fields: [],
  promote_comment_fields: [],
};

export function useRecentValues(platform: Platform, enabled = true) {
  const [recent, setRecent] = useState<RecentValues>({
    ...EMPTY,
    platform,
  });

  const refresh = useCallback(async () => {
    if (!enabled) return;
    try {
      const res = await api.listRecentValues();
      setRecent(res);
    } catch {
      /* ignore — picker stays empty */
    }
  }, [enabled]);

  useEffect(() => {
    void refresh();
  }, [platform, refresh]);

  return { recent, refresh };
}
