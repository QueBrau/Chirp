/** Shared UI primitives barrel — screens compose these (CONVENTIONS: components are primitives only). */

export { AppText, type AppTextProps, type TextTone } from "./AppText";
export { Avatar, type AvatarProps } from "./Avatar";
export { AvatarStack, type AvatarStackPerson, type AvatarStackProps } from "./AvatarStack";
export { Badge, type BadgeProps, type BadgeTone } from "./Badge";
export { Button, type ButtonProps, type ButtonVariant } from "./Button";
export { Card, type CardProps } from "./Card";
export { Chip, type ChipProps, type ChipVariant } from "./Chip";
export { CreateEventSheet, type CreateEventInput, type CreateEventSheetProps } from "./CreateEventSheet";
export { CreateSheet, type CreateSheetProps } from "./CreateSheet";
export { EmptyState, type EmptyStateProps } from "./EmptyState";
export { Fab, type FabProps } from "./Fab";
export { GradientAvatar, type GradientAvatarProps } from "./GradientAvatar";
export { HeroCard, type HeroCardProps } from "./HeroCard";
export { ListRow, type ListRowProps } from "./ListRow";
export { MediaPostCard, type MediaPostCardProps } from "./MediaPostCard";
export { MomentsRow, type MomentsRowMoment, type MomentsRowProps } from "./MomentsRow";
export { Screen, type ScreenProps } from "./Screen";
export { SectionHeader, type SectionHeaderProps } from "./SectionHeader";
export { VotePill, type VoteDirection, type VotePillProps } from "./VotePill";

/** Bottom padding scroll content needs to clear the floating tab bar. */
export { TAB_BAR_CLEARANCE } from "@/theme";
