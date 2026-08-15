/**
 * Invite-code forwarding for the deep-link chain (join-chapter -> sign-in ->
 * account-type -> join-chapter). One place owns the query-param name and
 * encoding so a new screen inserted into the chain can't silently drop the
 * code by hand-rolling its own template.
 */
export function withInviteCode(path: string, code?: string | null): string {
  return code ? `${path}?code=${encodeURIComponent(code)}` : path;
}

/**
 * Public https origin for links that leave the app. Mirrors the API_BASE_URL
 * pattern in src/api/client.ts: env-overridable, with the deployed default baked
 * in so a build with no .env still produces working links.
 */
export const WEB_BASE_URL: string =
  process.env.EXPO_PUBLIC_WEB_URL ?? "https://chirps-prod.web.app";

/**
 * The link an officer actually SHARES with a prospective member.
 *
 * Deliberately https and not `chirp://`: a custom scheme is not linkified by
 * Messages, WhatsApp, Instagram DMs or most mail clients, so a raw chirp:// URL
 * arrives as dead text exactly where invites get sent. The https page at
 * web/public/join-chapter/index.html reads the same `code` param and hands off
 * to chirp://join-chapter?code=..., so the in-app chain is unchanged — this only
 * changes what survives the trip through a text message.
 *
 * Path must stay /join-chapter: app.json declares it in both the iOS
 * associatedDomains entry and the Android intent filter, so once universal links
 * are wired this same URL opens the app directly with no page render at all.
 */
export function inviteShareUrl(code?: string | null): string {
  return withInviteCode(`${WEB_BASE_URL}/join-chapter`, code);
}
