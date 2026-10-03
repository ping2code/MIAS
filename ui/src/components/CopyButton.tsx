import { useEffect, useRef, useState } from "react";
import { copyText } from "../lib/canonical";

/** Copies exactly `text`. The accessible name says what is copied; the result is announced politely. */
export function CopyButton({
  text,
  label,
  compact = false,
  fallback = "Copy unavailable — select the text manually.",
}: {
  text: string;
  label: string;
  compact?: boolean;
  fallback?: string;
}) {
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => {
    clearTimeout(timer.current);
  }, []);
  const onClick = async (): Promise<void> => {
    const ok = await copyText(text);
    setStatus(ok ? "copied" : "failed");
    clearTimeout(timer.current);
    if (ok) {
      timer.current = setTimeout(() => {
        setStatus("idle");
      }, 2000);
    }
  };
  return (
    <span className="copy">
      <button
        type="button"
        className={compact ? "icon-button" : "button button-quiet button-small"}
        aria-label={label}
        title={label}
        onClick={() => void onClick()}
      >
        <svg className="icon" aria-hidden="true" focusable="false" viewBox="0 0 16 16" width="14" height="14">
          <rect x="5" y="5" width="9" height="9" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
          <path d="M3.5 10.5h-.5A1.5 1.5 0 0 1 1.5 9V3A1.5 1.5 0 0 1 3 1.5h6A1.5 1.5 0 0 1 10.5 3v.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
        </svg>
        {compact ? null : " Copy"}
      </button>
      <span className="copy-status" role="status" aria-live="polite">
        {status === "copied" ? "Copied" : status === "failed" ? fallback : ""}
      </span>
    </span>
  );
}
