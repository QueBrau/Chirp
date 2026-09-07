/**
 * The three backend account types (SPEC §2.4), with the friendlier framing
 * DESIGN.md §6 asks for ("I'm a student" / "I'm in a fraternity or sorority" /
 * "I'm an alum"). Shared by (auth)/account-type.tsx (the first choice, at
 * signup) and (tabs)/profile/account-type.tsx (board c381 — changing it
 * later), so the same choice reads identically in both places instead of
 * drifting into two copies of "what does 'greek' mean" over time.
 */

import type { ComponentProps } from "react";
import type { Feather } from "@expo/vector-icons";

import type { AccountType } from "@/api/auth";

export type AccountTypeIconName = ComponentProps<typeof Feather>["name"];

export interface AccountTypeOption {
  type: AccountType;
  icon: AccountTypeIconName;
  title: string;
  description: string;
}

export const ACCOUNT_TYPE_OPTIONS: readonly AccountTypeOption[] = [
  {
    type: "non_greek",
    icon: "user",
    title: "I'm a student",
    description: "Campus feed, the anonymous board, and messaging with friends.",
  },
  {
    type: "greek",
    icon: "users",
    title: "I'm in a fraternity or sorority",
    description: "Everything above, plus your chapter feed, dues, and the family tree.",
  },
  {
    type: "alumni",
    icon: "award",
    title: "I'm an alum",
    description: "Stay on the family tree, mentor actives, and post to the job board.",
  },
];
