/**
 * Invite-code forwarding for the deep-link chain (join-chapter -> sign-in ->
 * account-type -> join-chapter). One place owns the query-param name and
 * encoding so a new screen inserted into the chain can't silently drop the
 * code by hand-rolling its own template.
 */
export function withInviteCode(path: string, code?: string | null): string {
  return code ? `${path}?code=${encodeURIComponent(code)}` : path;
}
