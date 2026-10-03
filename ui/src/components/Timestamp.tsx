import { formatTimestamp } from "../lib/time";

/**
 * A timestamp shown in local time, with the original ISO string (exactly as returned) and UTC in the tooltip and
 * in the machine-readable `dateTime`. Invalid values are shown verbatim, never "fixed".
 */
export function Timestamp({ iso, label }: { iso: string; label?: string }) {
  const t = formatTimestamp(iso);
  const title = t.valid ? `Original: ${t.original}\nUTC: ${t.utc}` : `Original: ${t.original}`;
  return (
    <time className="timestamp" dateTime={t.valid ? t.original : undefined} title={title} data-original={t.original}>
      {label ? <span className="timestamp-label">{label} </span> : null}
      {t.local}
    </time>
  );
}
