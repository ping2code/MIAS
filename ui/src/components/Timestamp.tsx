import { formatTimestamp } from "../lib/time";

/**
 * A timestamp shown in local time, with the original ISO string (exactly as returned) and UTC in the tooltip and
 * in the machine-readable `dateTime`. `showOriginal` also prints both under the local time (detail pages). Invalid
 * values are shown verbatim, never "fixed".
 */
export function Timestamp({ iso, label, showOriginal = false }: { iso: string; label?: string; showOriginal?: boolean }) {
  const t = formatTimestamp(iso);
  const title = t.valid ? `Original: ${t.original}\nUTC: ${t.utc}` : `Original: ${t.original}`;
  return (
    <span className="timestamp-wrap">
      <time className="timestamp" dateTime={t.valid ? t.original : undefined} title={title} data-original={t.original}>
        {label ? <span className="timestamp-label">{label} </span> : null}
        {t.local}
      </time>
      {showOriginal ? (
        <span className="timestamp-detail">
          <span>
            Original <code>{t.original}</code>
          </span>
          {t.valid ? (
            <span>
              UTC <code>{t.utc}</code>
            </span>
          ) : null}
        </span>
      ) : null}
    </span>
  );
}
