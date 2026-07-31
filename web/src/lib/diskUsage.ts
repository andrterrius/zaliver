/** Format byte size for disk usage hints. */
export function formatBytes(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n) || n < 0) return "—";
  if (n < 1024) return `${n} Б`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} КБ`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} МБ`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(1)} ГБ`;
}

export type DiskUsage = {
  disk_total?: number | null;
  disk_used?: number | null;
  disk_free?: number | null;
};

export function formatDiskUsage(u: DiskUsage | null | undefined): string | null {
  if (!u) return null;
  const total = u.disk_total;
  const used = u.disk_used;
  const free = u.disk_free;
  if (total == null || used == null || free == null) return null;
  const pct = total > 0 ? Math.round((used / total) * 100) : 0;
  return `Диск: занято ${formatBytes(used)} из ${formatBytes(total)} (${pct}%) · свободно ${formatBytes(free)}`;
}
