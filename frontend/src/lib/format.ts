/** Display helpers. Times are shown in UTC with an explicit "Z" so they match evidence and logs. */

export function formatDateTime(iso: string): string {
  return `${iso.slice(0, 10)} ${iso.slice(11, 19)}Z`;
}

export function formatTime(iso: string): string {
  return `${iso.slice(11, 19)}Z`;
}

/** "42 s ago", "5 min ago", "3 h ago", "2 d ago"; `null` → "never". `now` is injectable for tests. */
export function formatAgo(iso: string | null, now: number = Date.now()): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86_400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86_400)} d ago`;
}

const NUMBER = new Intl.NumberFormat("en-US");

export function formatCount(n: number): string {
  return NUMBER.format(n);
}

/** Duration between two instants, "1 m 52 s" style; open-ended when `to` is null. */
export function formatDuration(fromIso: string, toIso: string | null, now: number = Date.now()): string {
  const end = toIso ? Date.parse(toIso) : now;
  const s = Math.max(0, Math.round((end - Date.parse(fromIso)) / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h} h ${m} min`;
  if (m > 0) return `${m} min ${sec} s`;
  return `${sec} s`;
}
