import { useEffect, useRef, useState } from "react";
import { copyText } from "../lib/canonical";

/** Copies exactly `text`. The accessible name says what is copied; the result is announced politely. */
export function CopyButton({ text, label, compact = false }: { text: string; label: string; compact?: boolean }) {
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => {
    clearTimeout(timer.current);
  }, []);
  const onClick = async (): Promise<void> => {
    const ok = await copyText(text);
    setStatus(ok ? "copied" : "failed");
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      setStatus("idle");
    }, 2000);
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
        <span aria-hidden="true">⧉</span>
        {compact ? null : " Copy"}
      </button>
      <span className="copy-status" role="status" aria-live="polite">
        {status === "copied" ? "Copied" : status === "failed" ? "Copy unavailable — select the text instead" : ""}
      </span>
    </span>
  );
}
