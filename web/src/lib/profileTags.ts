/** Shared profile tag helpers (Profiles page + upload dialog). */

export function tagList(tags: unknown[]): string[] {
  return (tags || [])
    .map((t) => {
      if (typeof t === "string") return t;
      if (t && typeof t === "object" && "name" in t)
        return String((t as { name: unknown }).name);
      return String(t);
    })
    .filter(Boolean);
}

/** Как в десктопе / antic: ошибка — красный, успех — зелёный. */
export function tagTone(tag: string): "error" | "success" | null {
  const low = tag.toLocaleLowerCase("ru");
  if (low.includes("ошибка")) return "error";
  if (low.includes("успех") || low.startsWith("успеш")) return "success";
  return null;
}

export function tagPillClass(tag: string): string {
  const tone = tagTone(tag);
  if (tone === "error") return "pill pill-error";
  if (tone === "success") return "pill pill-success";
  return "pill";
}

export function tagFilterClass(tag: string, active: boolean): string {
  const tone = tagTone(tag);
  const parts = ["tag-chip"];
  if (active) parts.push("active");
  if (tone === "error") parts.push("tag-chip-error");
  if (tone === "success") parts.push("tag-chip-success");
  return parts.join(" ");
}

export function profileHasTagError(tags: unknown[]): boolean {
  return tagList(tags).some((t) => tagTone(t) === "error");
}

export function profileHasAccountData(
  customData: Record<string, unknown> | undefined,
  platform: "youtube" | "instagram" | "yt_inst",
): boolean {
  const cd = customData || {};
  if (platform === "instagram") {
    return Boolean(
      String(cd.inst_login || "").trim() ||
        String(cd.inst_password || "").trim() ||
        String(cd.inst_2fa || "").trim(),
    );
  }
  return Boolean(
    String(cd.yt_login || "").trim() ||
      String(cd.yt_password || "").trim() ||
      String(cd.yt_2fa || "").trim(),
  );
}

export function profileHasOldestChannel(
  customData: Record<string, unknown> | undefined,
): boolean {
  return Boolean(String((customData || {}).yt_oldest_name || "").trim());
}
