import type { ReactNode } from "react";

export interface MetadataItem {
  term: string;
  value: ReactNode;
  /** Shown under the term in small text (e.g. the literal field name). */
  field?: string;
}

/** A labelled key/value list. Every row is a real field of the view; absent fields are simply not listed. */
export function MetadataList({ items, columns = 1 }: { items: readonly MetadataItem[]; columns?: 1 | 2 }) {
  return (
    <dl className={`meta-list meta-list-${String(columns)}`}>
      {items.map((item) => (
        <div className="meta-row" key={item.term}>
          <dt>
            {item.term}
            {item.field ? <code className="field-name">{item.field}</code> : null}
          </dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}
