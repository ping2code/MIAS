/**
 * Timestamp presentation (Phase 16A §11). The original ISO string is always preserved and shown in details;
 * the main display is the browser's local time. Nothing is rounded or rewritten.
 */
export interface FormattedTime {
  original: string;
  local: string;
  utc: string;
  valid: boolean;
}

const LOCAL = new Intl.DateTimeFormat(undefined, {
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  hour12: false, timeZoneName: "short",
});

export function formatTimestamp(iso: string): FormattedTime {
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return { original: iso, local: iso, utc: iso, valid: false };
  }
  return { original: iso, local: LOCAL.format(parsed), utc: parsed.toISOString(), valid: true };
}

export function formatClock(date: Date): FormattedTime {
  return formatTimestamp(date.toISOString());
}
