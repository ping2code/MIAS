import { useId, type ReactNode } from "react";

/** A titled card section of a detail page (a labelled region). */
export function DetailSection({
  title,
  description,
  actions,
  children,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section className="card detail-section" aria-labelledby={id}>
      <header className="card-header">
        <h2 id={id}>{title}</h2>
        {actions ? <div className="card-actions">{actions}</div> : null}
      </header>
      {description ? <p className="hint">{description}</p> : null}
      {children}
    </section>
  );
}

/** Section-shaped skeletons for a detail page while it loads. */
export function DetailSkeleton({ sections = 3 }: { sections?: number }) {
  return (
    <div className="detail-grid" role="status" aria-live="polite" aria-busy="true">
      <span className="visually-hidden-text">Loading details…</span>
      {Array.from({ length: sections }, (_, i) => (
        <div className="card skeleton-card" key={i} aria-hidden="true">
          <span className="skeleton skeleton-title" />
          <span className="skeleton" />
          <span className="skeleton skeleton-short" />
          <span className="skeleton" />
        </div>
      ))}
    </div>
  );
}
