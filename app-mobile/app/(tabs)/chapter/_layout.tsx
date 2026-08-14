/**
 * Chapter tab stack: role-gated hub + tree, treasurer, secretary, members.
 * DESIGN §8.6: the ENTIRE Orgs stack renders inside the current org's
 * OrgAccentScope — one wrap here covers every screen in this stack (hero,
 * tool tiles, chips, treasurer balance all re-accent via useTheme()).
 *
 * Real membership data: the org's colors are looked up by org_name
 * (mocks/data.ts's orgColorsByName, with a Chirp-brand default for orgs that
 * aren't one of the two color-seeded mocks) against the chapter fetched for
 * the caller's own membership. Single-org world for now — memberships[0] is
 * the only chapter a signed-in user can belong to.
 */

import { Stack } from "expo-router";
import { useEffect, useState } from "react";

import { getChapter, type ChapterOut } from "@/api/chapters";
import { useSession } from "@/auth";
import { DEFAULT_ORG_COLORS, orgColorsByName } from "@/mocks/data";
import { OrgAccentScope } from "@/theme";

export default function ChapterLayout() {
  const { memberships } = useSession();
  const chapterId = memberships[0]?.chapter_id ?? null;
  const [chapter, setChapter] = useState<ChapterOut | null>(null);

  useEffect(() => {
    if (chapterId === null) {
      setChapter(null);
      return;
    }
    // Fail soft: an errored/loading fetch just wears the default org colors
    // rather than blocking the stack from rendering.
    getChapter(chapterId)
      .then(setChapter)
      .catch(() => setChapter(null));
  }, [chapterId]);

  const colors = chapter !== null ? orgColorsByName(chapter.org_name) : DEFAULT_ORG_COLORS;

  return (
    <OrgAccentScope primary={colors.primary} secondary={colors.secondary}>
      <Stack screenOptions={{ headerShown: false }} />
    </OrgAccentScope>
  );
}
