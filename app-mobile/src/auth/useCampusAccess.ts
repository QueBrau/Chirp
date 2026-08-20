import { useSession } from "./SessionProvider";

/**
 * The one answer every campus-gated screen should ask for (c110).
 *
 * WHY THIS EXISTS. Before c88 the client could ask "does this user have a
 * campus_id" and that was the same question as "may they see campus content".
 * It is not any more: c96 made a chapter invite write campus_id, and c88 moved
 * the server's gate onto a verification timestamp, so a chapter-joined user has
 * a campus AND is refused. Screens branching on campus_id sent that user down
 * the call-the-endpoint path into a 403 which their loader rendered as a generic
 * error — indistinguishable from the wifi being down, and the "bare error toast"
 * outcome the product ruling explicitly rejected.
 *
 * The states are deliberately four, not two, because each is a different screen:
 *
 *   "loading"    - not resolved yet. Render nothing decisive. Never flash a
 *                  verify prompt at a student who turns out to be verified.
 *   "ok"         - verified and current. Load the campus content.
 *   "unverified" - has never proved an .edu. Verify screen, first-time copy.
 *   "lapsed"     - proved one, and the yearly re-check has passed. Verify screen,
 *                  "your verification expired" copy. Telling a returning student
 *                  they have never been here is how you lose them.
 *
 * Note "lapsed" is derived from `verified === false` WITH a non-null verified_at,
 * which is exactly why the endpoint returns verified_at even when it refuses.
 */
export type CampusAccess = "loading" | "ok" | "unverified" | "lapsed";

export function useCampusAccess(): CampusAccess {
  const { campusVerification } = useSession();
  if (campusVerification === null) return "loading";
  if (campusVerification.verified) return "ok";
  return campusVerification.verified_at === null ? "unverified" : "lapsed";
}
