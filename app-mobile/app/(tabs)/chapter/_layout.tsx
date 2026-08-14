/**
 * Chapter tab stack: role-gated hub + tree, treasurer, secretary, members.
 * DESIGN §8.6: the ENTIRE Orgs stack renders inside the current org's
 * OrgAccentScope — one wrap here covers every screen in this stack (hero,
 * tool tiles, chips, treasurer balance all re-accent via useTheme()).
 *
 * OwnChapterProvider (src/org) owns the membership derivation and the single
 * getChapter() fetch for the caller's own chapter — mounted here so every
 * screen below (this layout's OrgAccentScope lookup, chapter/index.tsx,
 * members.tsx) reads the same membership/chapter instead of each re-deriving
 * memberships[0] and re-fetching. Real membership data: the org's colors are
 * looked up by org_name (mocks/data.ts's orgColorsByName, with a Chirp-brand
 * default for orgs that aren't one of the two color-seeded mocks).
 */

import { Stack } from "expo-router";

import { DEFAULT_ORG_COLORS, orgColorsByName } from "@/mocks/data";
import { OwnChapterProvider, useOwnChapter } from "@/org/OwnChapterProvider";
import { OrgAccentScope } from "@/theme";

function ChapterStack() {
  const { chapter } = useOwnChapter();
  const colors = chapter !== null ? orgColorsByName(chapter.org_name) : DEFAULT_ORG_COLORS;

  return (
    <OrgAccentScope primary={colors.primary} secondary={colors.secondary}>
      <Stack screenOptions={{ headerShown: false }} />
    </OrgAccentScope>
  );
}

export default function ChapterLayout() {
  return (
    <OwnChapterProvider>
      <ChapterStack />
    </OwnChapterProvider>
  );
}
