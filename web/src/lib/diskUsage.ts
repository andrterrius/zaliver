/** Format byte size for disk usage hints. */
export function formatBytes(n: number | null | undefined): string {
  const v = typeof n === "string" ? Number(n) : n;
  if (v == null || Number.isNaN(Number(v)) || Number(v) < 0) return "—";
  const x = Number(v);
  if (x < 1024) return `${Math.round(x)} Б`;
  if (x < 1024 * 1024) return `${(x / 1024).toFixed(1)} КБ`;
  if (x < 1024 * 1024 * 1024) return `${(x / (1024 * 1024)).toFixed(1)} МБ`;
  return `${(x / (1024 * 1024 * 1024)).toFixed(1)} ГБ`;
}

export type DiskUsage = {
  disk_total?: number | null;
  disk_used?: number | null;
  disk_free?: number | null;
};

export function formatDiskUsage(u: DiskUsage | null | undefined): string {
  if (!u) return "Диск: нет данных";
  const total = u.disk_total == null ? null : Number(u.disk_total);
  const used = u.disk_used == null ? null : Number(u.disk_used);
  const free = u.disk_free == null ? null : Number(u.disk_free);
  if (
    total == null ||
    used == null ||
    free == null ||
    Number.isNaN(total) ||
    Number.isNaN(used) ||
    Number.isNaN(free)
  ) {
    return "Диск: нет данных";
  }
  const pct = total > 0 ? Math.round((used / total) * 100) : 0;
  return `Диск: занято ${formatBytes(used)} из ${formatBytes(total)} (${pct}%) · свободно ${formatBytes(free)}`;
}
