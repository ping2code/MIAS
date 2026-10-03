/**
 * Descriptions of the readiness checks mias-api defines today (api/readiness.py). Checks are always rendered from
 * the API's response; a name not listed here is shown with its literal name and no description.
 */
export const CHECK_DESCRIPTION: Readonly<Record<string, string>> = {
  settings: "The API's settings were validated at start-up.",
  artifact_root: "The artifact root is configured and is a readable directory.",
  artifact_index: "The artifact index is built and its latest refresh succeeded.",
  receipt_root: "The delivery receipt directory exists and its history validates.",
};
