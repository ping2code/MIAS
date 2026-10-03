import { humanize, toneFor, type BadgeKind } from "../lib/domain";

const SYMBOL: Record<string, string> = { neutral: "•", info: "ℹ", positive: "✓", negative: "✕", changed: "△" };

/** A domain value: always the text (a label derived from the literal value), colour secondary. */
export function DomainBadge({ kind, value, label }: { kind: BadgeKind; value: string; label?: string }) {
  const tone = toneFor(kind, value);
  return (
    <span className={`badge badge-${tone}`} title={value} data-value={value}>
      <span aria-hidden="true" className="badge-symbol">
        {SYMBOL[tone]}
      </span>
      {label ?? humanize(value)}
    </span>
  );
}
