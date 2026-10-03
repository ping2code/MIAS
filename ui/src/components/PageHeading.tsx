import { useEffect, useRef } from "react";

/** The page's single h1. Sets the document title and moves focus here on navigation (screen-reader friendly). */
export function PageHeading({ title, children }: { title: string; children?: React.ReactNode }) {
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    document.title = `${title} · MIAS`;
    ref.current?.focus();
  }, [title]);
  return (
    <div className="page-heading">
      <h1 ref={ref} tabIndex={-1}>
        {title}
      </h1>
      {children}
    </div>
  );
}
