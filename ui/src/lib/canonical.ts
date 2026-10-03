/**
 * Copy and download of canonical artifacts. Both always use the exact text the API returned (never a re-serialised
 * parse), so the bytes match the content id.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator === "undefined" || !("clipboard" in navigator)) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** A Blob of the original text (UTF-8, as received), typed application/json. */
export function canonicalBlob(text: string): Blob {
  return new Blob([text], { type: "application/json" });
}

export function downloadText(text: string, fileName: string): void {
  const url = URL.createObjectURL(canonicalBlob(text));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 0);
}

export function byteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** A formatted (pretty-printed) copy for reading only. Returns null if the text is not JSON. */
export function formatted(text: string): string | null {
  try {
    return JSON.stringify(JSON.parse(text) as unknown, null, 2);
  } catch {
    return null;
  }
}
