/**
 * CSV export share helper for the treasurer ledger export and secretary meetings
 * export (SPEC §7 milestone 9). Writes the CSV to a cache file, then hands it to
 * the OS share sheet via expo-sharing. UI-free by design: no Alerts, no
 * components — screens own their own error presentation (loading/toast/etc).
 *
 * Uses the SDK 54 File/Directory API (`import { File, Paths } from "expo-file-system"`),
 * not the old `writeAsStringAsync`/`documentDirectory` functions — those now live
 * under `expo-file-system/legacy` and the ones re-exported from the package root
 * throw at runtime in this SDK.
 */

import { File, Paths } from "expo-file-system";
import * as Sharing from "expo-sharing";

/**
 * Collapse anything that isn't filename-safe into `_` so a chapter/meeting name
 * containing `/` (or other path-breaking characters) can't produce a broken or
 * escaping file path. Falls back to a generic name if nothing survives.
 */
function sanitizeFilename(name: string): string {
  const cleaned = name
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return cleaned.length > 0 ? cleaned : "export";
}

/**
 * Write `csv` to a cache file named `filename` (`.csv` appended if missing) and
 * open the native share sheet for it. Throws if sharing isn't available on this
 * platform — notably web, where react-native-web has no share sheet to open.
 */
export async function shareCsv(filename: string, csv: string): Promise<void> {
  const available = await Sharing.isAvailableAsync();
  if (!available) {
    throw new Error("Sharing is not available on this device.");
  }

  const safeName = sanitizeFilename(filename);
  const fileName = safeName.toLowerCase().endsWith(".csv") ? safeName : `${safeName}.csv`;
  const file = new File(Paths.cache, fileName);
  file.write(csv);

  await Sharing.shareAsync(file.uri, {
    mimeType: "text/csv",
    UTI: "public.comma-separated-values-text",
  });
}
