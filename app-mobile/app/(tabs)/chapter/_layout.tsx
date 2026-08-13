/**
 * Chapter tab stack: role-gated hub + tree, treasurer, secretary, members.
 * DESIGN §8.6: the ENTIRE Orgs stack renders inside the current org's
 * OrgAccentScope — one wrap here covers every screen in this stack (hero,
 * tool tiles, chips, treasurer balance all re-accent via useTheme()).
 */

import { Stack } from "expo-router";

import { MOCK_CHAPTER } from "@/mocks/data";
import { OrgAccentScope } from "@/theme";

export default function ChapterLayout() {
  return (
    <OrgAccentScope primary={MOCK_CHAPTER.colors.primary} secondary={MOCK_CHAPTER.colors.secondary}>
      <Stack screenOptions={{ headerShown: false }} />
    </OrgAccentScope>
  );
}
