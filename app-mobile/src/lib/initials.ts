/** Display initials for a person's name — shared by the avatars and the lineage tree. */

/**
 * First + last initial, uppercased: "Jose Perdomo" -> "JP", "Cher" -> "C",
 * "" -> "?". Quotes and periods are stripped first, so a nickname written
 * `Robert "Bo" Vance` still initials as RV rather than R".
 *
 * This lived in three separate files before c239 (Avatar, GradientAvatar, and
 * tree/layout), and the third copy had already drifted textually: it wrote the
 * last initial as `(words[at][0] ?? "")`. That `?? ""` can never fire — the
 * preceding `.filter(Boolean)` guarantees every remaining word is non-empty, so
 * `[0]` is always a real character — and it is dropped here rather than kept,
 * since carrying it forward implies an empty-word case that does not exist.
 *
 * It sits in lib rather than next to the avatars on purpose: tree/layout.ts is
 * pure geometry and must not start importing from components/.
 */
export function initials(name: string): string {
  const words = name.replace(/["'.]/g, "").split(/\s+/).filter(Boolean);
  const first = words[0]?.[0] ?? "?";
  const last = words.length > 1 ? words[words.length - 1][0] : "";
  return `${first}${last}`.toUpperCase();
}
